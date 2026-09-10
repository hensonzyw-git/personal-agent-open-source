"""The calendar mirror's v2 shape: identity, dates, zones, limits, directory.

Three defects drove this shape (Calendar design v1.0, reviews R1-R7):

- one recurring series collapsed onto one row, because the identity was the
  `(calendar_identifier, event_identifier)` pair and EventKit gives every
  occurrence the same identifier;
- an all-day event's *date* was reconstructed from its epoch instant, so a
  Tokyo all-day event rendered as the previous Shanghai day;
- a long note was simply dropped, leaving a summary to report "no notes"
  about an event that has them.

Each rule below is a failing case first: the shape is only implemented when
the wrong behaviour it forbids is itself a test. The v1 shape stays accepted
throughout -- the phone updates on its own schedule, and a mirror that
refused its own history would be a mirror that stopped being current.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select

from personal_agent_core.crypto import KeyEntry, KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import load_manifest
from personal_agent_core.timeutil import to_rfc3339
from personal_data_mcp.calendar.ingest import ingest_events
from personal_data_mcp.calendar.query_events import query_events
from personal_data_mcp.server.control_queries import resolve_calendar_target
from personal_data_mcp.storage import db
from personal_data_mcp.storage.engine import (
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import CalendarDirectory, CalendarEvent


NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
WINDOW_START = "2026-10-01T00:00:00+08:00"
WINDOW_END = "2026-10-08T00:00:00+08:00"
SECRET = b"calendar-cursor-secret" * 2
KEY = bytes(range(32))

#: A Tokyo all-day event: the instant EventKit reports for 10-01 local, and
#: the instant that renders as 09-30 in Shanghai. The whole point of the date
#: columns is that these two disagree.
TOKYO_ALL_DAY_START = "2026-10-01T00:00:00+09:00"
TOKYO_ALL_DAY_END = "2026-10-04T00:00:00+09:00"


def _keyring() -> KeyRing:
    return KeyRing(
        [KeyEntry(kid="test", key=KEY, state="active")], service="personal_data_mcp"
    )


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "calendar.sqlite")
    db.upgrade(engine, "head")
    factory = session_factory(engine)
    _SNAPSHOT_TICK[0] = 0
    yield factory
    engine.dispose()


_SNAPSHOT_TICK: list[int] = [0]


def _ingest(
    sessions,
    events,
    *,
    window_complete=True,
    device_id="dev-1",
    as_of=None,
    calendars=None,
    window=(WINDOW_START, WINDOW_END),
):
    """Each call is a new snapshot: the default instant advances per call, so
    two complete batches never share one version by accident."""
    if as_of is None:
        _SNAPSHOT_TICK[0] += 1
        as_of = NOW - timedelta(hours=2) + timedelta(minutes=_SNAPSHOT_TICK[0])
    body = {
        "window_start": window[0],
        "window_end": window[1],
        "events": events,
        "window_complete": window_complete,
        "snapshot_as_of": to_rfc3339(as_of),
    }
    if calendars is not None:
        body["calendars"] = calendars
    return ingest_events(
        body,
        sessions=sessions,
        keyring=_keyring(),
        device_id=device_id,
        now=NOW,
    )


def _query(sessions, *, window=(WINDOW_START, WINDOW_END)):
    return query_events(
        {"start": window[0], "end": window[1]},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )


def _timed(event_id="ev-1", **overrides):
    """A v2 timed event: it states its own zone."""
    event = {
        "event_identifier": event_id,
        "calendar_identifier": "cal-1",
        "title": "网球",
        "start": "2026-10-02T15:00:00+08:00",
        "end": "2026-10-02T16:30:00+08:00",
        "all_day": False,
        "last_modified": "2026-10-05T20:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "created_by_agent": False,
    }
    event.update(overrides)
    return {key: value for key, value in event.items() if value is not _ABSENT}


def _all_day(event_id="ev-1", **overrides):
    """A v2 all-day event: dates, never a zone."""
    event = {
        "event_identifier": event_id,
        "calendar_identifier": "cal-1",
        "title": "东京出差",
        "start": TOKYO_ALL_DAY_START,
        "end": TOKYO_ALL_DAY_END,
        "all_day": True,
        "last_modified": "2026-10-05T20:00:00+08:00",
        "timezone": None,
        "start_date": "2026-10-01",
        "end_date": "2026-10-04",
        "date_anchor_unknown": False,
        "created_by_agent": False,
    }
    event.update(overrides)
    return {key: value for key, value in event.items() if value is not _ABSENT}


def _v1(event_id="ev-1", **overrides):
    """The pre-0.3.0 upload shape: no zone, no dates, no flags."""
    event = {
        "event_identifier": event_id,
        "calendar_identifier": "cal-1",
        "title": "网球",
        "start": "2026-10-02T15:00:00+08:00",
        "end": "2026-10-02T16:30:00+08:00",
        "all_day": False,
        "last_modified": "2026-10-05T20:00:00+08:00",
    }
    event.update(overrides)
    return {key: value for key, value in event.items() if value is not _ABSENT}


class _Absent:
    """Marker: drop the key entirely, which is not the same as null."""


_ABSENT = _Absent()


def _row(sessions, event_identifier="ev-1", start_ts=None) -> CalendarEvent:
    with sessions() as session:
        stmt = select(CalendarEvent).where(
            CalendarEvent.event_identifier == event_identifier
        )
        rows = session.execute(stmt).scalars().all()
    if start_ts is not None:
        return next(row for row in rows if row.start_ts == start_ts)
    return rows[0]


# --- identity: a recurring series is many rows ------------------------------


def test_three_occurrences_of_one_series_coexist(sessions) -> None:
    """EventKit gives every occurrence the same identifier; the pair-based key
    made them one row, so a weekly event showed up once."""
    result = _ingest(
        sessions,
        [
            _timed("series-1", start="2026-10-02T15:00:00+08:00", end="2026-10-02T16:00:00+08:00"),
            _timed("series-1", start="2026-10-03T15:00:00+08:00", end="2026-10-03T16:00:00+08:00"),
            _timed("series-1", start="2026-10-04T15:00:00+08:00", end="2026-10-04T16:00:00+08:00"),
        ],
    )
    assert result["upserted"] == 3

    with sessions() as session:
        rows = (
            session.execute(
                select(CalendarEvent).where(
                    CalendarEvent.event_identifier == "series-1"
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 3
    assert _query(sessions)["record_count"] == 3


def test_moving_one_occurrence_leaves_the_series_alone(sessions) -> None:
    """A rescheduled occurrence is a new triple. The old one is swept like any
    other row the newest snapshot no longer holds -- and its neighbours, which
    the same snapshot does hold, are untouched."""
    _ingest(
        sessions,
        [
            _timed("series-1", start="2026-10-02T15:00:00+08:00", end="2026-10-02T16:00:00+08:00"),
            _timed("series-1", start="2026-10-03T15:00:00+08:00", end="2026-10-03T16:00:00+08:00"),
        ],
    )
    _ingest(
        sessions,
        [
            # 10-02 moved to 21:00; 10-03 unchanged.
            _timed("series-1", start="2026-10-02T21:00:00+08:00", end="2026-10-02T22:00:00+08:00"),
            _timed("series-1", start="2026-10-03T15:00:00+08:00", end="2026-10-03T16:00:00+08:00"),
        ],
    )

    with sessions() as session:
        rows = (
            session.execute(
                select(CalendarEvent).where(
                    CalendarEvent.event_identifier == "series-1"
                )
            )
            .scalars()
            .all()
        )
    by_start = {row.start_ts: row for row in rows}
    moved = datetime(2026, 10, 2, 15, 0, tzinfo=timezone(timedelta(hours=8)))
    kept = datetime(2026, 10, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))
    assert by_start[int(moved.timestamp())].is_deleted is True
    assert by_start[int(kept.timestamp())].is_deleted is False
    assert _query(sessions)["record_count"] == 2


# --- dates and zones --------------------------------------------------------


def test_an_all_day_event_renders_from_its_dates_not_its_epoch(sessions) -> None:
    """The R1-F2 defect: 2026-10-01 00:00 in Tokyo is 2026-09-30 23:00 in
    Shanghai, so converting the instant reports the wrong day."""
    _ingest(sessions, [_all_day()])

    event = _query(sessions)["events"][0]
    assert event["start_date"] == "2026-10-01"
    assert event["end_date"] == "2026-10-04"
    assert event["timezone"] is None
    assert event["date_anchor_unknown"] is False
    # The instant is still carried -- the window filter is built on it -- and
    # it does fall on 09-30 in Shanghai, which is exactly why no renderer may
    # use it for an all-day event.
    assert event["start"].startswith("2026-09-30T15:00:00")


def test_a_timed_event_keeps_its_own_zone(sessions) -> None:
    _ingest(sessions, [_timed(timezone="Asia/Tokyo")])

    event = _query(sessions)["events"][0]
    assert event["timezone"] == "Asia/Tokyo"
    assert event["start_date"] is None and event["end_date"] is None
    assert event["date_anchor_unknown"] is False


def test_a_v1_timed_event_reads_as_having_no_zone(sessions) -> None:
    """Byte-for-byte the v1 behaviour: no zone recorded, rendered Shanghai."""
    _ingest(sessions, [_v1()])

    event = _query(sessions)["events"][0]
    assert event["timezone"] is None
    assert event["date_anchor_unknown"] is False


def test_a_v1_all_day_event_keeps_its_days_and_admits_they_are_derived(
    sessions,
) -> None:
    """The v1 upload carried no date at all, so the mirror derives one in
    Asia/Shanghai -- the old display, reproduced exactly (09-30, not 10-01,
    for a Tokyo event) -- and marks the row so nothing presents that
    derivation as the event's own local date."""
    _ingest(
        sessions,
        [
            _v1(
                "all-day-1",
                all_day=True,
                start=TOKYO_ALL_DAY_START,
                end=TOKYO_ALL_DAY_END,
            )
        ],
    )

    row = _row(sessions, "all-day-1")
    assert (row.all_day_start_date, row.all_day_end_date) == (
        "2026-09-30",
        "2026-10-03",
    )
    assert row.date_anchor_unknown is True
    assert _query(sessions)["events"][0]["date_anchor_unknown"] is True


