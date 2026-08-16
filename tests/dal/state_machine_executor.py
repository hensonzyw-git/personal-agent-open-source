"""Executes frozen DAL-009 state-machine operation fixtures against the real engine.

The DAL-009 scenarios that survived the B3 closure are the four operation
specs the single-transition harness cannot consume on its own:

- `OP-REC-001` (`record_external_effect_outcome`) — a business operation that
  drives the external-effect lifecycle (`intent_recorded -> claimed ->
  dispatch_started -> unknown`) and then stops the owning feature, or is denied
  because the effect is unknown.
- `OP-RESTART-001` (`resume_persisted_run`) — a restart evaluates persisted
  loop counters and blocks the feature when a retry or review limit is hit.
- `OP-CMD-IDEMPOTENCY-001` (`record_plan`) — an idempotent replay returns the
  original receipt; a reused key with different content is refused.
- `OP-EVENT-ORDER-001` (`apply_business_event`) — an event with a stale
  expected version is refused.

Like the single-transition harness, this module keeps arrangement and judgement
apart. **Arrangement** (seeding) may read the contract: what must pre-exist is
read from the fixture's authoritative facts and the pre-state. **Judgement** is
the caller's: the write set is measured from the database before and after the
whole sequence, never taken from the engine's account of itself.

The resolver below is the test-side counterpart of the production trusted
resolver (DAL-010+). It derives each registry command from the frozen
registry's own `actor_evidence_bindings`, `required_evidence_schema_versions`,
`command_parameters` and `allowed_reason_codes` — never from the oracle — so an
executor that picks the wrong spec fails against the frozen expectation.

Test-only module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.engine import (
    RECEIPT_SCHEMAS,
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


class UnsupportedOperationError(RuntimeError):
    """An operation spec the harness has no executor for. Never a silent pass."""


def _spec_for(
    aggregate_type: str, from_state: str | None, command_type: str, **resolution: Any
) -> dict[str, Any]:
    """The single registry spec matching a resolution key, by exact tuple.

    Fail loudly rather than defaulting: an unknown command is a harness gap,
    and silently picking a different spec would turn an unimplemented scenario
    into a passing one.
    """
    registry = transition_registry()
    matches = []
    for spec_id in registry.spec_ids:
        spec = registry.by_id(spec_id)
        if spec["aggregate_type"] != aggregate_type:
            continue
        if spec["from_state"] != from_state:
            continue
        if spec["command_type"] != command_type:
            continue
        parameters = spec["command_parameters"] or {}
        if "target_state" in resolution and parameters.get("target_state") != resolution["target_state"]:
            continue
        if "effect_outcome" in resolution and parameters.get("effect_outcome") != resolution["effect_outcome"]:
            continue
        if "decision_action" in resolution and spec["requires_decision_action"] != resolution["decision_action"]:
            continue
        if "reason_code" in resolution and spec["allowed_reason_codes"] != [resolution["reason_code"]]:
            continue
        matches.append(spec)
    if not matches:
        raise UnsupportedOperationError(
            f"no registry spec for ({aggregate_type}, {from_state}, {command_type}, "
            f"{resolution})"
        )
    if len(matches) > 1:
        raise UnsupportedOperationError(
            f"resolution not unique: {[m['spec_id'] for m in matches]}"
        )
    return matches[0]


def _command_for_spec(
    spec: dict[str, Any],
    *,
    aggregate_id: str,
    expected_version: int | None,
    idempotency_key: str,
) -> TransitionCommand:
    """A command built from the spec's own binding, evidence and parameters."""
    binding = spec["actor_evidence_bindings"][0]
    return TransitionCommand(
        aggregate_type=spec["aggregate_type"],
        aggregate_id=aggregate_id,
        command_type=spec["command_type"],
        command_parameters=dict(spec["command_parameters"] or {}),
        actor_type=binding["actor_type"],
        evidence_source_types=tuple(binding["required_evidence_source_types"]),
        evidence_schema_versions=tuple(spec["required_evidence_schema_versions"]),
        decision_action=spec["requires_decision_action"],
        reason_code=(
            spec["allowed_reason_codes"][0] if spec["allowed_reason_codes"] else None
        ),
        expected_version=expected_version,
        idempotency_key=idempotency_key,
    )


def _feature_state(
    engine: Any, feature_id: str
) -> tuple[str | None, int | None, str | None, str | None]:
    """The feature's (state, version, reason_code, reason_owner), or Nones."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, version, reason_code, reason_owner "
                "FROM features WHERE feature_id = :fid"
            ).bindparams(fid=feature_id)
        ).first()
    if row is None:
        return (None, None, None, None)
    return (row[0], row[1], row[2], row[3])


def _effect_state(engine: Any, effect_id: str) -> str | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state FROM external_effects WHERE effect_id = :eid"
            ).bindparams(eid=effect_id)
        ).first()
    return row[0] if row is not None else None


def _receipt_id_for(engine: Any, idempotency_key: str) -> str | None:
    """The persisted receipt for an idempotency key, if one exists.

    A replay and its original share the same receipt row (§2.6); the conflict
    seed already carries one. Reading it back lets the trace report the actual
    receipt ids the sequence touched, which the oracle's `unique_receipt_ids`
    asserts against.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT receipt_id FROM transition_receipts "
                "WHERE idempotency_key = :key"
            ).bindparams(key=idempotency_key)
        ).first()
    return row[0] if row is not None else None


