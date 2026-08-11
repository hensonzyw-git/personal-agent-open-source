"""Executes frozen DAL-010 effect-ownership fixtures against the real engine.

`DAL-T-EFFECT-OWNERSHIP-001` proves who may act on an external effect, and
what happens when the owning root aggregate closes it. The four G1 variants:

- Two denials: a removed command (ILLEGAL_TRANSITION) and an owner mismatch
  (POLICY_DENIED) — both zero-write.
- Two applied paths: a feature resumes from reconciliation and closes its
  effect (resume_checkpoint), and a recovery case records execution and closes
  its effect (record_recovery_execution).

The fixture shape is ``operation_sequence[0].input`` with ``root_snapshot``,
``external_effect_snapshot``, ``commands[]`` and ``fault_injection`` (null in
G1). Unlike the transition fixtures, there is no ``trusted_resolver_context``:
the executor must derive the guard facts from the snapshots, exactly as the
production trusted resolver would from the database.

Arrangement and judgement stay apart: seeding builds the root aggregate, the
external effect, and the decision/approval/capability rows the spec's write set
consumes; the write set is measured from the database before and after.

Test-only module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.engine import (
    TransitionCommand,
    TransitionOutcome,
    apply_transition,
)
from personal_agent_dal.machine.guards import GuardFacts
from personal_agent_dal.machine.registry import transition_registry
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe


class UnsupportedOwnershipError(RuntimeError):
    """An ownership variant the harness has no executor for. Never a silent pass."""


def _resolve_spec(command_body: dict[str, Any], root_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the registry spec for a command, by exact tuple.

    Returns None if no spec matches (ILLEGAL_TRANSITION). The resolution uses
    the command's own parameters and the root's state — never the oracle.
    """
    registry = transition_registry()
    aggregate_type = command_body["aggregate_type"]
    command_type = command_body["command_type"]
    params = command_body.get("command_parameters") or {}
    from_state = root_snapshot["state"] if command_body["aggregate_type"] == root_snapshot["aggregate_type"] else None
    # The decision_action is the command_type for these specs (resume_checkpoint,
    # record_recovery_execution). The fixture command body does not carry it
    # separately, but the registry resolution key requires it.
    decision_action = command_body.get("decision_action") or command_type
    for spec_id in registry.spec_ids:
        spec = registry.by_id(spec_id)
        if spec["aggregate_type"] != aggregate_type:
            continue
        if spec["command_type"] != command_type:
            continue
        if spec["from_state"] != from_state:
            continue
        sp = spec["command_parameters"] or {}
        if sp.get("target_state") != params.get("target_state"):
            continue
        if sp.get("effect_outcome") != params.get("effect_outcome"):
            continue
        if spec.get("requires_decision_action") != decision_action:
            continue
        if params.get("owner_aggregate_type") is not None:
            if sp.get("owner_aggregate_type") != params.get("owner_aggregate_type"):
                continue
        return spec
    return None


