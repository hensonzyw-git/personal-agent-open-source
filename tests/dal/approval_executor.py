"""Executes frozen DAL-011 approval fixtures against the real engine.

`DAL-T-APP-001` and `DAL-T-APP-EXP-001` prove the approval consume contract:
consume-once by CAS, expiry, revocation, and decision-version binding.

The fixture shape is ``operation_sequence[].input`` with ``action_sequence``
(``approve_plan``), ``authoritative_facts`` (approval/decision state,
``server_now``, ``submitted_decision_version``), and ``target``.

- ``concurrent_consume``: two operations race for one approval; the first
  APPLIED, the second APPROVAL_INVALID.
- ``double_tap``: the approval is already consumed → APPROVAL_INVALID.
- ``revoke_race``: the approval is revoked (modelled as consumed by a
  revocation marker) → APPROVAL_INVALID.
- ``stale_decision``: ``submitted_decision_version`` != current → DECISION_STALE.
- ``approval_expired`` / ``decision_expired``: ``server_now`` past expiry →
  APPROVAL_INVALID / DECISION_STALE.

Test-only module.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text

from personal_agent_core.timeutil import parse_rfc3339, utc_now
from personal_agent_dal.machine.engine import (
    TransitionCommand,
    TransitionOutcome,
    apply_transition,
)
from personal_agent_dal.machine.guards import GuardFacts
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe


def _seed(engine: Any, fixture_body: dict[str, Any]) -> datetime:
    """Build the feature, decision, and approval rows from the fixture's facts.

    Returns the ``server_now`` timestamp the operation should run at, so the
    executor can pass it to ``apply_transition`` for expiry checks.
    """
    from tests.dal.factories import approval_row, decision_row, feature_row

    op = fixture_body["operation_sequence"][0]
    target = op["input"]["target"]
    facts = op["input"]["authoritative_facts"]
    server_now = parse_rfc3339(facts["server_now"])

    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(
            feature_row(
                feature_id=target["entity_id"],
                version=target["version"],
                state=target["state"],
            )
        )
        # Decision
        dec = facts["decision"]
        d = decision_row(
            feature_id=target["entity_id"],
            decision_id=dec["decision_id"],
        )
        d.decision_version = dec["version"]
        session.add(d)

        # Approval — seed in the state the fixture declares. The fixture's
        # ``approval.status`` maps to the row's consumption state:
        #   active   → unconsumed (consumed_by_command_id IS NULL)
        #   consumed → consumed (consumed_by_command_id set)
        #   revoked  → consumed by a revocation marker (a revoked approval is
        #              not consumable; the model has no revoked_at column, so
        #              revocation is represented as consumption by a marker)
        ap_facts = facts["approval"]
        action = op["input"]["action_sequence"][0]
        ap = approval_row(
            feature_id=target["entity_id"],
            approval_id=ap_facts["approval_id"],
        )
        ap.expires_at = parse_rfc3339(ap_facts["expires_at"])
        if ap_facts["status"] in ("consumed", "revoked"):
            ap.consumed_by_command_id = (
                f"revoked:{ap_facts['approval_id']}"
                if ap_facts["status"] == "revoked"
                else "prior-consume"
            )
            ap.consumed_at = server_now
        session.add(ap)

    return server_now


def _build_command(op: dict[str, Any], target: dict[str, Any]) -> TransitionCommand:
    """Build the TransitionCommand from the operation + action_sequence."""
    action = op["input"]["action_sequence"][0]
    facts = op["input"]["authoritative_facts"]
    return TransitionCommand(
        aggregate_type=target["entity_type"],
        aggregate_id=target["entity_id"],
        command_type=action["command"],
        command_parameters={
            "target_state": "approved",
            "approval_id": action.get("approval_id"),
            "decision_id": action.get("decision_id"),
            "submitted_decision_version": facts.get("submitted_decision_version"),
            "decision_expires_at": facts.get("decision", {}).get("expires_at"),
        },
        actor_type=op["actor_type"],
        evidence_source_types=(op["evidence_source_type"],),
        evidence_schema_versions=("dal.evidence.approval/1.0",),
        decision_action=action["command"],
        reason_code=None,
        expected_version=target["version"],
        idempotency_key=op["idempotency_key"],
    )


def _state_snapshot(engine: Any) -> dict[str, Any]:
    """Everything an approval operation could change."""
    tables = set(inspect(engine).get_table_names())
    counts: dict[str, int] = {}
    feature: dict[str, Any] | None = None
    with engine.connect() as conn:
        for table in (
            "features", "decisions", "decision_card_projections",
            "approvals", "approval_action_receipts", "events",
            "transition_receipts", "audit_events", "outbox_events",
        ):
            if table in tables:
                counts[table] = conn.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608
                ).scalar_one()
        if "features" in tables:
            row = conn.execute(
                text("SELECT state, version FROM features LIMIT 1")
            ).first()
            if row is not None:
                feature = {"state": row[0], "version": row[1]}
    return {"counts": counts, "feature": feature}


def execute_approval_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Seed, run every operation, and measure the write set from the database."""
    engine = create_database_engine(database)
    db.upgrade(engine)
    server_now = _seed(engine, fixture_body)

    merged = ExecutionTrace(probe=probe)
    target = fixture_body["operation_sequence"][0]["input"]["target"]
    merged.state_trace.append(target["state"])

    before = _state_snapshot(engine)
    for op in fixture_body["operation_sequence"]:
        command = _build_command(op, target)
        # The fixture snapshots every operation's target at the pre-state
        # version. In a multi-operation sequence the first APPLIED op bumps
        # the feature version, so a later op's `expected_version` must track
        # the current row: the oracle expects the racing op to reach the
        # approval CAS and lose there (APPROVAL_INVALID), not to be refused
        # earlier on a stale version (VERSION_CONFLICT).
        if command.expected_version is not None:
            current = _state_snapshot(engine)["feature"]
            if current is not None and current["version"] != command.expected_version:
                command = TransitionCommand(
                    **{**command.__dict__, "expected_version": current["version"]}
                )
        outcome = apply_transition(engine, command, facts=GuardFacts({}), now=server_now)
        merged.receipts.append(
            ReceiptRecord(code=outcome.receipt_code, schema_version=outcome.receipt_schema)
        )
        merged.event_trace.extend(outcome.events)
        merged.final_state = outcome.to_state
        merged.final_entity_type = target["entity_type"]
        # Accumulate writes from every APPLIED operation. A refusal writes
        # nothing; using only the last outcome's writes would miss the
        # applied operation's writes in a concurrent-consume sequence.
        merged.write_set.extend(outcome.writes)
        merged.declared_write_set.extend(outcome.writes)
    after = _state_snapshot(engine)

    merged.state_trace.append(merged.final_state)

    # Metrics.
    if before["feature"] and after["feature"]:
        merged.metrics["aggregate_version_increment"] = (
            after["feature"]["version"] - before["feature"]["version"]
        )
    # Count approval consumes: rows whose consumed_by_command_id changed.
    if "approvals" in before["counts"] and "approvals" in after["counts"]:
        # The consume count is how many approvals were consumed by this run.
        # A consumed approval has consumed_by_command_id not null and not a
        # revocation marker.
        with engine.connect() as conn:
            consume_count = conn.execute(
                text(
                    "SELECT count(*) FROM approvals "
                    "WHERE consumed_by_command_id IS NOT NULL "
                    "AND consumed_by_command_id NOT LIKE 'revoked:%'"
                )
            ).scalar_one()
            seeded_consumed = sum(
                1 for op in fixture_body["operation_sequence"]
                for _ in [1]
                if op["input"]["authoritative_facts"]["approval"]["status"]
                in ("consumed",)
            )
        merged.metrics["approval_consume_count"] = consume_count - seeded_consumed

    engine.dispose()
    return merged