def _companion_ids_for(engine: Any, root_idempotency_key: str) -> list[str]:
    """Companion receipts for one root command, by shared idempotency key root.

    A companion is applied in the same atomic transaction as its root command
    and persists a receipt whose `idempotency_key` is the root command's key
    plus a companion suffix (§2.3.1, `<root-key>:effect` / `<root-key>:case`).
    Independent lifecycle commands use unrelated key roots, so an exact
    `<root-key>:` prefix match separates a companion from a separately
    dispatched command.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT DISTINCT spec_id FROM transition_receipts "
                "WHERE idempotency_key LIKE :prefix ESCAPE '\\'"
            ).bindparams(prefix=root_idempotency_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%")
        ).all()
    return sorted({row[0] for row in rows if row[0]})


# ---------------------------------------------------------------------------
# Arrangement: build the world the operation expects.
# ---------------------------------------------------------------------------


def _seed_receipt_for_conflict(session: Any, operation: dict[str, Any], facts: dict[str, Any]) -> None:
    """Seed the prior receipt that makes `idempotency_conflict` refuse.

    The fixture declares the *stored* payload hash for the same idempotency key
    that the operation will submit with different content. Seeding an APPLIED
    receipt with the stored hash lets the engine's own idempotency check detect
    the mismatch and refuse with `IDEMPOTENCY_CONFLICT` — no harness-forced code.
    """
    from personal_agent_dal.storage.machine_models import TransitionReceipt

    stored = facts["stored_idempotency_payload_sha256"]
    session.add(
        TransitionReceipt(
            receipt_id=f"receipt-seeded-conflict-{operation['idempotency_key'][-12:]}",
            idempotency_key=operation["idempotency_key"],
            aggregate_type="feature",
            aggregate_id=operation["input"]["target"]["entity_id"],
            aggregate_version=1,
            spec_id="SM-PLAN-READY",
            command_type="record_plan",
            from_state="planning",
            to_state="awaiting_plan_review",
            receipt_code="APPLIED",
            receipt_schema_version=RECEIPT_SCHEMAS["feature"],
            request_payload_sha256=stored,
            event_id=None,
            recorded_at=utc_now(),
        )
    )


def seed_state_machine(engine: Any, fixture_body: dict[str, Any]) -> None:
    """Build the fixture's pre-state from its authoritative facts alone.

    Arrangement, not judgement: what has to pre-exist is read from the fixture
    — the feature's pre-state, and the external effect the REC-001 scenarios
    act on. The seeds never branch on the expected *outcome*; they describe the
    world the operation starts from.
    """
    from tests.dal.factories import event_row, external_effect_row, feature_row

    test_id = fixture_body["test_id"]
    variant = fixture_body["variant_id"]
    first = fixture_body["operation_sequence"][0]
    target = first["input"]["target"]
    facts = first["input"]["authoritative_facts"]

    feature_id = target["entity_id"]
    feature_state = target["state"]
    feature_version = target["version"]

    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        feature = feature_row(
            feature_id=feature_id, version=feature_version, state=feature_state
        )
        # A feature already stopped at reconciliation_required carries the stop
        # reason that put it there: the registry's only path into that state
        # (`require_reconciliation`, `REC-UNKNOWN--*`) is
        # EXTERNAL_RESULT_UNKNOWN, so the pre-state a scenario starts from must
        # record it. The fixture names only the state; the reason is part of
        # the world the state implies.
        if feature_state == "reconciliation_required":
            feature.reason_code = "EXTERNAL_RESULT_UNKNOWN"
            feature.reason_owner = "feature"
        # A paused feature was paused by the user; the reason that put it there
        # is part of the world the state implies (mirroring the stop reason the
        # reconciliation_required branch records).
        if feature_state == "paused":
            feature.reason_code = "USER_PAUSE"
            feature.reason_owner = "feature"
        session.add(feature)

        if test_id == "DAL-T-REC-001":
            effect_id = facts["external_effect_id"]
            if variant.startswith("synthetic") or variant.startswith("worker"):
                # The operation records an *outcome* for an effect whose intent
                # was already recorded: the effect sits in intent_recorded, and
                # its creation event is the durable record of that fact.
                effect_state = "intent_recorded"
                intent = event_row(
                    event_id="event-intent-recorded", aggregate_version=1
                )
                intent.event_type = "external_effect.intent_recorded"
                intent.aggregate_type = "external_effect"
                intent.aggregate_id = effect_id
                session.add(intent)
            else:
                # unknown_*_cancel: the feature is already stopped for an
                # unknown effect; cancellation must be refused.
                effect_state = "unknown"
            session.add(
                external_effect_row(
                    effect_id=effect_id,
                    owner_id=feature_id,
                    version=1,
                    state=effect_state,
                    owner_type="feature",
                )
            )

        if (
            test_id == "DAL-T-CMD-IDEMPOTENCY-001"
            and variant == "idempotency_conflict"
        ):
            _seed_receipt_for_conflict(session, first, facts)


# ---------------------------------------------------------------------------
# Write-set observation — the independent second opinion.
# ---------------------------------------------------------------------------


def _state_snapshot(engine: Any) -> dict[str, Any]:
    """Everything a state-machine operation could change, as plain values."""
    tables = set(inspect(engine).get_table_names())
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in (
            "features",
            "external_effects",
            "events",
            "transition_receipts",
            "audit_events",
            "decisions",
            "decision_card_projections",
            "outbox_events",
            "approvals",
            "capabilities",
            "leases",
            "recovery_cases",
            "evidence_records",
            "impact_reports",
        ):
            if table in tables:
                counts[table] = connection.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608 — schema names
                ).scalar_one()
        feature = None
        if "features" in tables:
            row = connection.execute(
                text(
                    "SELECT state, version, reason_code, external_effect_inventory_sha256 "
                    "FROM features LIMIT 1"
                )
            ).first()
            if row is not None:
                feature = {
                    "state": row[0],
                    "version": row[1],
                    "reason": row[2],
                    "inventory": row[3],
                }
        effects: dict[str, tuple[str, int]] = {}
        if "external_effects" in tables:
            effects = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    text("SELECT effect_id, state, version FROM external_effects")
                )
            }
    return {
        "tables": tables,
        "counts": counts,
        "feature": feature,
        "effects": effects,
    }


#: Physical change -> registry write-set member. The oracle freezes the
#: *classes* an operation must write at the business level (`allowed_write_set`
#: for these operations is the feature-side E set); the external-effect
#: lifecycle's physical rows share those tables, so its `external_effect` and
#: `external_effect_transition_receipt` members collapse onto `aggregate` and
#: `transition_receipt` — the effect's own progression is judged by the
#: `expected_external_effect_trace` dimension instead.
_COUNTED_STATE_LABELS: dict[str, str] = {
    "events": "business_event",
    "transition_receipts": "transition_receipt",
    "audit_events": "audit",
    "decisions": "decision_create",
    "decision_card_projections": "decision_projection",
    "outbox_events": "notification_outbox",
}


def _observed_state_writes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The write classes derived from what the database actually changed."""
    labels: set[str] = set()

    for table, label in _COUNTED_STATE_LABELS.items():
        if after["counts"].get(table, 0) > before["counts"].get(table, 0):
            labels.add(label)

    if before["feature"] is not None and after["feature"] is not None:
        before_f, after_f = before["feature"], after["feature"]
        if (
            before_f["state"] != after_f["state"]
            or before_f["version"] != after_f["version"]
            or before_f["reason"] != after_f["reason"]
        ):
            labels.add("aggregate")
        if before_f["inventory"] != after_f["inventory"]:
            labels.add("external_effect_inventory")
    if set(after["effects"]) != set(before["effects"]) or any(
        after["effects"].get(eid) != state for eid, state in before["effects"].items()
    ):
        labels.add("aggregate")

    return sorted(labels)


