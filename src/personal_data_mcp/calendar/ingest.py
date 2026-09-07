"""Merge a device-reported calendar snapshot into the mirror.

The arbitration rule is the whole design: the device is the fact source, but
its uploads may arrive out of order (a foreground refresh racing a retry), so
every row carries the device's own `last_modified` and the merge is
monotonic —

- strictly newer `last_modified` overwrites;
- equal or older `last_modified` skips, no matter what the fields say;
- only a `window_complete` final chunk may tombstone window rows it did not
  mention, and a tombstone can only be cleared by a strictly newer copy.

The last property is what keeps a delayed chunk holding a pre-deletion copy
from resurrecting a deleted event: the tombstone was written by evidence
(newer state), and only newer evidence un-writes evidence.

One malformed batch fails whole, per the project's adversarial rule: no silent
triage. A device that sends an incoherent snapshot is told to resend a
coherent one, rather than having the server guess which half was intended.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Final

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import parse_rfc3339
from personal_data_mcp.storage.models import CalendarEvent


TABLE: Final[str] = "calendar_events"
MAX_BATCH_EVENTS: Final[int] = 200
MAX_WINDOW_DAYS: Final[int] = 400

_SEALED_COLUMNS: Final[tuple[str, ...]] = ("title", "notes", "location")


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _validate(arguments: dict[str, Any], now: datetime) -> tuple[datetime, datetime, bool]:
    try:
        window_start = parse_rfc3339(arguments["window_start"])
        window_end = parse_rfc3339(arguments["window_end"])
        events = arguments["events"]
        window_complete = arguments["window_complete"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"calendar ingest missing or malformed field: {exc}",
        ) from exc
    if not isinstance(events, list) or not isinstance(window_complete, bool):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="calendar ingest events must be a list, window_complete a bool",
        )
    if len(events) > MAX_BATCH_EVENTS:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"calendar ingest batch exceeds {MAX_BATCH_EVENTS} events",
        )
    if window_end <= window_start:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="calendar ingest window_end must be after window_start",
        )
    if (window_end - window_start).days > MAX_WINDOW_DAYS:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"calendar ingest window exceeds {MAX_WINDOW_DAYS} days",
        )
    return window_start, window_end, window_complete


def _row_from_event(
    event: dict[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
    synced_at: datetime,
    device_id: str,
    keyring: KeyRing,
) -> CalendarEvent:
    """Build one mirror row, refusing anything the window contradicts.

    A row disjoint from the declared window means the device and the snapshot
    disagree about what was being reported; that is a broken batch, not an
    event to quietly widen the window for.
    """
    try:
        event_identifier = event["event_identifier"]
        calendar_identifier = event["calendar_identifier"]
        start = parse_rfc3339(event["start"])
        end = parse_rfc3339(event["end"])
        all_day = event["all_day"]
        last_modified = parse_rfc3339(event["last_modified"])
        title = event.get("title")
        notes = event.get("notes")
        location = event.get("location")
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"calendar ingest event malformed: {exc}",
        ) from exc
    if (
        not isinstance(event_identifier, str)
        or not event_identifier
        or not isinstance(calendar_identifier, str)
        or not calendar_identifier
        or not isinstance(all_day, bool)
    ):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="calendar ingest event identifiers or all_day malformed",
        )
    # Overlap semantics, matching the query: a batch covers the events that
    # *intersect* its window, so a meeting that started before the window but
    # runs into it is a legitimate upload. An event disjoint from the window
    # means the device and the snapshot disagree about what was being
    # reported; that is a broken batch, not something to widen for.
    if not (start < window_end and end > window_start) or end < start:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="calendar ingest event outside the declared window",
        )

    row_key = str(uuid.uuid4())
    row = CalendarEvent(
        row_key=row_key,
        calendar_identifier=calendar_identifier,
        event_identifier=event_identifier,
        start_ts=_epoch(start),
        end_ts=_epoch(end),
        all_day=all_day,
        is_deleted=False,
        last_modified_ts=_epoch(last_modified),
        synced_at=synced_at,
        created_by_agent=False,
        device_id=device_id,
    )
    _seal_text(row, row_key, title=title, notes=notes, location=location, keyring=keyring)
    return row


def _seal_text(
    row: CalendarEvent,
    row_key: str,
    *,
    title: str | None,
    notes: str | None,
    location: str | None,
    keyring: KeyRing,
) -> None:
    for column, value in (
        ("title", title),
        ("notes", notes),
        ("location", location),
    ):
        if value is None:
            setattr(row, column, None)
            continue
        if not isinstance(value, str):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=f"calendar ingest {column} must be text or null",
            )
        setattr(
            row,
            column,
            keyring.encrypt(
                value.encode("utf-8"), table=TABLE, column=column, row_id=row_key
            ),
        )


def ingest_events(
    arguments: dict[str, Any],
    *,
    sessions,
    keyring: KeyRing,
    device_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Merge one snapshot batch. Structural idempotency: replays skip."""
    window_start, window_end, window_complete = _validate(arguments, now)
    events = arguments["events"]

    def work() -> dict[str, Any]:
        upserted = 0
        skipped = 0
        incoming_ids: set[tuple[str, str]] = set()

        for event in events:
            if not isinstance(event, dict):
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail="calendar ingest event must be an object",
                )
            row = _row_from_event(
                event,
                window_start=window_start,
                window_end=window_end,
                synced_at=now,
                device_id=device_id,
                keyring=keyring,
            )
            key = (row.calendar_identifier, row.event_identifier)
            if key in incoming_ids:
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail="calendar ingest batch repeats one event identity",
                )
            incoming_ids.add(key)

            existing = (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.calendar_identifier == row.calendar_identifier,
                    CalendarEvent.event_identifier == row.event_identifier,
                )
                .one_or_none()
            )
            if existing is not None and row.last_modified_ts <= existing.last_modified_ts:
                skipped += 1
                continue
            if existing is not None:
                # Monotonic merge: take the newer evidence, keep the identity
                # (and the existing tombstone) unless this copy is newer.
                existing.start_ts = row.start_ts
                existing.end_ts = row.end_ts
                existing.all_day = row.all_day
                existing.title = row.title
                existing.notes = row.notes
                existing.location = row.location
                existing.last_modified_ts = row.last_modified_ts
                existing.synced_at = row.synced_at
                existing.device_id = row.device_id
                existing.is_deleted = False
                existing.row_key = row.row_key
                upserted += 1
                continue
            session.add(row)
            upserted += 1

        marked_deleted = 0
        if window_complete:
            start_bound = _epoch(window_start)
            end_bound = _epoch(window_end)
            for existing in (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.start_ts < end_bound,
                    CalendarEvent.end_ts > start_bound,
                    CalendarEvent.is_deleted.is_(False),
                )
                .all()
            ):
                if (existing.calendar_identifier, existing.event_identifier) not in (
                    incoming_ids
                ):
                    existing.is_deleted = True
                    marked_deleted += 1

        return {
            "status": "ok",
            "upserted": upserted,
            "skipped": skipped,
            "marked_deleted": marked_deleted,
        }

    # No external call happens inside this unit, so a lost snapshot may retry.
    with sessions() as session:
        return run_write_transaction(session, work)