def test_an_all_day_event_that_is_silent_about_its_anchor_reads_unknown(
    sessions,
) -> None:
    """The flag follows the evidence, not the wire version: a v2 device that
    says nothing about an all-day event's attribution has not confirmed it."""
    _ingest(
        sessions,
        [_all_day("external-1", date_anchor_unknown=_ABSENT)],
    )

    assert _row(sessions, "external-1").date_anchor_unknown is True


def test_a_v2_all_day_event_may_confirm_its_anchor(sessions) -> None:
    """A self-created all-day event: the device knows the dates came from the
    action it performed, so the row is confirmed rather than merely derived."""
    _ingest(sessions, [_all_day("mine-1", date_anchor_unknown=False)])

    assert _row(sessions, "mine-1").date_anchor_unknown is False


# --- refusals: incoherent shapes are never resolved by guessing --------------


def _refused(sessions, events, **kwargs) -> AppError:
    with pytest.raises(AppError) as caught:
        _ingest(sessions, events, **kwargs)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    return caught.value


def test_a_timed_event_must_name_a_zone(sessions) -> None:
    """`timezone: null` on a timed event is a v2 device claiming it has no
    zone, which it cannot truthfully say about an event it just read back."""
    _refused(sessions, [_timed(timezone=None)])


def test_a_zone_that_does_not_exist_is_refused(sessions) -> None:
    _refused(sessions, [_timed(timezone="Mars/Olympus")])