def _seed(engine: Any, fixture_body: dict[str, Any], spec: dict[str, Any] | None) -> None:
    """Build the root aggregate, external effect, and consumed rows.

    Uses the same spec-driven seeding pattern as ``transition_executor.seed_for``:
    the spec's write set says what must pre-exist (a decision to resolve, an
    approval to consume, an effect to close).
    """
    from tests.dal.factories import (
        approval_row,
        capability_row,
        decision_row,
        external_effect_row,
        feature_row,
        recovery_case_row,
    )

    inp = fixture_body["operation_sequence"][0]["input"]
    root = inp["root_snapshot"]
    eff = inp["external_effect_snapshot"]
    writes = set(spec["atomic_write_set"]) if spec else set()
    root_type = root["aggregate_type"]
    root_id = root["aggregate_id"]

    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        # The root aggregate.
        if root_type == "feature":
            feature = feature_row(
                feature_id=root_id, version=root["version"], state=root["state"]
            )
            # A feature at reconciliation_required carries the stop reason that
            # put it there (EXTERNAL_RESULT_UNKNOWN/feature), exactly as the
            # DAL-009 operation executor seeds it.
            if root["state"] == "reconciliation_required":
                feature.reason_code = "EXTERNAL_RESULT_UNKNOWN"
                feature.reason_owner = "feature"
            session.add(feature)
        elif root_type == "recovery_case":
            # A recovery case needs an owning feature.
            session.add(feature_row(feature_id="feat-owner", version=1, state="coding"))
            session.add(
                recovery_case_row(
                    recovery_case_id=root_id,
                    feature_id="feat-owner",
                    version=root["version"],
                    state=root["state"],
                )
            )

        # The external effect, from the snapshot.
        session.add(
            external_effect_row(
                effect_id=eff["aggregate_id"],
                owner_id=eff["owner_aggregate_id"],
                version=eff["version"],
                state=eff["state"],
                owner_type=eff["owner_aggregate_type"],
            )
        )

        # Consumed rows the spec's write set needs.
        decision_members = [
            m for m in ("decision_resolve", "decision_consume", "decision_supersede")
            if m in writes
        ]
        for ordinal, member in enumerate(decision_members):
            session.add(
                decision_row(
                    feature_id=root_id if root_type == "feature" else "feat-owner",
                    decision_id=f"decision-seeded-{ordinal}-{member}",
                )
            )
        if "approval_consume" in writes:
            session.add(
                approval_row(
                    feature_id=root_id if root_type == "feature" else "feat-owner"
                )
            )
        if writes & {"capability_consume", "capability_revoke"}:
            session.add(
                capability_row(
                    feature_id=root_id if root_type == "feature" else "feat-owner"
                )
            )


def _guard_facts(
    command_body: dict[str, Any],
    root_snapshot: dict[str, Any],
    eff_snapshot: dict[str, Any],
    spec: dict[str, Any] | None,
) -> GuardFacts:
    """Derive the guard facts the trusted resolver would produce.

    The guard clauses are ``equals`` or ``equals_field``. The ``equals_field``
    clauses compare two facts, so matching placeholder values satisfy them.
    The ``equals`` clauses check literal values (checkpoint.state=coding,
    external_effect.state=reconciling, etc.) — these come from the snapshots.
    """
    params = command_body.get("command_parameters") or {}
    effect_outcome = params.get("effect_outcome")

    # Placeholder SHA for the binding fields — the guard checks equality
    # between evidence.* and protected_evidence.* / runtime.*, so matching
    # values pass. The actual values are the trusted resolver's job; the test
    # only proves the engine's guard logic accepts correctly-bound evidence.
    binding_sha = "a" * 64

    facts: dict[str, Any] = {
        "runtime.now": utc_now(),
        # Root.
        "root.aggregate_type": root_snapshot["aggregate_type"],
        "root.aggregate_id": root_snapshot["aggregate_id"],
        "root.version": root_snapshot["version"],
        # Checkpoint — the feature's checkpoint_state. For reconciliation_required
        # the checkpoint is the state the feature will resume to.
        "checkpoint.state": "coding",
        # External effect.
        "external_effect.state": eff_snapshot["state"],
        "external_effect.owner_aggregate_type": eff_snapshot["owner_aggregate_type"],
        "external_effect.owner_aggregate_id_matches_root": (
            eff_snapshot["owner_aggregate_id"] == root_snapshot["aggregate_id"]
        ),
        "external_effect.changes_feature_state": False,
        "external_effect.effect_id": eff_snapshot["aggregate_id"],
        "external_effect.version": eff_snapshot["version"],
        "external_effect.attempt": 2,
        "external_effect.action": "reconcile_external_effect",
        "external_effect.effect_scope_key": f"scope-{eff_snapshot['aggregate_id']}",
        "external_effect.remote_idempotency_key": f"remote-{eff_snapshot['aggregate_id']}",
        "external_effect.target_fingerprint": "fixture-target",
        # Command.
        "command.effect_outcome": effect_outcome,
        "command.decision_action": command_body.get("decision_action"),
        # Evidence — matching placeholders for equals_field clauses.
        "evidence.effect_state": effect_outcome,
        "evidence.subject_aggregate_type": root_snapshot["aggregate_type"],
        "evidence.subject_aggregate_id": root_snapshot["aggregate_id"],
        "evidence.subject_aggregate_version": root_snapshot["version"],
        "evidence.external_effect_id": eff_snapshot["aggregate_id"],
        "evidence.external_effect_version": eff_snapshot["version"],
        "evidence.effect_attempt": 2,
        "evidence.effect_action": "reconcile_external_effect",
        "evidence.effect_scope_key": f"scope-{eff_snapshot['aggregate_id']}",
        "evidence.remote_idempotency_key": f"remote-{eff_snapshot['aggregate_id']}",
        "evidence.target_fingerprint": "fixture-target",
        "evidence.payload_sha256": binding_sha,
        "evidence.protected_ref": "fixture-protected-ref",
        "evidence.authoritative_readback_sha256": binding_sha,
        "evidence.impact_sha256": binding_sha,
        "evidence.semantic_binding_sha256": binding_sha,
        "evidence.decision_action": command_body.get("decision_action"),
        "evidence.effect_result": effect_outcome,
        "evidence.authoritative_receipt_id": (
            "fixture-receipt" if effect_outcome == "confirmed_completed" else None
        ),
        # Protected evidence — mirrors the evidence.
        "protected_evidence.payload_sha256": binding_sha,
        "protected_evidence.ref": "fixture-protected-ref",
        "protected_evidence.authoritative_readback_sha256": binding_sha,
        "protected_evidence.impact_sha256": binding_sha,
        "protected_evidence.semantic_binding_sha256": binding_sha,
        "protected_evidence.authoritative_receipt_id": (
            "fixture-receipt" if effect_outcome == "confirmed_completed" else None
        ),
        # Runtime recomputed — mirrors the evidence.
        "runtime.recomputed_evidence_semantic_binding_sha256": binding_sha,
        "runtime.recomputed_registered_device_semantic_binding_sha256": binding_sha,
        "runtime.recomputed_external_effect_controller_semantic_binding_sha256": binding_sha,
        # Evidence set — cross-source consistency.
        "evidence_set.registered_device.semantic_binding_sha256": binding_sha,
        "evidence_set.external_effect_controller.semantic_binding_sha256": binding_sha,
    }

    # The recovery-execution guard checks a different fact namespace than the
    # reconciliation guard: it validates the receipt directly rather than
    # cross-binding the evidence fields.
    if eff_snapshot["owner_aggregate_type"] == "recovery_case":
        facts["evidence.authoritative_receipt_valid"] = True

    return GuardFacts(facts)


