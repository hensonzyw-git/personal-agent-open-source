"""Executes frozen notification-delivery fixtures against the real delivery handler.

`DAL-T-NOTIFY-001` proves the §3.7 delivery state machine. The provider seam is
injected from the fixture's frozen APNs results (`injected_results`), one status
per `deliver_attempt`; the handler drives claim → start → attempts and ends in
`delivered` or `dead_letter`. The executor derives the write set from the
database on either side of the call and reads the delivery events back from the
database so the event trace is independently corroborated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from personal_agent_dal.machine.notify import deliver_notification
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe


def _deliver_fn(injected_results: list[dict[str, Any]]):
    results = {result["attempt"]: result["status"] for result in injected_results}

    def deliver(attempt: int) -> str:
        if attempt not in results:
            raise ValueError(f"no injected APNs result for attempt {attempt}")
        return results[attempt]

    return deliver


def _snapshot(engine: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in (
            "notification_deliveries",
            "outbox_events",
            "operation_receipts",
            "audit_events",
        ):
            counts[table] = conn.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608
            ).scalar_one()
    return counts


def _observed_writes(before: dict[str, int], after: dict[str, int]) -> list[str]:
    writes: list[str] = []
    if after["notification_deliveries"] > before["notification_deliveries"]:
        writes.append("notification_delivery")
    if after["outbox_events"] > before["outbox_events"]:
        writes.append("notification_outbox")
    if after["operation_receipts"] > before["operation_receipts"]:
        writes.append("operation_receipt")
    if after["audit_events"] > before["audit_events"]:
        writes.append("audit")
    return writes


def _read_events(engine: Any) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT event_type FROM operation_events "
                    "WHERE event_type LIKE 'notification.delivery%' ORDER BY seq"
                )
            ).scalars()
        )


def execute_notify_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Run the real delivery handler and record its trace + independently-read events."""
    engine = create_database_engine(database)
    db.upgrade(engine)

    op = fixture_body["operation_sequence"][0]
    target = op["input"]["target"]
    deliver = _deliver_fn(op["input"]["injected_results"])

    before = _snapshot(engine)
    terminal_state, receipt = deliver_notification(engine, op, deliver=deliver)
    after = _snapshot(engine)
    events = _read_events(engine)
    engine.dispose()

    trace = ExecutionTrace(probe=probe)
    trace.state_trace = [target["state"], target["state"]]
    trace.final_state = target["state"]
    trace.final_entity_type = target["entity_type"]
    trace.receipts.append(
        ReceiptRecord(code=receipt.code.value, schema_version=receipt.schema_version)
    )
    trace.write_set = _observed_writes(before, after)
    trace.event_trace = events
    trace.metrics["terminal_delivery_state"] = terminal_state
    return trace