# ---------------------------------------------------------------------------
# The resolver: business commands -> registry transitions.
# ---------------------------------------------------------------------------

#: Injected transport status -> the guard's `executor.failure_shape` fact.
#: The G1 `synthetic_*` and G2 `worker_*` variants share the same effect
#: lifecycle; they differ only in which executor was lost (a synthetic transport
#: vs the Home Mac Worker).
_FAILURE_SHAPES: dict[str, str] = {
    "response_lost_after_remote_accept": "response_lost",
    "connection_lost_after_dispatch": "response_lost",
    "process_killed_after_dispatch": "executor_terminated",
    "worker_disconnected_after_dispatch": "response_lost",
    "worker_killed_after_dispatch": "executor_terminated",
}


def _record_external_effect_outcome(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-REC-001: drive the effect lifecycle and stop the owning feature.

    Returns the trace plus the number of transition-emitted events, so the
    fixture can accumulate the `business_event_count` metric without counting
    the seeded intent event.
    """
    trace = ExecutionTrace()
    transition_events = 0
    actions = [step["command"] for step in operation["input"]["action_sequence"]]
    injected = operation["input"].get("injected_results") or []
    effect_id = facts["external_effect_id"]
    feature_id = target["entity_id"]
    feature_version = target["version"]
    step_key = operation["idempotency_key"]

    trace.external_effect_trace.append(_effect_state(engine, effect_id))

    if actions == ["cancel_feature_with_unknown_effect"]:
        # The feature is already stopped for an unknown merge/deploy effect.
        # Moving it out of reconciliation_required is a human act on a
        # registered device (§2.3.1); this operation is submitted by the
        # service controller, so the actor allowlist denies it before any
        # write — cancellation with an unknown outcome is refused.
        effect_action = facts["effect_action"]
        command_type = (
            "accept_deploy_result" if effect_action == "deploy" else "accept_merge_result"
        )
        spec = _spec_for(
            "feature",
            "reconciliation_required",
            command_type,
            target_state={"merge": "merged", "deploy": "deployed"}[effect_action],
            effect_outcome="confirmed_completed",
        )
        command = _command_for_spec(
            spec,
            aggregate_id=feature_id,
            expected_version=feature_version,
            idempotency_key=f"{step_key}:reconcile",
        )
        outcome = apply_transition(engine, command, facts=GuardFacts({}))
        transition_events += len(outcome.events)
        _record_feature_outcome(
            trace, engine, feature_id, target["state"], outcome,
            idempotency_key=f"{step_key}:reconcile",
        )
        return trace, transition_events

    # Transport-loss variants (synthetic and worker): the four recording steps
    # drive the effect from intent_recorded to unknown, then the feature stops
    # for an unknown external result.
    trace.event_trace.append("external_effect.intent_recorded")
    steps = [
        {
            "command_type": "claim_external_effect",
            "from_state": "intent_recorded",
            "to_state": "claimed",
            "guard": {"capability.epoch_current": True, "lease.epoch_current": True},
        },
        {
            "command_type": "record_effect_dispatch",
            "from_state": "claimed",
            "to_state": "dispatch_started",
            "guard": {"executor.claim_matches": True, "executor.claim_expired": False},
        },
        {
            "command_type": "record_effect_unknown",
            "from_state": "dispatch_started",
            "to_state": "unknown",
            "guard": {"executor.failure_shape": _FAILURE_SHAPES[injected[0]["status"]]},
        },
    ]

    effect_version = 1
    for index, step in enumerate(steps):
        spec = _spec_for(
            "external_effect",
            step["from_state"],
            step["command_type"],
            target_state=step["to_state"],
            effect_outcome=None,
        )
        command = _command_for_spec(
            spec,
            aggregate_id=effect_id,
            expected_version=effect_version,
            idempotency_key=f"{step_key}:effect:{index}",
        )
        outcome = apply_transition(engine, command, facts=GuardFacts(step["guard"]))
        if outcome.receipt_code != "APPLIED":
            raise AssertionError(
                f"effect lifecycle step {step['command_type']} refused: "
                f"{outcome.receipt_code}"
            )
        effect_version += 1
        trace.external_effect_trace.append(outcome.to_state)
        trace.event_trace.extend(outcome.events)
        transition_events += len(outcome.events)

    # The feature stops for the unknown external result.
    spec = _spec_for(
        "feature",
        target["state"],
        "require_reconciliation",
        target_state="reconciliation_required",
        effect_outcome=None,
        reason_code="EXTERNAL_RESULT_UNKNOWN",
    )
    command = _command_for_spec(
        spec,
        aggregate_id=feature_id,
        expected_version=feature_version,
        idempotency_key=f"{step_key}:feature",
    )
    outcome = apply_transition(
        engine,
        command,
        facts=GuardFacts({"effect_inventory.unknown_or_reconciling_count": 1}),
    )
    if outcome.receipt_code != "APPLIED":
        raise AssertionError(f"feature stop refused: {outcome.receipt_code}")
    transition_events += len(outcome.events)
    trace.event_trace.extend(outcome.events)
    _record_feature_outcome(
        trace, engine, feature_id, target["state"], outcome,
        idempotency_key=f"{step_key}:feature",
    )
    return trace, transition_events


def _resume_persisted_run(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-RESTART-001: evaluate persisted loop counters and block when a limit hit.

    The operation loads the persisted run, evaluates the loop budget and either
    resumes or blocks. The only frozen variants block: a transient-retry count
    at its limit (`BLK-TRANSIENT`) or a review-fix count at its limit
    (`BLK-LOOP`). No provider call happens after restart — the decision is made
    from the persisted counters the fixture supplies.
    """
    trace = ExecutionTrace()
    feature_id = target["entity_id"]
    max_retries = facts["max_transient_retries"]
    max_review = facts["max_review_fixes"]
    retry_count = facts["transient_retry_count"]
    review_count = facts["review_fix_count"]

    if retry_count >= max_retries:
        spec = _spec_for(
            "feature",
            target["state"],
            "block_feature",
            target_state="needs_human",
            effect_outcome=None,
            reason_code="TRANSIENT_RETRY_EXHAUSTED",
        )
    elif review_count >= max_review:
        spec = _spec_for(
            "feature",
            target["state"],
            "block_feature",
            target_state="needs_human",
            effect_outcome=None,
            reason_code="REVIEW_LOOP_LIMIT",
        )
    else:
        raise UnsupportedOperationError(
            "a resume that neither limit blocks is not a frozen RESTART variant"
        )

    command = _command_for_spec(
        spec,
        aggregate_id=feature_id,
        expected_version=target["version"],
        idempotency_key=operation["idempotency_key"],
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    trace.event_trace.extend(outcome.events)
    _record_feature_outcome(
        trace, engine, feature_id, target["state"], outcome,
        idempotency_key=operation["idempotency_key"],
    )

    trace.metrics.update(
        {
            "provider_call_count_after_restart": 0,
            "provider_attempt_count": facts["provider_attempt_count"],
            "transient_retry_count": facts["transient_retry_count"],
            "review_fix_count": facts["review_fix_count"],
        }
    )
    return trace, len(outcome.events)


def _record_plan(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-CMD-IDEMPOTENCY-001: record a plan, letting the engine's own
    idempotency check answer a replay or a conflicting reuse.

    The command is built from the operation's own actor/evidence (the planner
    binding of `SM-PLAN-READY`); no registry spec is resolved here because the
    engine's idempotency check runs *before* resolution — a replayed key
    returns the original receipt and a reused key with different content is
    refused, in both cases before any spec is consulted.
    """
    trace = ExecutionTrace()
    command = TransitionCommand(
        aggregate_type="feature",
        aggregate_id=target["entity_id"],
        command_type="record_plan",
        command_parameters={"target_state": "awaiting_plan_review", "effect_outcome": None},
        actor_type=operation["actor_type"],
        evidence_source_types=(operation["evidence_source_type"],),
        evidence_schema_versions=("dal.evidence.plan/1.0",),
        decision_action=None,
        reason_code=None,
        expected_version=target["version"],
        idempotency_key=operation["idempotency_key"],
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    trace.event_trace.extend(outcome.events)
    _record_feature_outcome(
        trace, engine, target["entity_id"], target["state"], outcome,
        idempotency_key=operation["idempotency_key"],
    )
    return trace, len(outcome.events)


def _apply_business_event(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-EVENT-ORDER-001: appending an event with a stale expected version is
    refused by the engine's own compare-and-swap version check."""
    trace = ExecutionTrace()
    # The event-store appends `plan.approved`; the next registry command that
    # resolves from the `approved` state is `start_provider`. The event arrives
    # with `event_expected_version` 6 while the aggregate is at 7 — the
    # out-of-order event must be refused as VERSION_CONFLICT before any write.
    spec = _spec_for(
        "feature",
        target["state"],
        "start_provider",
        target_state="coding",
        effect_outcome=None,
    )
    command = _command_for_spec(
        spec,
        aggregate_id=target["entity_id"],
        expected_version=facts["event_expected_version"],
        idempotency_key=operation["idempotency_key"],
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    trace.event_trace.extend(outcome.events)
    _record_feature_outcome(
        trace, engine, target["entity_id"], target["state"], outcome,
        idempotency_key=operation["idempotency_key"],
    )
    return trace, len(outcome.events)


def _verify_git_mutation_preconditions(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-GIT-BASE-001: judge the git read-back and block on drift/conflict.

    The pure policy reports whether the mutation preconditions hold; the frozen
    G2 variants each fail one precondition, so the resolver blocks the feature
    with the ``BLK-GIT--coding`` transition (``GIT_CONFLICT``). A clean
    read-back is not a frozen GIT-BASE variant and is refused as a harness gap.
    """
    from personal_agent_dal.github.git_base import verify_git_mutation_preconditions

    trace = ExecutionTrace()
    evaluation = verify_git_mutation_preconditions(operation)
    if not evaluation.conflict:
        raise UnsupportedOperationError(
            "a clean git read-back is not a frozen GIT-BASE variant"
        )
    spec = _spec_for(
        "feature",
        target["state"],
        "block_feature",
        target_state="needs_human",
        effect_outcome=None,
        reason_code=evaluation.reason,
    )
    command = _command_for_spec(
        spec,
        aggregate_id=target["entity_id"],
        expected_version=target["version"],
        idempotency_key=operation["idempotency_key"],
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    trace.event_trace.extend(outcome.events)
    _record_feature_outcome(
        trace, engine, target["entity_id"], target["state"], outcome,
        idempotency_key=operation["idempotency_key"],
    )
    return trace, len(outcome.events)


def _evaluate_untrusted_content_for_coding(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-INJECTION-001 (coding carriers): block a tainted escalation.

    The pure injection policy reports ``POLICY_FAILURE`` as the block reason for
    a coding-state carrier whose content is tainted and requests escalation; the
    resolver blocks the feature with ``BLK-POLICY--coding``. A clean carrier is
    not a frozen INJECTION G2 variant and is refused as a harness gap.
    """
    from personal_agent_dal.machine.injection import evaluate_untrusted_content

    trace = ExecutionTrace()
    evaluation = evaluate_untrusted_content(operation)
    if evaluation.block_reason != "POLICY_FAILURE":
        raise UnsupportedOperationError(
            "a clean coding injection is not a frozen INJECTION G2 variant"
        )
    spec = _spec_for(
        "feature",
        target["state"],
        "block_feature",
        target_state="needs_human",
        effect_outcome=None,
        reason_code="POLICY_FAILURE",
    )
    command = _command_for_spec(
        spec,
        aggregate_id=target["entity_id"],
        expected_version=target["version"],
        idempotency_key=operation["idempotency_key"],
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    trace.event_trace.extend(outcome.events)
    _record_feature_outcome(
        trace, engine, target["entity_id"], target["state"], outcome,
        idempotency_key=operation["idempotency_key"],
    )
    return trace, len(outcome.events)


def _evaluate_lease(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-LEASE-001: refuse a dead lease, block a base drift.

    The pure lease policy reports a dead lease as ``CAPABILITY_STALE`` (a
    zero-write refusal) and a base drift as ``STATE_DRIFT`` (which the resolver
    carries into ``BLK-DRIFT--paused``). A clean lease is not a frozen LEASE-001
    variant and is refused as a harness gap.
    """
    from personal_agent_dal.machine.lease import evaluate_lease

    trace = ExecutionTrace()
    evaluation = evaluate_lease(operation)

    if evaluation.block_reason == "STATE_DRIFT":
        spec = _spec_for(
            "feature",
            target["state"],
            "block_feature",
            target_state="needs_human",
            effect_outcome=None,
            reason_code="STATE_DRIFT",
        )
        command = _command_for_spec(
            spec,
            aggregate_id=target["entity_id"],
            expected_version=target["version"],
            idempotency_key=operation["idempotency_key"],
        )
        outcome = apply_transition(engine, command, facts=GuardFacts({}))
        trace.event_trace.extend(outcome.events)
        _record_feature_outcome(
            trace, engine, target["entity_id"], target["state"], outcome,
            idempotency_key=operation["idempotency_key"],
        )
        return trace, len(outcome.events)

    if evaluation.receipt.code.value == "APPLIED":
        raise UnsupportedOperationError(
            "a clean lease is not a frozen LEASE-001 variant"
        )

    # A dead lease is refused with zero writes: the feature stays put and no
    # receipt is persisted, so the receipt is recorded from the pure decision
    # and the final reason is read from the seeded row (USER_PAUSE for paused).
    trace.receipts.append(
        ReceiptRecord(
            code=evaluation.receipt.code.value,
            schema_version=evaluation.receipt.schema_version,
        )
    )
    post_state, _, reason_code, reason_owner = _feature_state(engine, target["entity_id"])
    trace.state_trace.append(post_state or target["state"])
    trace.final_state = post_state or target["state"]
    trace.final_reason_code = reason_code
    trace.final_reason_owner = reason_owner
    trace.final_entity_type = "feature"
    return trace, 0


def _block_on_policy_failure(
    engine: Any,
    operation: dict[str, Any],
    target: dict[str, Any],
    policy,
) -> tuple[ExecutionTrace, int]:
    """Run a pure guard and block the feature on its ``POLICY_FAILURE`` verdict.

    The four worker-isolation guards (PATH/NET/CRED/SECRET-OUTPUT) all share the
    same resolver shape: a pure decision reports a conflict, and the resolver
    carries the reason into the ``BLK-POLICY--coding`` transition. A clean
    verdict is not a frozen G2 variant and is refused as a harness gap.
    """
    trace = ExecutionTrace()
    evaluation = policy(operation)
    if not evaluation.conflict:
        raise UnsupportedOperationError(
            "a clean guard verdict is not a frozen G2 variant"
        )
    spec = _spec_for(
        "feature",
        target["state"],
        "block_feature",
        target_state="needs_human",
        effect_outcome=None,
        reason_code=evaluation.reason,
    )
    command = _command_for_spec(
        spec,
        aggregate_id=target["entity_id"],
        expected_version=target["version"],
        idempotency_key=operation["idempotency_key"],
    )
    outcome = apply_transition(engine, command, facts=GuardFacts({}))
    trace.event_trace.extend(outcome.events)
    _record_feature_outcome(
        trace, engine, target["entity_id"], target["state"], outcome,
        idempotency_key=operation["idempotency_key"],
    )
    return trace, len(outcome.events)


def _evaluate_path(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-PATH-001: block a path escape with ``POLICY_FAILURE``."""
    from personal_agent_dal.machine.path import evaluate_path

    return _block_on_policy_failure(engine, operation, target, evaluate_path)


def _evaluate_network(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-NET-001: block a denied network request with ``POLICY_FAILURE``."""
    from personal_agent_dal.machine.net import evaluate_network_request

    return _block_on_policy_failure(engine, operation, target, evaluate_network_request)


def _evaluate_credential(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-CRED-001: block a credential-boundary breach with ``POLICY_FAILURE``."""
    from personal_agent_dal.machine.cred import evaluate_credential_boundary

    return _block_on_policy_failure(engine, operation, target, evaluate_credential_boundary)


def _evaluate_secret_output(
    engine: Any,
    operation: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
) -> tuple[ExecutionTrace, int]:
    """OP-SECRET-OUTPUT-001: block a secret leak with ``POLICY_FAILURE``."""
    from personal_agent_dal.machine.secret_output import evaluate_secret_output

    return _block_on_policy_failure(engine, operation, target, evaluate_secret_output)


def _record_feature_outcome(
    trace: ExecutionTrace,
    engine: Any,
    feature_id: str,
    pre_state: str,
    outcome: TransitionOutcome,
    *,
    idempotency_key: str,
) -> None:
    """Record the feature-level trace for one operation's final transition.

    The operation's business receipt is the feature's: the effect lifecycle is
    judged by its own trace dimensions (events and external-effect states), so
    the effect's per-step receipts are deliberately not added here. The receipt
    id is the row actually persisted for the operation's idempotency key — a
    replay reuses the original, which is what `unique_receipt_ids` asserts.
    """
    trace.receipts.append(
        ReceiptRecord(
            code=outcome.receipt_code,
            schema_version=outcome.receipt_schema,
            duplicate=outcome.duplicate,
            receipt_id=_receipt_id_for(engine, idempotency_key),
        )
    )
    post_state, _, reason_code, reason_owner = _feature_state(engine, feature_id)
    trace.state_trace.append(post_state or pre_state)
    trace.final_state = post_state or pre_state
    trace.final_reason_code = reason_code
    trace.final_reason_owner = reason_owner
    trace.final_entity_type = "feature"
    # A companion transition persists its own receipt whose idempotency key is
    # the root command's key with a companion suffix (§2.3.1); detect any so an
    # unsanctioned atomic companion fails the companion assertion.
    trace.companion_ids.extend(_companion_ids_for(engine, idempotency_key))


def execute_state_machine_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Seed, run every operation against the real engine, merge the traces.

    The write set is measured from the database across the whole sequence —
    never from the engine's own account — so a transition that silently stopped
    writing is visible as a missing class.
    """
    engine = create_database_engine(database)
    db.upgrade(engine)
    seed_state_machine(engine, fixture_body)

    merged = ExecutionTrace(probe=probe)
    pre_state = fixture_body["operation_sequence"][0]["input"]["target"]["state"]
    initial_version = fixture_body["operation_sequence"][0]["input"]["target"]["version"]
    merged.state_trace.append(pre_state)
    transition_events = 0

    before = _state_snapshot(engine)
    for command in fixture_body["operation_sequence"]:
        target = command["input"]["target"]
        facts = command["input"]["authoritative_facts"]
        operation_spec_id = command["operation_spec_id"]
        if operation_spec_id == "OP-REC-001":
            trace, events = _record_external_effect_outcome(engine, command, facts, target)
        elif operation_spec_id == "OP-RESTART-001":
            trace, events = _resume_persisted_run(engine, command, facts, target)
        elif operation_spec_id == "OP-CMD-IDEMPOTENCY-001":
            trace, events = _record_plan(engine, command, facts, target)
        elif operation_spec_id == "OP-EVENT-ORDER-001":
            trace, events = _apply_business_event(engine, command, facts, target)
        elif operation_spec_id == "OP-GIT-BASE-001":
            trace, events = _verify_git_mutation_preconditions(engine, command, facts, target)
        elif operation_spec_id == "OP-INJECTION-001":
            trace, events = _evaluate_untrusted_content_for_coding(engine, command, facts, target)
        elif operation_spec_id == "OP-LEASE-001":
            trace, events = _evaluate_lease(engine, command, facts, target)
        elif operation_spec_id == "OP-PATH-001":
            trace, events = _evaluate_path(engine, command, facts, target)
        elif operation_spec_id == "OP-NET-001":
            trace, events = _evaluate_network(engine, command, facts, target)
        elif operation_spec_id == "OP-CRED-001":
            trace, events = _evaluate_credential(engine, command, facts, target)
        elif operation_spec_id == "OP-SECRET-OUTPUT-001":
            trace, events = _evaluate_secret_output(engine, command, facts, target)
        else:
            engine.dispose()
            raise UnsupportedOperationError(
                f"no state-machine executor for {operation_spec_id}"
            )
        transition_events += events
        merged.state_trace.extend(trace.state_trace)
        merged.receipts.extend(trace.receipts)
        merged.event_trace.extend(trace.event_trace)
        merged.external_effect_trace.extend(trace.external_effect_trace)
        merged.metrics.update(trace.metrics)
        merged.final_state = trace.final_state
        merged.final_reason_code = trace.final_reason_code
        merged.final_reason_owner = trace.final_reason_owner
        merged.final_entity_type = trace.final_entity_type
    after = _state_snapshot(engine)

    merged.write_set.extend(_observed_state_writes(before, after))
    merged.declared_write_set.extend(merged.write_set)

    # Scenario metrics the frozen assertions judge.
    merged.metrics["business_event_count"] = transition_events
    if initial_version is not None:
        final_version = after["feature"]["version"] if after["feature"] else initial_version
        merged.metrics["aggregate_version_increment"] = final_version - initial_version

    engine.dispose()
    return merged


# ---------------------------------------------------------------------------
# Persisted content: the receipts as rows, not as the outcome reported them.
# ---------------------------------------------------------------------------


def _persisted_receipt_rows(engine: Any) -> list[dict[str, Any]]:
    """Every persisted receipt, read raw: what a replay or audit would see."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT aggregate_type, aggregate_id, aggregate_version, spec_id, "
                "from_state, to_state, receipt_code, receipt_schema_version, "
                "idempotency_key FROM transition_receipts"
            )
        ).all()
    return [
        {
            "aggregate_type": row[0],
            "aggregate_id": row[1],
            "aggregate_version": row[2],
            "spec_id": row[3],
            "from_state": row[4],
            "to_state": row[5],
            "receipt_code": row[6],
            "receipt_schema_version": row[7],
            "idempotency_key": row[8],
        }
        for row in rows
    ]


#: The external-effect lifecycle the REC-001 synthetic variants record, in the
#: order the resolver drives it. Checking the persisted EE receipts against
#: this sequence is what catches an executor that picked the wrong spec or
#: recorded the wrong state transition: the oracle's write-set classes cannot
#: see *which* receipt a transition persisted, only that one exists.
_EFFECT_LIFECYCLE: tuple[tuple[str, str, str], ...] = (
    ("EE-CLAIM", "intent_recorded", "claimed"),
    ("EE-DISPATCH", "claimed", "dispatch_started"),
    ("EE-DISPATCH-UNKNOWN", "dispatch_started", "unknown"),
)


def _refused_problems(
    problems: list[str],
    receipts: list[dict[str, Any]],
    operation: dict[str, Any],
    engine: Any,
    *,
    baseline_receipt_count: int = 0,
) -> None:
    """What must hold of the database after a refused operation.

    A refusal is zero-write: no new receipt beyond the `baseline_receipt_count`
    the arrangement planted (the conflict variant's stored receipt is seeded,
    not written by the operation), and the aggregate row must be exactly where
    the fixture put it. The CMD-IDEMPOTENCY replay is the odd one out — it
    persists a receipt and moves the feature because the operation sequence
    applies the command first and replays it second; its applied step is
    checked by the applied branch, so it never reaches this function.
    """
    target = operation["input"]["target"]
    if len(receipts) != baseline_receipt_count:
        problems.append(
            f"refused operation persisted {len(receipts)} receipt(s) beyond "
            f"the seeded baseline of {baseline_receipt_count}"
        )
    post_state, post_version, _, _ = _feature_state(engine, target["entity_id"])
    if post_state != target["state"] or post_version != target["version"]:
        problems.append(
            f"refused operation moved the feature: "
            f"({target['state']!r}, v{target['version']}) -> ({post_state!r}, v{post_version})"
        )


def _applied_feature_receipt_problems(
    problems: list[str],
    receipt: dict[str, Any],
    *,
    expected_id: str,
    expected_spec: str | None,
    pre_version: int,
    post_state: str | None,
    post_version: int | None,
) -> None:
    """The applied feature receipt must record the resolved transition."""
    checks: dict[str, tuple[Any, Any]] = {
        "aggregate_id": (receipt["aggregate_id"], expected_id),
        "aggregate_version": (receipt["aggregate_version"], pre_version + 1),
    }
    if expected_spec is not None:
        checks["spec_id"] = (receipt["spec_id"], expected_spec)
        if post_state is not None:
            checks["to_state"] = (receipt["to_state"], post_state)
    for field, (actual, expected) in checks.items():
        if actual != expected:
            problems.append(
                f"feature receipt {field}: expected {expected!r}, got {actual!r}"
            )

    # The durable record and the aggregate row must agree: a receipt that says
    # the feature moved somewhere it did not is exactly the lie a later replay
    # would act on.
    if post_state is not None:
        if receipt["to_state"] != post_state:
            problems.append(
                f"feature receipt to_state {receipt['to_state']!r} != "
                f"persisted state {post_state!r}"
            )
        if post_version is not None and receipt["aggregate_version"] != post_version:
            problems.append(
                f"feature receipt version {receipt['aggregate_version']} != "
                f"persisted version {post_version}"
            )


def operation_persisted_divergences(
    database: Path, fixture_body: dict[str, Any]
) -> list[str]:
    """Divergences between what should be persisted and what actually is.

    The comparator judges the trace the executor assembled; this judges the
    database itself, so an outcome that misreports its own spec or state cannot
    hide behind a faithful trace. For each operation:

    - the feature receipt must carry the spec the operation resolved, the
      fixture's pre-state as `from_state`, the operation's final state as
      `to_state`, and exactly one version above the pre-state;
    - the feature row must agree with that receipt on state and version —
      the durable record and the aggregate it certifies cannot disagree;
    - the REC-001 synthetic lifecycle must persist exactly the three EE
      receipts of `_EFFECT_LIFECYCLE` at versions 2–4.

    The expectations come from the fixture's authoritative facts and the
    resolver's own spec resolution — never from the oracle.
    """
    problems: list[str] = []
    engine = create_database_engine(database)
    try:
        receipts = _persisted_receipt_rows(engine)
        first = fixture_body["operation_sequence"][0]
        target = first["input"]["target"]
        facts = first["input"]["authoritative_facts"]
        feature_id = target["entity_id"]
        pre_state = target["state"]
        pre_version = target["version"]

        feature_receipts = [
            r for r in receipts if r["aggregate_type"] == "feature"
        ]
        post_state, post_version, _, _ = _feature_state(engine, feature_id)

        # The scenarios split into applied (a real transition persisted its
        # receipt) and refused (the engine denied before any write). A refused
        # variant is zero-write: checking it for an applied receipt would
        # invent an expectation the contract does not have. The conflict
        # variant's stored receipt is arrangement, not outcome — the seed
        # plants it so the reused key has something to conflict with — so its
        # zero-write check counts receipts beyond that baseline.
        refused = (
            first["operation_spec_id"] == "OP-EVENT-ORDER-001"
            or (
                first["operation_spec_id"] == "OP-CMD-IDEMPOTENCY-001"
                and fixture_body["variant_id"] == "idempotency_conflict"
            )
            or (
                first["operation_spec_id"] == "OP-REC-001"
                and fixture_body["variant_id"].startswith("unknown_")
            )
            or (
                first["operation_spec_id"] == "OP-LEASE-001"
                and fixture_body["variant_id"] != "new_lease_after_drift"
            )
        )
        baseline_receipt_count = 1 if (
            first["operation_spec_id"] == "OP-CMD-IDEMPOTENCY-001"
            and fixture_body["variant_id"] == "idempotency_conflict"
        ) else 0

        if refused:
            _refused_problems(
                problems, receipts, first, engine,
                baseline_receipt_count=baseline_receipt_count,
            )
        else:
            if len(feature_receipts) != 1:
                problems.append(
                    f"expected exactly 1 feature receipt, found {len(feature_receipts)}"
                )
            else:
                _applied_feature_receipt_problems(
                    problems,
                    feature_receipts[0],
                    expected_id=feature_id,
                    expected_spec=_expected_feature_spec(
                        first["operation_spec_id"], fixture_body["variant_id"],
                        first, facts, pre_state,
                    ),
                    pre_version=pre_version,
                    post_state=post_state,
                    post_version=post_version,
                )

        # The REC-001 synthetic variants drive the effect through three
        # transitions; each must persist its own receipt with the right spec
        # and from/to states, in lifecycle order at versions 2, 3, 4.
        if (
            fixture_body["test_id"] == "DAL-T-REC-001"
            and (
                fixture_body["variant_id"].startswith("synthetic")
                or fixture_body["variant_id"].startswith("worker")
            )
        ):
            effect_id = facts["external_effect_id"]
            effect_receipts = sorted(
                (
                    r
                    for r in receipts
                    if r["aggregate_type"] == "external_effect"
                    and r["aggregate_id"] == effect_id
                ),
                key=lambda r: r["aggregate_version"],
            )
            if len(effect_receipts) != len(_EFFECT_LIFECYCLE):
                problems.append(
                    f"expected {len(_EFFECT_LIFECYCLE)} effect receipts, "
                    f"found {len(effect_receipts)}"
                )
            else:
                for index, (spec_id, from_state, to_state) in enumerate(
                    _EFFECT_LIFECYCLE
                ):
                    receipt = effect_receipts[index]
                    expected = {
                        "spec_id": spec_id,
                        "from_state": from_state,
                        "to_state": to_state,
                        "aggregate_version": index + 2,
                    }
                    for field, want in expected.items():
                        if receipt[field] != want:
                            problems.append(
                                f"effect receipt[{index}] {field}: "
                                f"expected {want!r}, got {receipt[field]!r}"
                            )
    finally:
        engine.dispose()
    return problems


def _expected_feature_spec(
    operation_spec_id: str,
    variant_id: str,
    operation: dict[str, Any],
    facts: dict[str, Any],
    pre_state: str,
) -> str | None:
    """The spec_id the feature receipt must name, from the resolver's own key.

    Returns None where the engine refuses before resolution (the idempotency
    replay/conflict variants return or refuse the *seeded* receipt, whose
    spec_id belongs to the seed, not the operation).
    """
    if operation_spec_id == "OP-REC-001":
        if variant_id.startswith("synthetic") or variant_id.startswith("worker"):
            return _spec_for(
                "feature", pre_state, "require_reconciliation",
                target_state="reconciliation_required", effect_outcome=None,
                reason_code="EXTERNAL_RESULT_UNKNOWN",
            )["spec_id"]
        effect_action = facts["effect_action"]
        command_type = (
            "accept_deploy_result" if effect_action == "deploy" else "accept_merge_result"
        )
        return _spec_for(
            "feature", "reconciliation_required", command_type,
            target_state={"merge": "merged", "deploy": "deployed"}[effect_action],
            effect_outcome="confirmed_completed",
        )["spec_id"]
    if operation_spec_id == "OP-RESTART-001":
        reason_code = (
            "TRANSIENT_RETRY_EXHAUSTED"
            if facts["transient_retry_count"] >= facts["max_transient_retries"]
            else "REVIEW_LOOP_LIMIT"
        )
        return _spec_for(
            "feature", pre_state, "block_feature",
            target_state="needs_human", effect_outcome=None,
            reason_code=reason_code,
        )["spec_id"]
    if operation_spec_id == "OP-EVENT-ORDER-001":
        return _spec_for(
            "feature", pre_state, "start_provider",
            target_state="coding", effect_outcome=None,
        )["spec_id"]
    if operation_spec_id == "OP-CMD-IDEMPOTENCY-001":
        # The replay variant applies the plan command and then replays it; the
        # single persisted receipt records the applied transition, so its
        # spec_id is the plan spec the resolver would pick.
        return _spec_for(
            "feature", pre_state, "record_plan",
            target_state="awaiting_plan_review", effect_outcome=None,
        )["spec_id"]
    if operation_spec_id == "OP-LEASE-001":
        return _spec_for(
            "feature", pre_state, "block_feature",
            target_state="needs_human", effect_outcome=None,
            reason_code="STATE_DRIFT",
        )["spec_id"]
    raise UnsupportedOperationError(
        f"no expected feature spec for {operation_spec_id}"
    )
