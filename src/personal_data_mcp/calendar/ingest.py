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

Identity is the triple `(calendar_identifier, event_identifier, start_ts)`:
EventKit expands a recurring event into one `EKEvent` per occurrence, so the
pair alone would collapse a whole series. An occurrence that is moved becomes
a new row, and the old one is tombstoned by the normal sweep (design 7).

Two shape rules carry the v1 compatibility (design 5.2, R2-F11/R3-F14):

- A **v1 upload** carries no `timezone`, no dates and no flags. It is still
  accepted byte-for-byte as before: a timed event reads as having no zone
  (rendered Asia/Shanghai), and an all-day event's dates are **derived** in
  Asia/Shanghai — which reproduces the old display exactly, but is not
  evidence of the event's own local date, so the row is marked
  `date_anchor_unknown`.
- A **v2 upload** states the dates it read back from the device calendar, and
  says whether the attribution is confirmed. `date_anchor_unknown=true` is
  legal on v2 as well: an all-day event created by another app has no
  creation record on this device, so the phone cannot claim its local date —
  the flag follows the *evidence*, not the wire version. When a v2 all-day
  event does not state the flag at all, it defaults to **true**, because
  silence is not a claim.

One malformed batch fails whole, per the project's adversarial rule: no silent
triage. A device that sends an incoherent snapshot is told to resend a
coherent one, rather than having the server guess which half was intended.
That includes a field longer than the mirror's threshold (design 6): the
device is supposed to upload it as null *and* raise the over-limit flag, so a
too-long field is a device bug, not something to truncate.

Before any of that, a gate (design 14.2): a mirror that was **rebuilt** must
never be written by a batch captured before the rebuild, and since nothing the
server says can recall a request that is already in flight, an old window has
to have no writable entry point at all rather than be recognised and refused.
The channel therefore carries a one-way protocol floor and a per-device
rebuild flag, both read here -- inside the write unit, against the database's
current state, never from a value the caller supplied in its payload.
`calendar.policy` owns those states and the argument for why the floor never
returns to 1.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import (
    format_ledger_date,
    ledger_date,
    parse_ledger_date,
    parse_rfc3339,
)
from personal_data_mcp.calendar import policy as ingest_policy
from personal_data_mcp.calendar.directory import upsert_calendars
from personal_data_mcp.storage.models import CalendarDeviceSync, CalendarEvent


logger = logging.getLogger(__name__)


TABLE: Final[str] = "calendar_events"
MAX_BATCH_EVENTS: Final[int] = 200
MAX_WINDOW_DAYS: Final[int] = 400
MAX_CALENDARS: Final[int] = 200

#: Mirror thresholds in Unicode **code points** (design 6). Python's `len` on
#: a `str` counts code points, which is what the device counts with
#: `unicodeScalars.count`; Swift's `String.count` counts grapheme clusters and
#: would disagree on exactly the multi-byte text this domain is full of.
OVER_LIMIT_THRESHOLDS: Final[dict[str, int]] = {
    "title": 200,
    "location": 500,
    "notes": 4096,
}

_SEALED_COLUMNS: Final[tuple[str, ...]] = ("title", "notes", "location")

_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_UTC: Final[timezone] = timezone.utc


