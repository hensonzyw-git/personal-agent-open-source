"""Executes frozen Decision-Dock fixtures against the real projection handler.

`DAL-T-DOCK-001` proves the §3.5.1 rank pipeline: candidate decisions are
bucketed by first-match rank, filtered fail-closed on unresolved dependencies,
sorted, deduplicated by root and truncated to `maximum_items`. The projection is
then persisted as `DecisionCardProjection` rows with their `dock_rank`, plus the
operation receipt and audit.

The executor seeds the feature and the candidate decisions the fixture names,
calls the real `apply_decision_dock`, and derives the write set from the
database on either side of the call. The `ordered_decision_ids`/`ranks`/
`evictions` metrics come from the projection the handler returned — the frozen
`scenario_assertions` then judge them against the authority's independent
statement, while `dock_persisted_divergences` re-reads the projection rows from
the database to confirm the handler actually wrote what it declared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import parse_rfc3339
from personal_agent_dal.machine.dock import apply_decision_dock
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe


def _seed(engine: Any, fixture_body: dict[str, Any]) -> None:
    from tests.dal.factories import decision_row, feature_row

    op = fixture_body["operation_sequence"][0]
    target = op["input"]["target"]
    result = op["input"]["injected_results"][0]
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(
            feature_row(
                feature_id=target["entity_id"],
                version=target["version"],
                state=target["state"],
            )
        )
        for candidate in result["candidates"]:
            decision = decision_row(
                feature_id=target["entity_id"],
                decision_id=candidate["decision_id"],
            )
            decision.root_id = candidate["root_id"]
            decision.status = candidate["status"]
            decision.safety_or_irreversible = candidate["safety_or_irreversible"]
            decision.blocking_scope = candidate["blocking_scope"]
            decision.depends_on_json = canonical_json(candidate["depends_on"])
            decision.expires_at = (
                parse_rfc3339(candidate["expires_at"])
                if candidate["expires_at"] is not None
                else None
            )
            decision.created_at = parse_rfc3339(candidate["created_at"])
            decision.updated_at = decision.created_at
            session.add(decision)


def _snapshot(engine: Any) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    with engine.connect() as conn:
        for table in (
            "decision_card_projections",
            "operation_receipts",
            "audit_events",
        ):
            counts[table] = conn.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608
            ).scalar_one()
        events = list(
            conn.execute(
                text("SELECT event_type FROM operation_events ORDER BY seq")
            ).scalars()
        )
    counts["event_trace"] = events
    return counts


def _observed_writes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    writes: list[str] = []
    if after["decision_card_projections"] > before["decision_card_projections"]:
        writes.append("decision_projection")
    if after["operation_receipts"] > before["operation_receipts"]:
        writes.append("operation_receipt")
    if after["audit_events"] > before["audit_events"]:
        writes.append("audit")
    return writes


def execute_dock_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Seed, run the real projection handler, and record its trace."""
    engine = create_database_engine(database)
    db.upgrade(engine)
    _seed(engine, fixture_body)

    op = fixture_body["operation_sequence"][0]
    target = op["input"]["target"]
    server_now = parse_rfc3339(op["input"]["injected_results"][0]["server_now"])

    before = _snapshot(engine)
    projection, receipt = apply_decision_dock(engine, op, now=server_now)
    after = _snapshot(engine)
    engine.dispose()

    trace = ExecutionTrace(probe=probe)
    # Projection never moves the feature aggregate.
    trace.state_trace = [target["state"], target["state"]]
    trace.final_state = target["state"]
    trace.final_entity_type = target["entity_type"]
    trace.receipts.append(
        ReceiptRecord(code=receipt.code.value, schema_version=receipt.schema_version)
    )
    trace.event_trace = after["event_trace"][len(before["event_trace"]):]
    trace.write_set = _observed_writes(before, after)
    trace.metrics["ordered_decision_ids"] = list(projection.ordered_decision_ids)
    trace.metrics["ranks"] = dict(projection.ranks)
    trace.metrics["evictions"] = dict(projection.evictions)
    return trace


def dock_persisted_divergences(
    database: Path, trace: ExecutionTrace
) -> list[str]:
    """The projection rows must match what the handler declared.

    Re-reads `decision_card_projections` from the database and checks that every
    declared ordered decision is present exactly once with its declared rank,
    and that no undeclared row appeared.
    """
    problems: list[str] = []
    engine = create_database_engine(database)
    try:
        with engine.connect() as conn:
            rows = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        "SELECT decision_id, dock_rank "
                        "FROM decision_card_projections WHERE actionable = 1"
                    )
                )
            }
        ordered = trace.metrics["ordered_decision_ids"]
        ranks = trace.metrics["ranks"]
        expected = {decision_id: ranks[decision_id] for decision_id in ordered}
        if rows != expected:
            problems.append(
                f"projection rows {rows!r} != declared {expected!r}"
            )
    finally:
        engine.dispose()
    return problems