def test_an_all_day_event_must_not_carry_a_zone(sessions) -> None:
    """The probe froze this: an all-day event has a date, not an instant."""
    _refused(sessions, [_all_day(timezone="Asia/Tokyo")])


def test_an_all_day_event_needs_an_exclusive_end_after_its_start(sessions) -> None:
    _refused(sessions, [_all_day(start_date="2026-10-04", end_date="2026-10-01")])
    _refused(sessions, [_all_day(start_date="2026-10-01", end_date="2026-10-01")])


def test_an_all_day_date_must_be_a_plain_date(sessions) -> None:
    _refused(sessions, [_all_day(start_date="20261001")])
    _refused(sessions, [_all_day(end_date="2026-10-04T00:00:00+09:00")])


def test_a_timed_event_must_not_carry_all_day_dates(sessions) -> None:
    _refused(sessions, [_timed(start_date="2026-10-01")])


def test_a_timed_event_cannot_claim_an_unknown_date_anchor(sessions) -> None:
    _refused(sessions, [_timed(date_anchor_unknown=True)])


def test_the_whole_batch_is_refused_and_nothing_is_written(sessions) -> None:
    """§5.1: no silent triage. A batch with one incoherent event is refused
    entire -- the coherent half is not quietly kept."""
    _refused(sessions, [_timed("ev-1"), _all_day("ev-2", timezone="Asia/Tokyo")])

    with sessions() as session:
        assert session.execute(select(CalendarEvent)).scalars().all() == []


