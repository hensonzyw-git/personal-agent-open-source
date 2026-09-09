"""Merge a device-reported calendar snapshot into the mirror.

The device is the fact source, but EventKit exposes no per-event modification
time, so the **snapshot instant** (`snapshot_as_of`, identical across every
batch of one window) is the only honest version the mirror can arbitrate on.
The rules:

- A batch whose snapshot is not newer than the device's watermark is a
  **late packet**. It may re-assert rows that same-or-newer snapshots hold,
  but it may **never insert** a row the mirror has never seen and may **never
  revive** a tombstone: the device already vouched, at a newer instant, that
  the window did not contain those events. It also may never tombstone — the
  sweep is a property of a completed, current snapshot.
- Within one snapshot, rows merge monotonically on the device's own
  `last_modified` (strictly newer overwrites; equal or older skips) — but a
  row whose stored version equals this snapshot's instant is *re-asserted*:
  a straggler chunk arriving after its own snapshot's sweep revives the rows
  that snapshot holds (its testimony is this snapshot's, so it cannot lose
  to a tombstone the same sweep wrote).
- Only a `window_complete` batch whose snapshot is strictly newer than the
  watermark sweeps the window, and the sweep obeys version monotonicity: a
  row is tombstoned only when its stored version is **older** than this
  snapshot's instant. Rows a newer (possibly still incomplete) snapshot
  already upserted are not deletable by an older complete snapshot arriving
  late (second review F4). The tombstone's version is the snapshot instant,
  so only a newer snapshot's assertion can clear it.

A per-device watermark (`calendar_device_sync`) records the newest completed
snapshot and the window it covered, and is also the freshness source: a
partial upload never reads as fresh, and a window the completed snapshot did
not cover never reads as covered (review R2/R3/R10 + second review F7).

One malformed batch fails whole, per the project's adversarial rule: no silent
triage. A device that sends an incoherent snapshot is told to resend a
coherent one, rather than having the server guess which half was intended.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import select

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import parse_rfc3339
from personal_data_mcp.storage.models import CalendarDeviceSync, CalendarEvent


TABLE: Final[str] = "calendar_events"
MAX_BATCH_EVENTS: Final[int] = 200
MAX_WINDOW_DAYS: Final[int] = 400

_SEALED_COLUMNS: Final[tuple[str, ...]] = ("title", "notes", "location")

_UTC: Final[timezone] = timezone.utc


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _validate(
    arguments: dict[str, Any], now: datetime
) -> tuple[datetime, datetime, bool, datetime]:
    try:
        window_start = parse_rfc3339(arguments["window_start"])
        window_end = parse_rfc3339(arguments["window_end"])
        events = arguments["events"]
        window_complete = arguments["window_complete"]
        snapshot_as_of = parse_rfc3339(arguments["snapshot_as_of"])
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
    if snapshot_as_of > now:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="calendar ingest snapshot_as_of lies in the future",
        )
    return window_start, window_end, window_complete, snapshot_as_of


def _row_from_event(
    event: dict[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
    synced_at: datetime,
    snapshot_ts: int,
    device_id: str,
    keyring: KeyRing,
) -> CalendarEvent:
    """Build one mirror row, refusing anything the window contradicts.

    A row disjoint from the declared window means the device and the snapshot
    disagree about what was being reported; that is a broken batch, not an
    event to quietly widen the window for. The row's version is the snapshot
    instant (`snapshot_ts`) — the only honest timestamp EventKit lets the
    device vouch for; the per-event `last_modified` the schema still carries
    is merged between snapshots but never arbitrates sweeps.
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
        snapshot_ts=snapshot_ts,
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
    window_start, window_end, window_complete, snapshot_as_of = _validate(
        arguments, now
    )
    events = arguments["events"]
    snapshot_ts = _epoch(snapshot_as_of)

    def work() -> dict[str, Any]:
        upserted = 0
        skipped = 0
        incoming_ids: set[tuple[str, str]] = set()

        watermark_row = (
            session.get(CalendarDeviceSync, device_id)
        )
        watermark_ts = watermark_row.watermark_ts if watermark_row else None
        #: A snapshot **strictly older** than the watermark is a *late
        #: packet*: it has no standing to speak for events the completed
        #: snapshots never saw — so it may not insert an unknown row and may
        #: not revive a tombstone, and it has no standing to tombstone either.
        #:
        #: A snapshot **equal** to the watermark is a straggler of the very
        #: snapshot that set the watermark (third review G3): its membership
        #: is testimony of the snapshot the watermark itself names, so it may
        #: insert and revive — the watermark's completion said "this window is
        #: all of this snapshot", and a chunk of that same snapshot arriving
        #: late is how the whole window actually arrives. It still may not
        #: sweep: the window is already complete.
        is_late_packet = watermark_ts is not None and snapshot_ts < watermark_ts
        may_sweep = window_complete and (
            watermark_ts is None or snapshot_ts > watermark_ts
        )

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
                snapshot_ts=snapshot_ts,
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
            if existing is None:
                # Second review F3a: a late packet may not introduce a row the
                # mirror has never seen. The watermark is the device's own
                # testimony that a *newer* completed snapshot existed; letting
                # an older packet invent rows behind it is exactly how a
                # deleted-then-late-arriving event comes back.
                if is_late_packet:
                    skipped += 1
                    continue
                session.add(row)
                upserted += 1
                continue
            if existing.is_deleted:
                # A tombstone clears only through evidence at least as new as
                # the tombstone's version (which a sweep set to *its* snapshot
                # instant, and which only advances): the same snapshot's own
                # straggler chunk, or a genuinely newer snapshot asserting the
                # event. A late packet (older than the watermark, hence older
                # than the tombstone's version) has no standing (F3b).
                tombstone_version = existing.snapshot_ts
                same_snapshot = existing.snapshot_ts == snapshot_ts
                clears = (
                    row.last_modified_ts > existing.last_modified_ts
                    and snapshot_ts >= tombstone_version
                ) or same_snapshot
                if not clears:
                    skipped += 1
                    continue
                existing.is_deleted = False
                existing.synced_at = row.synced_at
                existing.device_id = row.device_id
                if same_snapshot:
                    # Third review G4: a same-snapshot revive takes the
                    # uploaded fields unconditionally. The sweep stamped
                    # `last_modified_ts` with the snapshot instant, so the
                    # event's *real* last_modified is never strictly newer —
                    # gating the field copy on it kept the swept-away stale
                    # content on a row this snapshot explicitly asserts.
                    existing.start_ts = row.start_ts
                    existing.end_ts = row.end_ts
                    existing.all_day = row.all_day
                    existing.title = row.title
                    existing.notes = row.notes
                    existing.location = row.location
                    existing.last_modified_ts = row.last_modified_ts
                    existing.snapshot_ts = snapshot_ts
                    existing.row_key = row.row_key
                elif row.last_modified_ts > existing.last_modified_ts:
                    existing.start_ts = row.start_ts
                    existing.end_ts = row.end_ts
                    existing.all_day = row.all_day
                    existing.title = row.title
                    existing.notes = row.notes
                    existing.location = row.location
                    existing.last_modified_ts = row.last_modified_ts
                    existing.snapshot_ts = snapshot_ts
                    existing.row_key = row.row_key
                upserted += 1
                continue
            if row.last_modified_ts <= existing.last_modified_ts:
                # Equal or older `last_modified` carries no new field evidence
                # — with one exception: a row whose stored snapshot version is
                # *this same* snapshot has been re-asserted by it (a straggler
                # chunk trailing its own sweep). That assertion is a no-op on a
                # live row but is the revive path for a tombstone handled above.
                skipped += 1
                continue
            # Monotonic merge: strictly newer `last_modified` takes the fields.
            existing.start_ts = row.start_ts
            existing.end_ts = row.end_ts
            existing.all_day = row.all_day
            existing.title = row.title
            existing.notes = row.notes
            existing.location = row.location
            existing.last_modified_ts = row.last_modified_ts
            existing.snapshot_ts = snapshot_ts
            existing.synced_at = row.synced_at
            existing.device_id = row.device_id
            existing.is_deleted = False
            existing.row_key = row.row_key
            upserted += 1

        marked_deleted = 0
        if may_sweep:
            start_bound = _epoch(window_start)
            end_bound = _epoch(window_end)
            for existing in (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.start_ts < end_bound,
                    CalendarEvent.end_ts > start_bound,
                )
                .all()
            ):
                if (existing.calendar_identifier, existing.event_identifier) in (
                    incoming_ids
                ):
                    continue
                # A row whose snapshot version is this very snapshot was
                # re-asserted by it (a straggler of the same snapshot); the
                # sweep must not delete what its own snapshot holds.
                if existing.snapshot_ts == snapshot_ts:
                    continue
                # Second review F4: deletion obeys version monotonicity. A row
                # whose version is *newer* than this sweeping snapshot — an
                # incomplete newer snapshot upserted it, and this older
                # complete one arrived late — is not this snapshot's to
                # delete. Only rows older than this snapshot are absent from
                # *this* observation.
                if existing.snapshot_ts > snapshot_ts:
                    continue
                if not existing.is_deleted:
                    # The tombstone is versioned like any other row state: its
                    # version is *this deleting* snapshot's instant, so a later
                    # chunk of the same snapshot can recognise its own sweep
                    # and revoke it, while a straggler from an *older* snapshot
                    # — whose version no longer matches anything — can neither
                    # match the reassertion rule nor clear the tombstone.
                    existing.is_deleted = True
                    existing.last_modified_ts = snapshot_ts
                    existing.snapshot_ts = snapshot_ts
                    marked_deleted += 1
                else:
                    # Already tombstoned, but an older version. Re-confirming
                    # absence at a newer instant is still testimony (F3b): the
                    # tombstone's version advances, so a straggler stamped
                    # between the two can no longer clear it.
                    existing.last_modified_ts = snapshot_ts
                    existing.snapshot_ts = snapshot_ts

        if window_complete and (watermark_ts is None or snapshot_ts > watermark_ts):
            if watermark_row is None:
                session.add(
                    CalendarDeviceSync(
                        device_id=device_id,
                        watermark_ts=snapshot_ts,
                        window_start_ts=_epoch(window_start),
                        window_end_ts=_epoch(window_end),
                        updated_at=now,
                    )
                )
            else:
                watermark_row.watermark_ts = snapshot_ts
                watermark_row.window_start_ts = _epoch(window_start)
                watermark_row.window_end_ts = _epoch(window_end)
                watermark_row.updated_at = now

        return {
            "status": "ok",
            "upserted": upserted,
            "skipped": skipped,
            "marked_deleted": marked_deleted,
        }

    # No external call happens inside this unit, so a lost snapshot may retry.
    with sessions() as session:
        return run_write_transaction(session, work)


def device_watermark(sessions, *, device_id: str) -> datetime | None:
    """The instant of this device's newest completed snapshot, or None."""
    with sessions() as session:
        row = session.get(CalendarDeviceSync, device_id)
        if row is None:
            return None
        return datetime.fromtimestamp(row.watermark_ts, tz=_UTC)