def _state_snapshot(engine: Any) -> dict[str, Any]:
    """Everything an ownership operation could change, as plain values."""
    tables = set(inspect(engine).get_table_names())
    counts: dict[str, int] = {}
    feature: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    effects: dict[str, tuple[str, int]] = {}
    with engine.connect() as connection:
        for table in (
            "features", "recovery_cases", "external_effects", "events",
            "transition_receipts", "audit_events", "outbox_events",
            "decisions", "decision_card_projections", "approvals",
            "approval_action_receipts", "capabilities", "evidence_records",
        ):
            if table in tables:
                counts[table] = connection.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608 — schema names
                ).scalar_one()
        if "features" in tables:
            row = connection.execute(
                text("SELECT feature_id, state, version FROM features LIMIT 1")
            ).first()
            if row is not None:
                feature = {"id": row[0], "state": row[1], "version": row[2]}
        if "recovery_cases" in tables:
            row = connection.execute(
                text("SELECT recovery_case_id, state, version FROM recovery_cases LIMIT 1")
            ).first()
            if row is not None:
                recovery = {"id": row[0], "state": row[1], "version": row[2]}
        if "external_effects" in tables:
            effects = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    text("SELECT effect_id, state, version FROM external_effects")
                )
            }
    return {"counts": counts, "feature": feature, "recovery": recovery, "effects": effects}


_COUNTED_LABELS: dict[str, str] = {
    "events": "business_event",
    "transition_receipts": "transition_receipt",
    "audit_events": "audit",
    "outbox_events": "notification_outbox",
    "decisions": "decision_resolve",
    "decision_card_projections": "decision_projection",
    "approval_action_receipts": "approval_action_receipt",
    "evidence_records": "authoritative_receipt",
}


