"""Executes frozen decision-card fixtures against the real card validator.

`DAL-T-CARD-001` proves a device action on a stale decision card is refused
with `DECISION_STALE` and zero writes. Every frozen G1 variant diverges on one
of four dimensions — superseded, resolved, expired, or a `projection_version`
behind the server's — and the oracle expects the same refusal for each.

The executor does not reimplement the comparison. It seeds the feature row the
fixture names, then calls the real `apply_decision_action` with the card the
device submitted, the server's authoritative projection and the server clock,
and records the receipt faithfully. The comparator — not the executor — fails a
wrong receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from personal_agent_core.timeutil import parse_rfc3339
from personal_agent_dal.machine.card import load_and_apply_decision_action
from personal_agent_dal.machine.engine import RECEIPT_SCHEMAS
from personal_agent_dal.machine.transition_types import ReceiptCodes, TransitionRefused
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe


def _seed_feature(engine: Any, fixture_body: dict[str, Any]) -> None:
    from personal_agent_dal.storage.machine_models import DecisionCardProjection
    from tests.dal.factories import decision_row, feature_row

    target = fixture_body["operation_sequence"][0]["input"]["target"]
    facts = fixture_body["operation_sequence"][0]["input"]["authoritative_facts"]
    server = facts["server_projection"]
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(
            feature_row(
                feature_id=target["entity_id"],
                version=target["version"],
                state=target["state"],
            )
        )
        decision = decision_row(
            feature_id=target["entity_id"], decision_id=server["decision_id"]
        )
        decision.status = server["status"]
        decision.expires_at = parse_rfc3339(server["expires_at"])
        decision.superseded_by = server["superseded_by"]
        session.add(decision)
        session.add(
            DecisionCardProjection(
                projection_id=facts["latest_projection_id"],
                decision_id=server["decision_id"],
                decision_version=decision.decision_version,
                projection_version=server["projection_version"],
                actionable=True,
                display_state=target["state"],
                dock_rank=4,
                created_at=parse_rfc3339(facts["server_now"]),
            )
        )


def execute_card_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Seed, run the real card validator, and record the refusal and zero writes.

    All frozen G1 variants diverge, so the validator always raises. A variant
    that unexpectedly matched would fall through to ``APPLIED`` and fail the
    oracle's ``DECISION_STALE`` expectation loudly rather than being folded into
    the expected code.
    """
    engine = create_database_engine(database)
    db.upgrade(engine)
    _seed_feature(engine, fixture_body)

    op = fixture_body["operation_sequence"][0]
    step = op["input"]["action_sequence"][0]
    facts = op["input"]["authoritative_facts"]
    target = op["input"]["target"]

    trace = ExecutionTrace(probe=probe)
    trace.state_trace.append(target["state"])
    trace.final_entity_type = target["entity_type"]

    try:
        load_and_apply_decision_action(
            engine,
            card=step["card"],
            now=parse_rfc3339(facts["server_now"]),
        )
        code = ReceiptCodes.APPLIED
    except TransitionRefused as refusal:
        code = refusal.code
        trace.metrics["latest_projection"] = refusal.latest_projection

    trace.receipts.append(
        ReceiptRecord(code=code, schema_version=RECEIPT_SCHEMAS["feature"])
    )
    # A refusal leaves the feature exactly where it was and writes nothing:
    # the validator is a pure comparison, not a transition.
    trace.final_state = target["state"]
    trace.state_trace.append(target["state"])

    engine.dispose()
    return trace


def card_persisted_divergences(
    database: Path, fixture_body: dict[str, Any]
) -> list[str]:
    """A refusal must leave the database untouched: the seeded feature is the
    only row, unchanged, and no decision/projection/receipt row appeared."""
    problems: list[str] = []
    engine = create_database_engine(database)
    try:
        target = fixture_body["operation_sequence"][0]["input"]["target"]
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT state, version FROM features WHERE feature_id = :id")
                .bindparams(id=target["entity_id"])
            ).first()
            if row is None:
                problems.append("feature row missing")
            elif row[0] != target["state"] or row[1] != target["version"]:
                problems.append(
                    f"feature changed on refusal: ({target['state']!r}, "
                    f"v{target['version']}) -> ({row[0]!r}, v{row[1]})"
                )
            expected_counts = {
                "decisions": 1,
                "decision_card_projections": 1,
                "transition_receipts": 0,
                "operation_receipts": 0,
                "events": 0,
                "audit_events": 0,
                "outbox_events": 0,
            }
            for table in expected_counts:
                count = conn.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608
                ).scalar_one()
                expected = expected_counts.get(table, 0)
                if count != expected:
                    problems.append(
                        f"{table} has {count} rows after refusal; expected {expected}"
                    )
    finally:
        engine.dispose()
    return problems