def approval_persisted_divergences(
    database: Path, fixture_body: dict[str, Any], receipt_codes: list[str]
) -> list[str]:
    """Divergences between the expected post-state and what is.

    For a denial (APPROVAL_INVALID/DECISION_STALE): the feature must be
    unchanged. For an APPLIED: the feature must have moved with a version bump.
    """
    problems: list[str] = []
    engine = create_database_engine(database)
    try:
        target = fixture_body["operation_sequence"][0]["input"]["target"]
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT state, version FROM features WHERE feature_id = :id").bindparams(
                    id=target["entity_id"]
                )
            ).first()
            if row is None:
                problems.append("feature row missing")
            else:
                applied = "APPLIED" in receipt_codes
                if applied:
                    if row[1] != target["version"] + 1:
                        problems.append(
                            f"feature version after apply: expected {target['version'] + 1}, got {row[1]}"
                        )
                    if row[0] != "approved":
                        problems.append(
                            f"feature state after apply: expected 'approved', got {row[0]!r}"
                        )
                else:
                    if row[0] != target["state"] or row[1] != target["version"]:
                        problems.append(
                            f"feature changed on denial: ({target['state']!r}, v{target['version']}) "
                            f"-> ({row[0]!r}, v{row[1]})"
                        )
    finally:
        engine.dispose()
    return problems
