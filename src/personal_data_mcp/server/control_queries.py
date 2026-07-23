"""Read-only queries behind the internal control API.

These serve the two callers described in technical design 7.6.1 and 7.7: the
Agent API crash-recovery scan, which asks for one execution's state, and the
daily review job, which asks for the successful writes committed on a given day.

Both are strictly read-only. The control plane never mutates an execution: the
scheduler "modifies or de-duplicates nothing" (component table, section 3.1),
and recovery is driven by the Finance MCP's own worker, not by the caller.

`committed_at` is the receipt's creation instant — the moment the record id came
back from Feishu. DEV-018 sets it when it records the receipt; here the query
reads it and resolves the write day in `Asia/Shanghai`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent_core.timeutil import (
    ledger_day_start_utc,
    to_rfc3339,
)
from personal_data_mcp.storage.models import ExternalReceipt, ToolExecution


def get_execution_status(
    session: Session, idempotency_key: str
) -> dict[str, Any] | None:
    """The state of one execution, or None if the key is unknown.

    Recovery (7.6.1) branches on this state, so it is reported verbatim rather
    than collapsed into a coarser status. Encrypted payloads and results are not
    returned: recovery needs the state, not the operational data.
    """
    execution = session.get(ToolExecution, idempotency_key)
    if execution is None:
        return None

    receipt = session.scalars(
        select(ExternalReceipt).where(
            ExternalReceipt.idempotency_key == idempotency_key
        )
    ).one_or_none()

    return {
        "idempotency_key": execution.idempotency_key,
        "tool": execution.tool,
        "state": execution.state,
        "state_version": execution.state_version,
        "created_at": to_rfc3339(execution.created_at),
        "updated_at": to_rfc3339(execution.updated_at),
        "submitted_at": (
            to_rfc3339(execution.submitted_at)
            if execution.submitted_at is not None
            else None
        ),
        "completed_at": (
            to_rfc3339(execution.completed_at)
            if execution.completed_at is not None
            else None
        ),
        "record_id": receipt.record_id if receipt is not None else None,
        "receipt_verified": (
            receipt is not None and receipt.verified_at is not None
        ),
    }


def successful_writes_on(session: Session, day: date) -> list[dict[str, Any]]:
    """The verified successful writes whose commit instant falls on `day`.

    A write counts only when the execution reached `succeeded` and its receipt
    was verified, so a `committed_unverified` row awaiting read-back never
    appears in a review. The day is a `Asia/Shanghai` calendar day, resolved to
    a UTC half-open interval so a write at local midnight lands on one day only.
    """
    start = ledger_day_start_utc(day)
    end = ledger_day_start_utc(date.fromordinal(day.toordinal() + 1))

    rows = session.execute(
        select(ToolExecution, ExternalReceipt)
        .join(
            ExternalReceipt,
            ExternalReceipt.idempotency_key == ToolExecution.idempotency_key,
        )
        .where(
            ToolExecution.state == "succeeded",
            ExternalReceipt.verified_at.is_not(None),
            ExternalReceipt.created_at >= start,
            ExternalReceipt.created_at < end,
        )
        .order_by(ExternalReceipt.created_at)
    ).all()

    return [
        {
            "tool": execution.tool,
            "table_kind": receipt.table_kind,
            "record_id": receipt.record_id,
            "committed_at": to_rfc3339(receipt.created_at),
        }
        for execution, receipt in rows
    ]
