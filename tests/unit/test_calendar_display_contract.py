"""What a calendar read *renders* — the list card's fields and the summary.

Design §9.2 and §5.2/§5.3 fix the presentation contract, and three of its
rules were still unimplemented when the mirror shape landed:

- **the row must name its calendar.** The list card reads
  「标题 · 日期时间 · 日历名」, and a bare `calendar_identifier` is an EventKit
  UUID that names nothing to a person. The name lives in the device's
  directory, keyed by the same identifier the event carries.
- **an all-day event renders from its dates, never from its instant.** The
  mirror already stores the dates; the summary was still printing the epoch
  through a Shanghai conversion, which is precisely the R1-F2 defect the date
  columns exist to remove — a Tokyo all-day event showed as 09-30 23:00.
- **a timed event renders in its own zone.** Q12 forbids folding a foreign
  event into Shanghai time on any path; a 19:00 Tokyo appointment is not an
  18:00 Shanghai one, and the summary has to say which zone it is naming.

Plus the two honesty annotations: a date whose anchor was never confirmed,
and a field the mirror dropped for length (§6 — 不静默).

Each case below is written against the behaviour that was wrong, so the fix
lands only once they are red.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_agent.api.calendar_query_projection import (
    CalendarQueryProjectionError,
    decode_calendar_query_projection,
    summarise_calendar_projection,
)
from personal_data_mcp.storage import db
from personal_data_mcp.storage.engine import (
    create_database_engine,
    session_factory,
)
from test_calendar_v2_shape import (
    NOW,
    WINDOW_END,
    WINDOW_START,
    _all_day,
    _directory_entry,
    _ingest,
    _query,
    _timed,
)


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "calendar.sqlite")
    db.upgrade(engine, "head")
    yield session_factory(engine)
    engine.dispose()


def _summary(sessions) -> str:
    return summarise_calendar_projection(
        decode_calendar_query_projection(_query(sessions))
    )


def _titles(sessions) -> list[dict]:
    return _query(sessions)["events"]


# --- the list card's calendar name -------------------------------------------


def test_the_row_names_the_calendar_the_event_is_on(sessions) -> None:
    """The directory is the only place a name exists; the identifier is an
    EventKit UUID. Without this the card could show nothing but that UUID."""
    _ingest(
        sessions,
        [_timed("ev-1", calendar_identifier="cal-1")],
        calendars=[_directory_entry("cal-1", "演出&活动")],
    )
    assert _titles(sessions)[0]["calendar_title"] == "演出&活动"


def test_a_row_whose_calendar_is_unknown_says_so_with_a_null(sessions) -> None:
    """A calendar the device never listed (deleted since, or an event whose
    batch predates the directory) has no name to report. The key is present
    and null —「没有名字」is a fact, a missing key would be a different one."""
    _ingest(
        sessions,
        [_timed("ev-1", calendar_identifier="cal-gone")],
        calendars=[_directory_entry("cal-1", "日常安排")],
    )
    row = _titles(sessions)[0]
    assert "calendar_title" in row
    assert row["calendar_title"] is None


def test_the_projection_keeps_the_name_and_refuses_anything_else() -> None:
    base = {
        "status": "ok",
        "record_count": 1,
        "data_as_of": "2026-10-06T08:00:00+08:00",
        "mirror_stale": False,
        "source_system": "apple_calendar_mirror",
        "next_cursor": None,
        "events": [
            {
                "event_identifier": "ev-1",
                "calendar_identifier": "cal-1",
                "calendar_title": "演出&活动",
                "title": "网球",
                "start": "2026-10-02T15:00:00+08:00",
                "end": "2026-10-02T16:30:00+08:00",
                "all_day": False,
                "timezone": "Asia/Shanghai",
                "start_date": None,
                "end_date": None,
                "date_anchor_unknown": False,
                "location": None,
                "notes": None,
                "title_over_limit": False,
                "location_over_limit": False,
                "notes_over_limit": False,
                "created_by_agent": False,
            }
        ],
    }
    decoded = decode_calendar_query_projection(base)
    assert decoded["events"][0]["calendar_title"] == "演出&活动"

    # A name that is not text (or null) is a broken result, and a broken
    # result is never rendered — the same fail-closed rule as every other
    # display field.
    for bad in (123, ["演出&活动"]):
        with pytest.raises(CalendarQueryProjectionError):
            decode_calendar_query_projection(
                {
                    **base,
                    "events": [{**base["events"][0], "calendar_title": bad}],
                }
            )


# --- the summary renders dates and zones -------------------------------------


def test_an_all_day_event_is_rendered_from_its_dates(sessions) -> None:
    """10-01 (Tokyo) through 10-03, whose exclusive end date is 10-04. The
    instant EventKit reports for Tokyo 10-01 is 09-30 23:00 in Shanghai —
    the date the old summary printed."""
    _ingest(sessions, [_all_day("ev-1", title="东京出差")])
    summary = _summary(sessions)
    assert "东京出差" in summary
    assert "10-01 至 10-03" in summary
    assert "全天" in summary
    assert "09-30" not in summary, "the date must not come from the instant"
    assert "23:00" not in summary, "an all-day event has no start time"


def test_a_single_day_all_day_event_shows_one_date(sessions) -> None:
    _ingest(
        sessions,
        [_all_day("ev-1", title="体检", start_date="2026-10-02", end_date="2026-10-03")],
    )
    summary = _summary(sessions)
    assert "10-02 全天" in summary
    assert "10-02 至" not in summary, "one day is not a range"
    assert "10-03" not in summary, "the exclusive end date is not a second day"


def test_a_timed_event_is_rendered_in_its_own_zone(sessions) -> None:
    """Q12: a 19:00 Tokyo appointment is not an 18:00 Shanghai one. The
    summary names the zone instead of folding the instant."""
    _ingest(
        sessions,
        [
            _timed(
                "ev-1",
                title="打喷嚏",
                start="2026-10-02T19:00:00+09:00",
                end="2026-10-02T20:00:00+09:00",
                timezone="Asia/Tokyo",
            )
        ],
    )
    summary = _summary(sessions)
    assert "19:00" in summary
    assert "日本时间" in summary
    assert "18:00" not in summary, "the instant must not be folded into Shanghai"


def test_a_shanghai_event_carries_no_zone_label(sessions) -> None:
    """The reference zone needs no annotation — and it is also what a v1
    upload (no zone recorded) renders as, byte-identical to before."""
    _ingest(sessions, [_timed("ev-1", title="网球")])
    summary = _summary(sessions)
    assert "15:00" in summary
    assert "上海" not in summary and "时间 开始" not in summary


def test_a_zone_with_no_short_name_falls_back_to_its_offset(sessions) -> None:
    """The built-in map covers the common zones; anything else is still
    stated honestly (design §5.3) rather than silently rendered as local."""
    _ingest(
        sessions,
        [
            _timed(
                "ev-1",
                title="加德满都的电话",
                start="2026-10-02T19:00:00+05:45",
                end="2026-10-02T20:00:00+05:45",
                timezone="Asia/Kathmandu",
            )
        ],
    )
    summary = _summary(sessions)
    assert "19:00" in summary
    assert "UTC+05:45" in summary


def test_the_summary_marks_a_date_whose_anchor_was_never_confirmed(sessions) -> None:
    """An external app's all-day event is shown (availability first) with its
    uncertainty stated, not hidden and not asserted away."""
    _ingest(
        sessions,
        [
            _all_day(
                "ev-1",
                title="飞行计划",
                calendar_identifier="cal-ext",
                date_anchor_unknown=True,
            )
        ],
        calendars=[_directory_entry("cal-ext", "飞行计划")],
    )
    summary = _summary(sessions)
    assert "飞行计划" in summary
    assert "日期归属未确认" in summary


def test_a_field_the_mirror_dropped_is_not_silently_absent(sessions) -> None:
    """§6: a dropped note and an absent note must not look alike."""
    _ingest(
        sessions,
        [
            _timed(
                "ev-1",
                title="牙医",
                location=None,
                notes=None,
                location_over_limit=True,
                notes_over_limit=True,
            )
        ],
    )
    summary = _summary(sessions)
    assert "地点过长未同步" in summary
    assert "备注过长未同步" in summary


def test_a_dropped_title_says_what_happened_to_it(sessions) -> None:
    """`（无标题日程）` and「标题被丢掉」are different facts about the same
    null, and only one of them is true here."""
    _ingest(sessions, [_timed("ev-1", title=None, title_over_limit=True)])
    summary = _summary(sessions)
    assert "标题过长未同步" in summary
    assert "无标题" not in summary


def test_the_display_whitelist_narrows_the_ir_and_never_widens_it() -> None:
    """The projection is allowed to keep *fewer* fields than the tool's own
    output contract declares — that is what a display whitelist is — but a
    field it keeps that the IR never declared would be a private contract
    beside the signed one, which is the drift this repo has been bitten by
    three times. This is the assertion that keeps the two lists one list.
    """
    from personal_agent_core.manifest import load_manifest

    from personal_agent.api.calendar_query_projection import (
        _EVENT_FIELDS,
        _TOP_LEVEL_FIELDS,
    )

    schema = next(
        tool
        for tool in load_manifest()["tools"]
        if tool["name"] == "calendar.query_events"
    )["output_schema"]
    declared = set(schema["properties"]) | set(
        schema["properties"]["events"]["items"]["properties"]
    )
    assert (_TOP_LEVEL_FIELDS | _EVENT_FIELDS) <= declared, (
        "the display projection keeps fields the IR does not declare: "
        f"{sorted((_TOP_LEVEL_FIELDS | _EVENT_FIELDS) - declared)}"
    )
    assert "calendar_title" in _EVENT_FIELDS


def test_the_card_row_carries_every_field_the_list_needs(sessions) -> None:
    """The four fields §9.2 adds to the row, on one all-day event: a name, a
    date range, no zone (an all-day event has none) and no truncation."""
    _ingest(
        sessions,
        [_all_day("ev-1", title="东京出差")],
        calendars=[_directory_entry("cal-1", "出游计划")],
    )
    row = _titles(sessions)[0]
    assert row["calendar_title"] == "出游计划"
    assert row["all_day"] is True
    assert row["timezone"] is None
    assert (row["start_date"], row["end_date"]) == ("2026-10-01", "2026-10-04")
    assert row["date_anchor_unknown"] is False
    assert row["title_over_limit"] is False