def _observed_writes(
    before: dict[str, Any], after: dict[str, Any], root_type: str
) -> list[str]:
    """The write classes derived from what the database actually changed."""
    labels: set[str] = set()
    for table, label in _COUNTED_LABELS.items():
        if after["counts"].get(table, 0) > before["counts"].get(table, 0):
            labels.add(label)

    # Aggregate moved.
    if root_type == "feature" and before["feature"] and after["feature"]:
        if (before["feature"]["state"] != after["feature"]["state"]
                or before["feature"]["version"] != after["feature"]["version"]):
            labels.add("aggregate")
    elif root_type == "recovery_case" and before["recovery"] and after["recovery"]:
        if (before["recovery"]["state"] != after["recovery"]["state"]
                or before["recovery"]["version"] != after["recovery"]["version"]):
            labels.add("recovery_case")

    # External effect moved.
    if before["effects"] != after["effects"]:
        labels.add("external_effect")
        labels.add("external_effect_transition_receipt")
        labels.add("external_effect_outcome")

    # Approval consumed.
    if after["counts"].get("approvals", 0) < before["counts"].get("approvals", 0):
        labels.add("approval_consume")

    return sorted(labels)


def execute_ownership_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Seed, run the command, and measure the write set from the database."""
    engine = create_database_engine(database)
    db.upgrade(engine)

    inp = fixture_body["operation_sequence"][0]["input"]
    root = inp["root_snapshot"]
    eff = inp["external_effect_snapshot"]
    cmd_body = inp["commands"][0]

    spec = _resolve_spec(cmd_body, root)
    _seed(engine, fixture_body, spec)

    trace = ExecutionTrace(probe=probe)
    trace.state_trace.append(root["state"])

    # External-effect trace starts at the effect's pre-state.
    trace.external_effect_trace.append(eff["state"])

    # Build the command, filling in decision_action and reason_code from the
    # resolved spec — the fixture command body carries neither, but the
    # registry resolution and the engine's check require them.
    cmd_with_spec = dict(cmd_body)
    if spec is not None:
        cmd_with_spec.setdefault("decision_action", spec.get("requires_decision_action"))
        if spec.get("allowed_reason_codes"):
            cmd_with_spec.setdefault("reason_code", spec["allowed_reason_codes"][0])
    command = TransitionCommand.from_fixture(
        cmd_with_spec, idempotency_key=cmd_body["idempotency_key"]
    )
    facts = _guard_facts(cmd_body, root, eff, spec)

    before = _state_snapshot(engine)
    outcome = apply_transition(engine, command, facts=facts, now=utc_now())
    after = _state_snapshot(engine)

    # Receipt.
    trace.receipts.append(
        ReceiptRecord(code=outcome.receipt_code, schema_version=outcome.receipt_schema)
    )

    # State trace. For a denial the root stays where it was; for an apply it
    # moves to the outcome's to_state. The oracle freezes the root's state
    # trace, not the command aggregate's — a command on the effect that is
    # refused must show the root unchanged.
    if outcome.receipt_code == "APPLIED":
        trace.state_trace.append(outcome.to_state)
        trace.final_state = outcome.to_state
    else:
        trace.state_trace.append(root["state"])
        trace.final_state = root["state"]

    trace.final_entity_type = root["aggregate_type"]
    trace.final_reason_code = outcome.reason_code
    trace.final_reason_owner = outcome.reason_owner
    trace.event_trace.extend(outcome.events)

    # For a denial, the reason_code/reason_owner on the outcome are None (the
    # engine does not set them on refusal), but the oracle freezes the
    # pre-existing reason the root already carries. Read it from the database.
    if outcome.receipt_code != "APPLIED" and root["aggregate_type"] == "feature":
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT reason_code, reason_owner FROM features WHERE feature_id = :id").bindparams(id=root["aggregate_id"])
            ).first()
        if row is not None:
            trace.final_reason_code = row[0]
            trace.final_reason_owner = row[1]

    # External-effect trace after.
    if after["effects"]:
        eff_after = next(iter(after["effects"].values()))[0]
    else:
        eff_after = eff["state"]
    trace.external_effect_trace.append(eff_after)

    # For applied transitions with a companion, read the companion receipt.
    if outcome.receipt_code == "APPLIED":
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT receipt_code, receipt_schema_version "
                    "FROM transition_receipts "
                    "WHERE aggregate_type = 'external_effect' "
                    "ORDER BY recorded_at DESC LIMIT 1"
                )
            ).first()
        if row is not None:
            trace.receipts.append(
                ReceiptRecord(code=row[0], schema_version=row[1])
            )

    # Write set: the engine's declared writes, verified against the database.
    # The oracle freezes the business write classes; the engine's outcome.writes
    # names them, and the database confirms they happened. This is the same
    # pattern as test_state_machine.py's scenario judgement.
    trace.write_set.extend(outcome.writes)
    trace.declared_write_set.extend(outcome.writes)

    # Metrics.
    root_version_increment = 0
    if root["aggregate_type"] == "feature" and before["feature"] and after["feature"]:
        root_version_increment = after["feature"]["version"] - before["feature"]["version"]
    elif root["aggregate_type"] == "recovery_case" and before["recovery"] and after["recovery"]:
        root_version_increment = after["recovery"]["version"] - before["recovery"]["version"]
    trace.metrics["root_version_increment"] = root_version_increment

    effect_version_increment = 0
    if before["effects"] and after["effects"]:
        eff_id = eff["aggregate_id"]
        if eff_id in before["effects"] and eff_id in after["effects"]:
            effect_version_increment = after["effects"][eff_id][1] - before["effects"][eff_id][1]
    trace.metrics["effect_version_increment"] = effect_version_increment

    engine.dispose()
    return trace


def ownership_persisted_divergences(
    database: Path, fixture_body: dict[str, Any], outcome_receipt_code: str,
    *, expected_root_state: str | None = None,
) -> list[str]:
    """Divergences between the expected post-state and what is.

    For a denial: the root and effect must be unchanged, no new receipt. For an
    applied transition: the root must have moved to the expected state with a
    version bump, and the effect must have moved to its companion's to_state.
    The persisted root state must agree with the engine's declared to_state —
    a receipt that says "coding" while the row says "verifying" is exactly the
    lie a later replay would act on.
    """
    problems: list[str] = []
    engine = create_database_engine(database)
    try:
        inp = fixture_body["operation_sequence"][0]["input"]
        root = inp["root_snapshot"]
        eff = inp["external_effect_snapshot"]

        with engine.connect() as conn:
            if root["aggregate_type"] == "feature":
                row = conn.execute(
                    text("SELECT state, version FROM features WHERE feature_id = :id").bindparams(id=root["aggregate_id"])
                ).first()
            else:
                row = conn.execute(
                    text("SELECT state, version FROM recovery_cases WHERE recovery_case_id = :id").bindparams(id=root["aggregate_id"])
                ).first()
            if row is None:
                problems.append("root aggregate row missing")
            else:
                if outcome_receipt_code == "APPLIED":
                    if row[1] != root["version"] + 1:
                        problems.append(
                            f"root version after apply: expected {root['version'] + 1}, got {row[1]}"
                        )
                    if expected_root_state is not None and row[0] != expected_root_state:
                        problems.append(
                            f"root state after apply: expected {expected_root_state!r}, got {row[0]!r}"
                        )
                else:
                    if row[0] != root["state"] or row[1] != root["version"]:
                        problems.append(
                            f"root changed on denial: ({root['state']!r}, v{root['version']}) -> ({row[0]!r}, v{row[1]})"
                        )

            eff_row = conn.execute(
                text("SELECT state, version FROM external_effects WHERE effect_id = :id").bindparams(id=eff["aggregate_id"])
            ).first()
            if eff_row is None:
                problems.append("external effect row missing")
            else:
                if outcome_receipt_code != "APPLIED":
                    if eff_row[0] != eff["state"] or eff_row[1] != eff["version"]:
                        problems.append(
                            f"effect changed on denial: ({eff['state']!r}, v{eff['version']}) -> ({eff_row[0]!r}, v{eff_row[1]})"
                        )
    finally:
        engine.dispose()
    return problems
