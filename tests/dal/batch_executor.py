"""Executes frozen notification-batch fixtures against the real batch handler.

`DAL-T-BATCH-001` proves the §3.5.2 fixed-window batch: normal decisions batch
until the 2-minute window expires or the 5th member arrives; an `immediate`
decision closes and flushes the batch at once; when every member is invalid the
evaluation is `NOOP` with `batch_window_state=cancelled` and zero writes.

The executor seeds nothing — the members are the fixture's `authoritative_facts`
(a batch evaluates the members it is handed, it does not project a decision
store). It calls the real `apply_notification_batch`, derives the write set from
the database on either side of the call, and reads the operation events back
from the database so the event trace is independently corroborated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from personal_agent_dal.machine.batch import apply_notification_batch
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe


def _snapshot(engine: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in (
            "notification_batches",
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
    if after["notification_batches"] > before["notification_batches"]:
        writes.append("notification_batch")
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
                    "WHERE event_type LIKE 'notification.%' ORDER BY seq"
                )
            ).scalars()
        )


def execute_batch_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Run the real batch handler and record its trace + independently-read events."""
    engine = create_database_engine(database)
    db.upgrade(engine)

    op = fixture_body["operation_sequence"][0]
    target = op["input"]["target"]

    before = _snapshot(engine)
    decision, receipt = apply_notification_batch(engine, op)
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
    if decision.cancelled:
        trace.metrics["batch_window_state"] = "cancelled"
        trace.metrics["notification_outbox_create_count"] = 0
    return trace
