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

from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import (
    ledger_day_start_utc,
    to_rfc3339,
)
from personal_data_mcp.finance.duplicate_check import (
    candidate_summary_for_check,
    pending_check_for,
)
from personal_data_mcp.storage.models import (
    CalendarDirectory,
    ExternalReceipt,
    ToolExecution,
)


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


def get_pending_duplicate_check(
    session: Session,
    idempotency_key: str,
    *,
    now: datetime,
    keyring: KeyRing | None,
) -> dict[str, Any] | None:
    """The undecided duplicate check raised for this request, or None.

    The check id, expiry and a display-only candidate summary leave. Candidate
    record ids stay sealed: the Agent needs to show what matched, but it never
    needs the source identifier. This is the Host-only side channel; the
    model-facing MCP error remains the bare `POSSIBLE_DUPLICATE`.
    """
    check = pending_check_for(
        session, idempotency_key=idempotency_key, now=now
    )
    if check is None:
        return None
    if keyring is None:
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail=(
                "a pending duplicate check exists but no data keyring is "
                "composed for its display projection"
            ),
        )
    try:
        existing_summary = candidate_summary_for_check(check, keyring)
    except Exception as exc:
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail="the pending duplicate display projection is unreadable",
        ) from exc
    return {
        "duplicate_check_id": check.check_id,
        "status": check.status,
        "created_at": to_rfc3339(check.created_at),
        "expires_at": to_rfc3339(check.expires_at),
        "existing_summary": existing_summary,
    }


def verified_receipt_for(
    session: Session, *, table_kind: str, record_id: str
) -> ExternalReceipt | None:
    """The verified receipt Finance holds for this record, or None.

    This is what keeps the current-value read from being a general ledger
    reader: the only records it can return are the ones this service wrote and
    verified, which is exactly the set a review card can contain. A record id
    from anywhere else has no receipt and is simply not found.
    """
    return session.scalars(
        select(ExternalReceipt).where(
            ExternalReceipt.record_id == record_id,
            ExternalReceipt.table_kind == table_kind,
            ExternalReceipt.verified_at.is_not(None),
        )
    ).first()


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


#: The stable outcome names of a calendar directory lookup. They are stated on
#: the wire rather than collapsed into one "no result", because they mean
#: different things to the user and only one of them is a dead end: an empty
#: directory is "your phone has not reported its calendars yet", a title miss is
#: "there is no such calendar", and a read-only match is "that one cannot be
#: written to".
CALENDAR_RESOLVED: Final[str] = "resolved"
CALENDAR_NOT_FOUND: Final[str] = "not_found"
CALENDAR_AMBIGUOUS: Final[str] = "ambiguous"
CALENDAR_READ_ONLY: Final[str] = "read_only"
CALENDAR_DIRECTORY_EMPTY: Final[str] = "directory_empty"


def resolve_calendar_target(
    session: Session, *, device_id: str, title: str
) -> dict[str, Any]:
    """Resolve one calendar *name* to one EventKit identifier (design 2.1).

    Routing is an exact-title lookup and nothing cleverer: the directory is what
    the phone last reported, and a fuzzy match would silently write to a
    calendar the user did not name. Writability is part of the match predicate
    rather than a post-filter, so a subscribed or read-only calendar that
    happens to share the name can never be selected -- it is reported as
    read-only instead, which is the reason design 2.4 keeps subscribed
    calendars in the directory at all.

    The identifier is an EventKit UUID and leaves only on the resolved path,
    where the caller seals it into a device action. Candidates carry titles and
    source names, which is all a "which one did you mean?" question needs.

    A retired row is not a candidate at all: the directory is the device's
    *whole* statement about its calendars, so a calendar absent from it is one
    the phone no longer has, and sealing its identifier would issue a write the
    phone cannot execute. Retired rows stay in the table (events mirrored from
    them are still named by them) and are filtered here, at the one place where
    a calendar is chosen.
    """
    rows = (
        session.execute(
            select(CalendarDirectory)
            .where(CalendarDirectory.device_id == device_id)
            .where(CalendarDirectory.retired_at.is_(None))
            .order_by(CalendarDirectory.calendar_identifier)
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"status": CALENDAR_DIRECTORY_EMPTY}

    named = [row for row in rows if row.title == title]
    if not named:
        return {"status": CALENDAR_NOT_FOUND}

    writable = [
        row
        for row in named
        if row.allows_content_modifications and not row.is_subscribed
    ]
    if len(writable) == 1:
        return {
            "status": CALENDAR_RESOLVED,
            "calendar_identifier": writable[0].calendar_identifier,
            "title": writable[0].title,
        }
    if len(writable) > 1:
        # The same name on two accounts. Picking either one writes to a
        # calendar the user did not choose, so both are offered back instead.
        return {
            "status": CALENDAR_AMBIGUOUS,
            "candidates": [
                {"title": row.title, "source_title": row.source_title}
                for row in writable
            ],
        }
    return {
        "status": CALENDAR_READ_ONLY,
        "candidates": [
            {"title": row.title, "source_title": row.source_title}
            for row in named
        ],
    }