# --- long text ---------------------------------------------------------------


def test_a_field_longer_than_the_threshold_refuses_the_batch(sessions) -> None:
    """A device that sends an over-long field instead of nulling it and
    raising the flag has a bug; the server does not truncate for it."""
    _refused(sessions, [_timed(notes="长" * 4097)])
    _refused(sessions, [_timed(title="长" * 201)])
    _refused(sessions, [_timed(location="长" * 501)])


def test_a_field_exactly_at_the_threshold_is_accepted(sessions) -> None:
    """The boundary is `>`, not `>=`: a complete 200-character title is a
    legitimate upload, and treating it as over-limit would lose real content."""
    _ingest(sessions, [_timed(title="长" * 200, notes="长" * 4096)])

    event = _query(sessions)["events"][0]
    assert len(event["title"]) == 200
    assert event["title_over_limit"] is False
    assert len(event["notes"]) == 4096
    assert event["notes_over_limit"] is False


def test_an_over_limit_field_arrives_null_with_its_flag(sessions) -> None:
    """The dropped field and the absent field must not look alike: without the
    flag a summary reports "no notes" about an event that has them."""
    _ingest(
        sessions,
        [_timed(notes=None, notes_over_limit=True, title="团建", location=None)],
    )

    row = _row(sessions)
    assert row.notes is None and row.notes_over_limit is True
    assert row.title_over_limit is False and row.location_over_limit is False

    event = _query(sessions)["events"][0]
    assert event["notes"] is None
    assert event["notes_over_limit"] is True
    assert event["title"] == "团建"


def test_a_flagged_field_that_still_carries_text_is_refused(sessions) -> None:
    _refused(sessions, [_timed(notes="还有内容", notes_over_limit=True)])


def test_a_flag_that_is_not_a_boolean_is_refused(sessions) -> None:
    _refused(sessions, [_timed(notes_over_limit="yes")])


# --- created_by_agent -------------------------------------------------------


def test_an_uploaded_created_by_agent_is_kept(sessions) -> None:
    """Ingest used to hard-code False, which made the query's own
    `created_by_agent` field incapable of ever being true."""
    _ingest(sessions, [_timed("mine-1", created_by_agent=True), _timed("theirs-1")])

    assert _row(sessions, "mine-1").created_by_agent is True
    assert _row(sessions, "theirs-1").created_by_agent is False


# --- the calendar directory --------------------------------------------------


def _directory_entry(identifier="cal-1", title="演出&活动", **overrides):
    entry = {
        "calendar_identifier": identifier,
        "title": title,
        "source_title": "iCloud",
        "allows_content_modifications": True,
        "is_subscribed": False,
    }
    entry.update(overrides)
    return entry


def test_a_directory_rides_along_with_its_events(sessions) -> None:
    _ingest(
        sessions,
        [_timed()],
        calendars=[_directory_entry(), _directory_entry("cal-2", "日常安排")],
    )

    with sessions() as session:
        rows = (
            session.execute(select(CalendarDirectory)).scalars().all()
        )
    assert {(row.device_id, row.calendar_identifier) for row in rows} == {
        ("dev-1", "cal-1"),
        ("dev-1", "cal-2"),
    }


def test_a_repeated_directory_entry_is_the_same_row_updated(sessions) -> None:
    """The phone uploads its whole directory every batch; a rename arrives as
    the same identifier with a new name."""
    _ingest(sessions, [_timed()], calendars=[_directory_entry(title="演出&活动")])
    _ingest(sessions, [_timed()], calendars=[_directory_entry(title="演出")])

    with sessions() as session:
        rows = session.execute(select(CalendarDirectory)).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "演出"