@dataclass(frozen=True)
class _Temporal:
    """What one uploaded event says about *when*, in either wire shape."""

    timezone: str | None
    start_date: str | None
    end_date: str | None
    date_anchor_unknown: bool


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _invalid(detail: str) -> AppError:
    return AppError(ErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _validate(
    arguments: dict[str, Any], now: datetime
) -> tuple[datetime, datetime, bool, datetime, list[Any]]:
    try:
        window_start = parse_rfc3339(arguments["window_start"])
        window_end = parse_rfc3339(arguments["window_end"])
        events = arguments["events"]
        window_complete = arguments["window_complete"]
        snapshot_as_of = parse_rfc3339(arguments["snapshot_as_of"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid(f"calendar ingest missing or malformed field: {exc}") from exc
    if not isinstance(events, list) or not isinstance(window_complete, bool):
        raise _invalid(
            "calendar ingest events must be a list, window_complete a bool"
        )
    if len(events) > MAX_BATCH_EVENTS:
        raise _invalid(f"calendar ingest batch exceeds {MAX_BATCH_EVENTS} events")
    # The directory rides along with the snapshot (design 2.1). It is optional
    # so that a v1 client -- which knows nothing about it -- keeps working; a
    # batch that sends it is upserted in the same transaction as its events,
    # so the two can never disagree about which world they describe.
    calendars = arguments.get("calendars")
    if calendars is None:
        calendars = []
    if not isinstance(calendars, list) or len(calendars) > MAX_CALENDARS:
        raise _invalid(
            f"calendar ingest calendars must be a list of at most {MAX_CALENDARS}"
        )
    if window_end <= window_start:
        raise _invalid("calendar ingest window_end must be after window_start")
    if (window_end - window_start).days > MAX_WINDOW_DAYS:
        raise _invalid(f"calendar ingest window exceeds {MAX_WINDOW_DAYS} days")
    if snapshot_as_of > now:
        raise _invalid("calendar ingest snapshot_as_of lies in the future")
    return window_start, window_end, window_complete, snapshot_as_of, calendars


def _require_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not _DATE_PATTERN.match(value):
        raise _invalid(f"calendar ingest {field} must be a YYYY-MM-DD date")
    return parse_ledger_date(value)


def _temporal(
    event: dict[str, Any], *, all_day: bool, start: datetime, end: datetime
) -> _Temporal:
    """Resolve the date/zone columns for one uploaded event.

    The wire shape is decided per aspect, not per batch: a v1 event simply
    has no date and no zone keys, and the absence is what makes it v1. An
    *explicit* null is a v2 statement and is judged as one -- for a timed
    event it is refused, because "I have no zone" is not something a v2 device
    can truthfully say about a timed event it just read back.
    """
    start_date = event.get("start_date")
    end_date = event.get("end_date")
    states_zone = "timezone" in event
    timezone_value = event.get("timezone")
    anchor = event.get("date_anchor_unknown")
    if anchor is not None and not isinstance(anchor, bool):
        raise _invalid("calendar ingest date_anchor_unknown must be a boolean")

    if all_day:
        if timezone_value is not None:
            # An all-day event has a date, not an instant, so it has no anchor
            # zone to record; the probe froze this (Henson 2026-09-10).
            raise _invalid("calendar ingest all-day event must not carry a timezone")
        if start_date is not None or end_date is not None:
            first = _require_date(start_date, "start_date")
            last = _require_date(end_date, "end_date")
            if last <= first:
                raise _invalid(
                    "calendar ingest all-day end_date must be after start_date"
                )
            # A v2 all-day event that says nothing about the anchor has not
            # confirmed it; silence is not a claim.
            return _Temporal(
                timezone=None,
                start_date=format_ledger_date(first),
                end_date=format_ledger_date(last),
                date_anchor_unknown=True if anchor is None else anchor,
            )
        # v1 shape: reproduce the old Shanghai rendering, and say so.
        return _Temporal(
            timezone=None,
            start_date=format_ledger_date(ledger_date(start)),
            end_date=format_ledger_date(ledger_date(end)),
            date_anchor_unknown=True,
        )

    if start_date is not None or end_date is not None:
        raise _invalid("calendar ingest timed event must not carry all-day dates")
    if anchor:
        raise _invalid(
            "calendar ingest timed event has no date attribution to be unknown"
        )
    if not states_zone:
        # v1 shape: no zone recorded, rendered as Asia/Shanghai.
        return _Temporal(
            timezone=None,
            start_date=None,
            end_date=None,
            date_anchor_unknown=False,
        )
    if timezone_value is None or not isinstance(timezone_value, str):
        raise _invalid("calendar ingest timed event must name a timezone")
    try:
        ZoneInfo(timezone_value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _invalid(
            f"calendar ingest timezone {timezone_value!r} is not a known zone"
        ) from exc
    return _Temporal(
        timezone=timezone_value,
        start_date=None,
        end_date=None,
        date_anchor_unknown=False,
    )


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
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid(f"calendar ingest event malformed: {exc}") from exc
    if (
        not isinstance(event_identifier, str)
        or not event_identifier
        or not isinstance(calendar_identifier, str)
        or not calendar_identifier
        or not isinstance(all_day, bool)
    ):
        raise _invalid("calendar ingest event identifiers or all_day malformed")
    # Overlap semantics, matching the query: a batch covers the events that
    # *intersect* its window, so a meeting that started before the window but
    # runs into it is a legitimate upload. An event disjoint from the window
    # means the device and the snapshot disagree about what was being
    # reported; that is a broken batch, not something to widen for.
    if not (start < window_end and end > window_start) or end < start:
        raise _invalid("calendar ingest event outside the declared window")

    temporal = _temporal(event, all_day=all_day, start=start, end=end)
    created_by_agent = event.get("created_by_agent", False)
    if not isinstance(created_by_agent, bool):
        raise _invalid("calendar ingest created_by_agent must be a boolean")

    row_key = str(uuid.uuid4())
    row = CalendarEvent(
        row_key=row_key,
        calendar_identifier=calendar_identifier,
        event_identifier=event_identifier,
        start_ts=_epoch(start),
        end_ts=_epoch(end),
        all_day=all_day,
        timezone=temporal.timezone,
        all_day_start_date=temporal.start_date,
        all_day_end_date=temporal.end_date,
        date_anchor_unknown=temporal.date_anchor_unknown,
        is_deleted=False,
        last_modified_ts=_epoch(last_modified),
        snapshot_ts=snapshot_ts,
        synced_at=synced_at,
        created_by_agent=created_by_agent,
        device_id=device_id,
    )
    _seal_text(row, row_key, event=event, keyring=keyring)
    return row


def _seal_text(
    row: CalendarEvent,
    row_key: str,
    *,
    event: dict[str, Any],
    keyring: KeyRing,
) -> None:
    """Seal the three personal text fields, enforcing the over-limit contract.

    The device may not send an over-long field (that is a device bug -- the
    whole batch is refused), and it may not flag a field it also sent. A
    flagged field arrives as null, so the flag is the only surviving record
    that there *was* content; without it the mirror would report "no notes"
    about an event that has them.
    """
    for column in _SEALED_COLUMNS:
        value = event.get(column)
        flag = event.get(f"{column}_over_limit", False)
        if not isinstance(flag, bool):
            raise _invalid(f"calendar ingest {column}_over_limit must be a boolean")
        # Set the flag on every path, not only when it is true: the merge
        # copies columns onto an existing row, and a column left unset reads
        # as None -- which is not a boolean the table accepts.
        setattr(row, f"{column}_over_limit", flag)
        if value is None:
            setattr(row, column, None)
            continue
        if not isinstance(value, str):
            raise _invalid(f"calendar ingest {column} must be text or null")
        threshold = OVER_LIMIT_THRESHOLDS[column]
        if len(value) > threshold:
            raise _invalid(
                f"calendar ingest {column} exceeds {threshold} characters"
            )
        if flag:
            raise _invalid(
                f"calendar ingest {column} is flagged over limit but carries text"
            )
        setattr(
            row,
            column,
            keyring.encrypt(
                value.encode("utf-8"), table=TABLE, column=column, row_id=row_key
            ),
        )


def _take_fields(existing: CalendarEvent, row: CalendarEvent) -> None:
    """Copy an uploaded row's facts onto the stored row.

    Every mutable column travels together: the identity (`start_ts`) is the
    one field that cannot change here, because it *is* part of the key the
    two rows were matched on. A merge that copied only some fields is how a
    schema addition silently stops being applied (CLAUDE.md 5.2).
    """
    existing.end_ts = row.end_ts
    existing.all_day = row.all_day
    existing.timezone = row.timezone
    existing.all_day_start_date = row.all_day_start_date
    existing.all_day_end_date = row.all_day_end_date
    existing.date_anchor_unknown = row.date_anchor_unknown
    existing.title = row.title
    existing.notes = row.notes
    existing.location = row.location
    existing.title_over_limit = row.title_over_limit
    existing.location_over_limit = row.location_over_limit
    existing.notes_over_limit = row.notes_over_limit
    existing.created_by_agent = row.created_by_agent
    existing.last_modified_ts = row.last_modified_ts
    existing.snapshot_ts = row.snapshot_ts
    existing.synced_at = row.synced_at
    existing.device_id = row.device_id
    existing.row_key = row.row_key


def _enforce_ingest_barrier(
    session,
    *,
    device_id: str,
    client_wire_version: int,
    snapshot_ts: int,
    watermark_row: CalendarDeviceSync | None,
) -> None:
    """Refuse a batch the barrier closes the channel to, and warn on old ones.

    The warning is emitted **before** the refusal, and that order is the point
    of having it: its subject is "an old window is still arriving", and a
    refused arrival is precisely the observation an operator needs -- it says
    the controlled recovery worked, while silence would leave them unable to
    tell that from the device having gone quiet. It is logged, never acted on.
    Time is not a safety predicate here and must not become one: under zero
    clock skew the tolerance in `snapshot_as_of > rebuild_instant - tolerance`
    admits a window captured seconds before the rebuild, so no tolerance both
    admits the new window and refuses the old one (design 14.2, R6-F20).

    The design asks the warning to name whether the batch carried an epoch.
    Nothing on the wire does -- there is no epoch field in any schema, which is
    the open contract gap the delivery notes record -- so what is logged is the
    carrier the design was replaced with: the client's declared protocol
    version. A v1 batch that reaches here at all is the shape the epoch would
    have separated.
    """
    rebuild_instant = (
        watermark_row.rebuild_instant if watermark_row is not None else None
    )
    if rebuild_instant is not None and snapshot_ts <= rebuild_instant:
        logger.warning(
            "calendar ingest: a batch captured at or before this device's "
            "rebuild is arriving (device=%s snapshot_ts=%s rebuild_instant=%s "
            "client_wire_version=%s)",
            device_id,
            snapshot_ts,
            rebuild_instant,
            client_wire_version,
        )
    policy = ingest_policy.read_policy(session)
    ingest_policy.check_ingest_allowed(
        policy,
        rebuild_pending=(
            watermark_row.rebuild_pending if watermark_row is not None else False
        ),
        client_wire_version=client_wire_version,
    )


def ingest_events(
    arguments: dict[str, Any],
    *,
    sessions,
    keyring: KeyRing,
    device_id: str,
    client_wire_version: int,
    now: datetime,
) -> dict[str, Any]:
    """Merge one snapshot batch. Structural idempotency: replays skip.

    `client_wire_version` is the version the **signed Host Context** reports
    (design §2.5), not anything read from `arguments`: the barrier decides from
    it whether this batch may be written at all, so a value the caller could
    set in its own payload would be the caller deciding its own admission.
    """
    window_start, window_end, window_complete, snapshot_as_of, calendars = _validate(
        arguments, now
    )
    events = arguments["events"]
    snapshot_ts = _epoch(snapshot_as_of)

    def work() -> dict[str, Any]:
        upserted = 0
        skipped = 0
        incoming_ids: set[tuple[str, str, int]] = set()

        watermark_row = session.get(CalendarDeviceSync, device_id)
        watermark_ts = watermark_row.watermark_ts if watermark_row else None

        # The barrier, read here rather than before the unit: a concurrent
        # rebuild commits outside this session, and a value read outside the
        # transaction would let a batch through on the strength of a state that
        # no longer holds (CLAUDE.md §5.2). Inside it, SQLite's snapshot rules
        # make the write fail and `run_write_transaction` re-run this unit
        # against the fresh state, where the gate then refuses.
        _enforce_ingest_barrier(
            session,
            device_id=device_id,
            client_wire_version=client_wire_version,
            snapshot_ts=snapshot_ts,
            watermark_row=watermark_row,
        )
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

        # The directory rides in the same transaction as the events it
        # describes; a create routed from this batch and the events that batch
        # reported therefore cannot disagree about the world. It is applied
        # *after* the watermark is read, because the directory is a statement
        # about a set and a late packet has no standing to make one: it may not
        # rename a calendar the newer statement renamed, nor re-add one that
        # statement dropped, nor retire one it keeps. The rule inside
        # `upsert_calendars` is the same version monotonicity the events below
        # are merged under; this gate is the batch-level half of it, which is
        # the half that also stops a late packet *introducing* a calendar.
        if not is_late_packet:
            upsert_calendars(
                session,
                device_id=device_id,
                calendars=calendars,
                now=now,
                snapshot_ts=snapshot_ts,
                # An absent `calendars` key is a client that says nothing about
                # the directory, and saying nothing may not retire anything.
                # (`_validate` folds absent and explicit null into one empty
                # list, so the raw argument is what distinguishes them.)
                states_the_whole_directory=arguments.get("calendars") is not None,
            )

        for event in events:
            if not isinstance(event, dict):
                raise _invalid("calendar ingest event must be an object")
            row = _row_from_event(
                event,
                window_start=window_start,
                window_end=window_end,
                synced_at=now,
                snapshot_ts=snapshot_ts,
                device_id=device_id,
                keyring=keyring,
            )
            key = (
                row.calendar_identifier,
                row.event_identifier,
                row.start_ts,
            )
            if key in incoming_ids:
                raise _invalid("calendar ingest batch repeats one event identity")
            incoming_ids.add(key)

            existing = (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.calendar_identifier == row.calendar_identifier,
                    CalendarEvent.event_identifier == row.event_identifier,
                    CalendarEvent.start_ts == row.start_ts,
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
                # Third review G4: a same-snapshot revive takes the uploaded
                # fields unconditionally. The sweep stamped `last_modified_ts`
                # with the snapshot instant, so the event's *real*
                # last_modified is never strictly newer — gating the field
                # copy on it kept the swept-away stale content on a row this
                # snapshot explicitly asserts. A genuinely newer snapshot
                # revive still has to beat the stored `last_modified`.
                if same_snapshot or row.last_modified_ts > existing.last_modified_ts:
                    _take_fields(existing, row)
                upserted += 1
                continue
            if snapshot_ts < existing.snapshot_ts:
                # Fourth review H2: the snapshot version is monotonic. A
                # packet whose snapshot is *older* than the row's stored
                # version has no standing to change anything — even when its
                # `last_modified` looks newer, because the two clocks are
                # unrelated fields and the schema does not order them. Without
                # this gate, a stale snapshot could overwrite revived content
                # and demote the stored version.
                skipped += 1
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
            _take_fields(existing, row)
            existing.is_deleted = False
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
                if (
                    existing.calendar_identifier,
                    existing.event_identifier,
                    existing.start_ts,
                ) in incoming_ids:
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
            # A completed window is what ends "mid-rebuild": the mirror now
            # holds a whole snapshot again, so the prompt stops being true.
            # This clears the *presentation* flag and nothing else -- the
            # protocol floor that refused the old client stays exactly where it
            # was, which is the asymmetry `calendar.policy` exists to keep.
            ingest_policy.complete_rebuild(session, device_id=device_id)

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