def test_one_batch_may_not_name_a_calendar_twice(sessions) -> None:
    _refused(
        sessions,
        [_timed()],
        calendars=[_directory_entry(), _directory_entry(title="另一个名字")],
    )


def test_a_directory_entry_must_state_writability(sessions) -> None:
    """The routing rule refuses a read-only or subscribed calendar, so an
    entry that does not say which it is cannot be routed to at all."""
    entry = _directory_entry()
    del entry["allows_content_modifications"]
    _refused(sessions, [_timed()], calendars=[entry])


def test_a_v1_batch_leaves_the_directory_alone(sessions) -> None:
    """A batch without the directory says nothing about it: absence is not a
    statement that the phone has no calendars."""
    _ingest(sessions, [_timed()], calendars=[_directory_entry()])
    _ingest(sessions, [_v1("ev-2")])

    with sessions() as session:
        rows = session.execute(select(CalendarDirectory)).scalars().all()
    assert len(rows) == 1


# --- the directory is one whole statement, not an accumulating one -----------
#
# The phone uploads its *entire* directory with every batch (design 2.1), so a
# calendar the newest statement does not name is one the device no longer has.
# Keeping it is not a harmless leftover: the row still answers the create
# routing lookup. A re-installed phone hands every calendar a fresh EventKit
# identifier, so the old and the new row carry the same title -- and a name
# that is unique on the phone resolves to 「两个以上的日历都叫…」 and cannot be
# written to at all. That is the reported defect (2026-09-10 review), and it is
# why a whole-directory statement has to be able to *remove*.


def _directory_rows(sessions) -> dict[str, CalendarDirectory]:
    with sessions() as session:
        return {
            row.calendar_identifier: row
            for row in session.execute(select(CalendarDirectory)).scalars().all()
        }


def _resolved(sessions, title: str, *, device_id: str = "dev-1"):
    with sessions() as session:
        return resolve_calendar_target(session, device_id=device_id, title=title)


def test_a_whole_directory_statement_retires_what_it_stops_naming(sessions) -> None:
    """{A, B} then {A}: B stops being selectable, and stays on record.

    The row is kept rather than deleted because events already mirrored from
    that calendar still point at its identifier, and the display read names
    them by it. Retiring is what removes it from *choice*; it is not a claim
    that it never existed.
    """
    _ingest(
        sessions,
        [_timed()],
        calendars=[
            _directory_entry("cal-1", "日常安排"),
            _directory_entry("cal-2", "出游计划"),
        ],
    )
    _ingest(sessions, [_timed()], calendars=[_directory_entry("cal-1", "日常安排")])

    assert _resolved(sessions, "出游计划")["status"] == "not_found"
    assert _resolved(sessions, "日常安排")["status"] == "resolved"

    rows = _directory_rows(sessions)
    assert set(rows) == {"cal-1", "cal-2"}
    assert rows["cal-2"].retired_at is not None
    assert rows["cal-1"].retired_at is None


def test_a_reinstalled_phones_old_calendar_does_not_make_the_name_ambiguous(
    sessions,
) -> None:
    """The defect's actual shape, end to end: a name that is unique on the
    phone must resolve, even though the mirror has seen another identifier
    under that title."""
    _ingest(sessions, [_timed()], calendars=[_directory_entry("old-uuid", "日常安排")])
    _ingest(sessions, [_timed()], calendars=[_directory_entry("new-uuid", "日常安排")])

    resolution = _resolved(sessions, "日常安排")
    assert resolution == {
        "status": "resolved",
        "calendar_identifier": "new-uuid",
        "title": "日常安排",
    }


def test_a_late_packet_may_not_rename_readd_or_retire_a_calendar(sessions) -> None:
    """A batch whose snapshot is older than the watermark has no standing to
    speak about the directory either (§5.1: the device is the fact source, and
    an older packet is not newer testimony). It may not rename a calendar the
    newer statement renamed, may not re-add one it retired, and may not retire
    one it keeps."""
    first = NOW - timedelta(hours=2)
    newer = first + timedelta(minutes=30)
    _ingest(
        sessions,
        [_timed()],
        as_of=newer,
        calendars=[_directory_entry("cal-1", "演出")],
    )
    # A packet from the *older* snapshot, carrying the directory as it was:
    # cal-1 under its old name, plus a calendar the newer one dropped.
    _ingest(
        sessions,
        [_timed()],
        as_of=first,
        calendars=[
            _directory_entry("cal-1", "演出&活动"),
            _directory_entry("cal-2", "出游计划"),
        ],
    )

    rows = _directory_rows(sessions)
    assert set(rows) == {"cal-1"}
    assert rows["cal-1"].title == "演出"
    assert rows["cal-1"].retired_at is None


def test_a_calendar_named_again_comes_back(sessions) -> None:
    """Retirement is not a tombstone: the phone re-adding a calendar is the
    same testimony as any other, and the row returns to the directory. A
    transient empty read on the phone must not be permanent."""
    _ingest(sessions, [_timed()], calendars=[_directory_entry("cal-1", "日常安排")])
    _ingest(sessions, [_timed()], calendars=[])
    assert _resolved(sessions, "日常安排")["status"] == "directory_empty"

    _ingest(sessions, [_timed()], calendars=[_directory_entry("cal-1", "日常安排")])
    rows = _directory_rows(sessions)
    assert rows["cal-1"].retired_at is None
    assert _resolved(sessions, "日常安排")["status"] == "resolved"


def test_re_uploading_one_statement_twice_changes_nothing(sessions) -> None:
    """Idempotence, end to end: the same directory in two batches leaves one
    row per calendar, neither retired."""
    entry = _directory_entry("cal-1", "日常安排")
    _ingest(sessions, [_timed()], calendars=[entry])
    _ingest(sessions, [_timed()], calendars=[entry])

    rows = _directory_rows(sessions)
    assert set(rows) == {"cal-1"}
    assert rows["cal-1"].retired_at is None
    assert rows["cal-1"].title == "日常安排"


def test_a_retired_calendar_still_names_the_events_it_already_mirrored(
    sessions,
) -> None:
    """Retirement removes a calendar from *choice*, not from the record. An
    event mirrored while it was live keeps the name its owner knows it by --
    the mirror is a history, and a list card that forgot where an event lives
    the moment the calendar was deleted would be the worse answer."""
    _ingest(
        sessions,
        [_timed()],
        calendars=[
            _directory_entry("cal-1", "日常安排"),
            _directory_entry("cal-2", "出游计划"),
        ],
    )
    _ingest(
        sessions,
        [_timed("ev-2", calendar_identifier="cal-2")],
        calendars=[_directory_entry("cal-1", "日常安排")],
    )

    page = _query(sessions)
    by_id = {item["event_identifier"]: item for item in page["events"]}
    assert by_id["ev-2"]["calendar_title"] == "出游计划"
    assert _resolved(sessions, "出游计划")["status"] == "not_found"


# --- the contract the server actually ships ----------------------------------


def test_the_query_output_satisfies_its_own_ir_contract(sessions) -> None:
    """The renderer and the contract are checked against each other, with the
    same validator the bridge uses, on a page holding one of every shape."""
    _ingest(
        sessions,
        [
            _timed("timed-1", timezone="Asia/Tokyo"),
            _v1("timed-2"),
            _all_day("all-day-1"),
            _all_day("all-day-2", date_anchor_unknown=_ABSENT),
            _timed("timed-3", notes=None, notes_over_limit=True),
        ],
    )

    result = _query(sessions)
    assert result["record_count"] == 5

    contract = next(
        tool
        for tool in load_manifest()["tools"]
        if tool["name"] == "calendar.query_events"
    )
    Draft202012Validator(
        contract["output_schema"], format_checker=FormatChecker()
    ).validate(json.loads(json.dumps(result)))


def test_the_ingest_contract_accepts_both_shapes(sessions) -> None:
    """Both upload shapes must validate against the shipped input schema --
    the route validates the body before the core ever sees it."""
    contract = next(
        tool
        for tool in load_manifest()["tools"]
        if tool["name"] == "calendar.ingest_events"
    )
    validator = Draft202012Validator(
        contract["model_input_schema"], format_checker=FormatChecker()
    )
    for events in ([_v1()], [_timed()], [_all_day()]):
        validator.validate(
            {
                "window_start": WINDOW_START,
                "window_end": WINDOW_END,
                "events": events,
                "window_complete": True,
                "snapshot_as_of": to_rfc3339(NOW),
                "calendars": [_directory_entry()],
            }
        )
