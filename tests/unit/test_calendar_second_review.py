"""Second-review reproducers for the calendar mirror (2026-09-08).

The first remediation round made the snapshot instant the version, but the
watermark still only gated *sweep eligibility* — it did not constrain inserts
or tombstone updates (F3), an older complete snapshot could still delete a
newer row (F4), the display projection equated one page with the whole result
(F5), the deterministic summary hid both the events and the staleness (F6),
and one completed window made never-uploaded windows read as fresh (F7).

Every test here reproduces its defect against the pre-fix behaviour, so the
fix lands only when all of these are red first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from personal_agent_core.crypto import KeyEntry, KeyRing
from personal_agent_core.timeutil import to_rfc3339
from personal_data_mcp.calendar.ingest import ingest_events
from personal_data_mcp.calendar.query_events import query_events
from personal_data_mcp.storage import db
from personal_data_mcp.storage.engine import (
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import CalendarEvent


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
WINDOW_START = "2026-09-07T00:00:00+08:00"
WINDOW_END = "2026-09-08T00:00:00+08:00"
SECRET = b"calendar-cursor-secret" * 2
KEY = bytes(range(32))


def _keyring() -> KeyRing:
    return KeyRing(
        [KeyEntry(kid="test", key=KEY, state="active")], service="personal_data_mcp"
    )


def _event(
    event_id: str,
    *,
    title: str | None = "网球",
    start: str = "2026-09-07T15:00:00+08:00",
    end: str = "2026-09-07T16:30:00+08:00",
    last_modified: str = "2026-09-06T20:00:00+08:00",
    calendar_identifier: str = "cal-1",
    all_day: bool = False,
) -> dict:
    return {
        "event_identifier": event_id,
        "calendar_identifier": calendar_identifier,
        "title": title,
        "start": start,
        "end": end,
        "all_day": all_day,
        "location": None,
        "notes": None,
        "last_modified": last_modified,
    }


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "calendar.sqlite")
    db.upgrade(engine, "head")
    yield session_factory(engine)
    engine.dispose()


def _ingest_as_of(
    sessions, events, *, as_of, window_complete=True, device_id="dev-1",
    window=(WINDOW_START, WINDOW_END),
):
    return ingest_events(
        {
            "window_start": window[0],
            "window_end": window[1],
            "events": events,
            "window_complete": window_complete,
            "snapshot_as_of": to_rfc3339(as_of),
        },
        sessions=sessions,
        keyring=_keyring(),
        device_id=device_id,
        now=NOW,
    )


def _query(sessions, start=WINDOW_START, end=WINDOW_END, now=NOW, **kwargs):
    return query_events(
        {"start": start, "end": end},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=now,
        **kwargs,
    )


# --- F3: the watermark must constrain inserts and tombstones -----------------


def test_a_late_packet_cannot_insert_behind_the_watermark(sessions) -> None:
    """F3a: a new complete empty snapshot finishes (watermark T4), then a late
    packet from an older snapshot T3 first brings event A. The insert must be
    refused — the device already vouched, at a newer instant, that the window
    did not contain A."""
    t3 = NOW - timedelta(hours=3)
    t4 = NOW - timedelta(hours=2)
    _ingest_as_of(sessions, [], as_of=t4, window_complete=True)
    result = _ingest_as_of(
        sessions, [_event("ev-1")], as_of=t3, window_complete=False
    )
    assert result["upserted"] == 0
    with sessions() as session:
        rows = session.execute(select(CalendarEvent)).scalars().all()
    assert rows == [], "a packet older than the watermark must not insert"


def test_a_late_packet_cannot_revive_behind_the_watermark(sessions) -> None:
    """F3b: A deleted at T2 and re-confirmed absent at T4; a late T3 packet
    still asserts A. The tombstone's version was advanced by T4, so the T3
    assertion has no standing to clear it."""
    t2 = NOW - timedelta(hours=4)
    t3 = NOW - timedelta(hours=3)
    t4 = NOW - timedelta(hours=2)
    _ingest_as_of(sessions, [_event("ev-1"), _event("ev-2")], as_of=t2)
    # A newer snapshot holds only ev-2: the sweep tombstones ev-1 at T2+1min.
    _ingest_as_of(sessions, [_event("ev-2")], as_of=t2 + timedelta(minutes=1))
    # T4 completes without ev-1 again: the tombstone's version advances to T4.
    _ingest_as_of(sessions, [_event("ev-2")], as_of=t4, window_complete=True)
    # The late T3 packet asserts ev-1 with a last_modified between the two.
    result = _ingest_as_of(
        sessions,
        [_event("ev-1", last_modified=to_rfc3339(t3))],
        as_of=t3,
        window_complete=False,
    )
    assert result["upserted"] == 0
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
    assert row.is_deleted
    assert row.snapshot_ts == int(t4.timestamp())


def test_an_older_complete_snapshot_cannot_delete_a_newer_rows_event(
    sessions,
) -> None:
    """F4: an incomplete T4 batch uploads A, then a complete empty T2 snapshot
    arrives late. T2's sweep must not delete A — A's version (T4) is newer
    than the sweeping snapshot's version (T2). Deletion obeys version
    monotonicity, not batch order."""
    t2 = NOW - timedelta(hours=4)
    t4 = NOW - timedelta(hours=2)
    _ingest_as_of(sessions, [_event("ev-1")], as_of=t4, window_complete=False)
    result = _ingest_as_of(sessions, [], as_of=t2, window_complete=True)
    assert result["marked_deleted"] == 0
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
    assert not row.is_deleted


# --- F5: a page is a page -----------------------------------------------------


def test_the_projection_accepts_a_page_of_a_larger_result(sessions) -> None:
    """51 events with page size 50 — the projection demanded
    len(events) == record_count and failed the whole query at the boundary the
    pagination itself creates. A page is a page: events ≤ record_count, and a
    next_cursor means strictly fewer arrived than the total."""
    from personal_agent.api.calendar_query_projection import (
        CalendarQueryProjectionError,
        decode_calendar_query_projection,
    )

    events = [
        _event(
            f"ev-{index:02d}",
            start=f"2026-09-07T{index % 24:02d}:{index % 60:02d}:00+08:00",
            end=f"2026-09-07T{index % 24:02d}:{index % 60:02d}:30+08:00",
            last_modified=f"2026-09-06T{index % 24:02d}:00:00+08:00",
        )
        for index in range(51)
    ]
    _ingest_as_of(sessions, events, as_of=NOW - timedelta(hours=1))
    result = _query(sessions)
    assert result["record_count"] == 51
    assert len(result["events"]) == 50
    assert result["next_cursor"] is not None

    projection = decode_calendar_query_projection(result)
    assert projection["record_count"] == 51
    assert len(projection["events"]) == 50

    final = _query(sessions)
    assert final["record_count"] == 51

    # The last page: continue through the cursor.
    continued = query_events(
        {
            "start": WINDOW_START,
            "end": WINDOW_END,
            "cursor": result["next_cursor"],
        },
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert len(continued["events"]) == 1
    assert continued["next_cursor"] is None
    decode_calendar_query_projection(continued)

    # A page that *exceeds* record_count is still a broken result.
    with pytest.raises(CalendarQueryProjectionError):
        decode_calendar_query_projection({**result, "record_count": 10})
    # A page that claims to hold every record while still offering a cursor
    # contradicts itself: a strict prefix must be strict.
    with pytest.raises(CalendarQueryProjectionError):
        decode_calendar_query_projection({**result, "record_count": 50})
    # An empty result with a cursor is equally self-contradictory.
    with pytest.raises(CalendarQueryProjectionError):
        decode_calendar_query_projection(
            {**result, "events": [], "record_count": 0}
        )


# --- F6: the summary must carry the events and the staleness ------------------


def test_the_summary_names_the_events_and_the_staleness(sessions) -> None:
    """The fallback said only 共 N 条日程 — no titles, no times, no
    staleness. The deterministic answer must name what the mirror holds (up to
    the first few), say when it was taken, and warn when it is stale."""
    from personal_agent.api.calendar_query_projection import (
        decode_calendar_query_projection,
        summarise_calendar_projection,
    )

    _ingest_as_of(
        sessions,
        [_event("ev-1", title="网球"), _event("ev-2", title="体检")],
        as_of=NOW - timedelta(hours=1),
    )
    result = _query(sessions)
    summary = summarise_calendar_projection(decode_calendar_query_projection(result))
    assert "网球" in summary and "体检" in summary
    assert "15:00" in summary, "the summary must carry a start time, not a count"
    assert "数据截至" in summary
    assert "陈旧" not in summary, "a fresh mirror must not warn"

    three_days_later = NOW + timedelta(days=3)
    stale = _query(sessions, now=three_days_later)
    stale_summary = summarise_calendar_projection(
        decode_calendar_query_projection(stale)
    )
    assert "陈旧" in stale_summary, "a stale mirror must say so in the answer"


def test_the_empty_mirror_summary_says_never_synced(sessions) -> None:
    """Before any completed snapshot, 数据截至 is the query-instant
    placeholder — presenting it as a real observation time is the lie F6
    names. The summary must say the mirror has not synced."""
    from personal_agent.api.calendar_query_projection import (
        decode_calendar_query_projection,
        summarise_calendar_projection,
    )

    result = _query(sessions)
    summary = summarise_calendar_projection(decode_calendar_query_projection(result))
    assert "尚未同步" in summary


# --- F7: freshness requires window coverage -----------------------------------


def test_freshness_requires_the_window_to_be_covered(sessions) -> None:
    """Completing the September window made a January window — never uploaded
    — read as fresh. The watermark must record which window it completed, and
    a query outside that coverage is honestly stale."""
    january = ("2027-01-07T00:00:00+08:00", "2027-01-08T00:00:00+08:00")
    _ingest_as_of(sessions, [_event("ev-1")], as_of=NOW - timedelta(hours=1))

    covered = _query(sessions)
    assert covered["mirror_stale"] is False
    assert covered["data_as_of"] == to_rfc3339(NOW - timedelta(hours=1))

    uncovered = _query(sessions, start=january[0], end=january[1])
    assert uncovered["mirror_stale"] is True


# --- third-review defects (2026-09-09, G3/G4/G5) ------------------------------
#
# The watermark fix over-corrected: "not newer than the watermark" also
# swallowed a straggler *of the very snapshot that set the watermark* — its
# early batches arriving after the last batch completed. And the tombstone
# revive kept the swept row's stale fields, because the sweep itself had
# stamped last_modified with the snapshot instant.


def _titles(sessions) -> dict[str, str | None]:
    """event_identifier -> title, through the sealed column."""
    with sessions() as session:
        rows = session.execute(select(CalendarEvent)).scalars().all()
    ring = _keyring()
    out: dict[str, str | None] = {}
    for row in rows:
        envelope = row.title
        out[row.event_identifier] = (
            None
            if envelope is None
            else ring.decrypt(
                envelope,
                table="calendar_events",
                column="title",
                row_id=row.row_key,
            ).decode("utf-8")
        )
    return out


def test_a_same_snapshot_straggler_batch_still_lands(sessions) -> None:
    """G3: the last batch of a snapshot completes the window (the watermark
    advances to that snapshot's instant); an early batch of the *same*
    snapshot arrives afterwards. Its events are not late evidence about a
    *different* older state — they are members of the snapshot the watermark
    itself names, and they must land. Treating `== watermark` as late loses
    them and the query answers as if the window had only the last batch."""
    as_of = NOW - timedelta(hours=1)
    # Batch 1 (incomplete) is delayed in flight; batch 2 (the last one)
    # completes the window first.
    _ingest_as_of(
        sessions, [_event("ev-2")], as_of=as_of, window_complete=True
    )
    # The delayed batch 1 of the same snapshot arrives now.
    result = _ingest_as_of(
        sessions, [_event("ev-1")], as_of=as_of, window_complete=False
    )
    assert result["upserted"] == 1
    answer = _query(sessions)
    assert answer["record_count"] == 2
    assert {event["event_identifier"] for event in answer["events"]} == {
        "ev-1",
        "ev-2",
    }


def test_a_same_snapshot_revive_takes_the_uploaded_fields(sessions) -> None:
    """G4: the last batch completes first and the sweep tombstones a row the
    snapshot still holds; the row's own batch arrives afterwards and revives
    it. The sweep had stamped `last_modified_ts` with the snapshot instant —
    strictly newer than the event's real last_modified — so a revive gated on
    "strictly newer last_modified" copies no fields and the row keeps its
    swept-away stale content. A same-snapshot revive must take the fields the
    snapshot actually uploaded."""
    as_of = NOW - timedelta(hours=1)
    # An older snapshot seeds ev-1 with the old title.
    _ingest_as_of(
        sessions, [_event("ev-1", title="old")], as_of=NOW - timedelta(hours=3)
    )
    # The same snapshot's last batch (only ev-2) completes and sweeps ev-1.
    _ingest_as_of(
        sessions, [_event("ev-2")], as_of=as_of, window_complete=True
    )
    # The delayed batch of the *same* snapshot carries ev-1's new content.
    result = _ingest_as_of(
        sessions,
        [_event("ev-1", title="new", last_modified=to_rfc3339(as_of))],
        as_of=as_of,
        window_complete=False,
    )
    assert result["upserted"] == 1
    assert _titles(sessions)["ev-1"] == "new"


def test_the_summary_counts_only_what_it_omits(sessions) -> None:
    """G5: the summary showed three events but computed the omitted count
    against the whole *page* (count − len(events)), so a page of five showed
    three lines and claimed nothing was missing. The omitted count is against
    what was actually displayed."""
    from personal_agent.api.calendar_query_projection import (
        decode_calendar_query_projection,
        summarise_calendar_projection,
    )

    events = [
        _event(
            f"ev-{index}",
            title=f"日程{index}",
            start=f"2026-09-07T{8 + index:02d}:00:00+08:00",
            end=f"2026-09-07T{8 + index:02d}:30:00+08:00",
        )
        for index in range(5)
    ]
    _ingest_as_of(sessions, events, as_of=NOW - timedelta(hours=1))
    result = _query(sessions)
    assert len(result["events"]) == 5
    summary = summarise_calendar_projection(decode_calendar_query_projection(result))
    assert "日程0" in summary and "日程2" in summary
    assert "日程3" not in summary and "日程4" not in summary
    assert "另有 2 条未列出" in summary, (
        "the summary must say how many it omitted, computed against what it "
        f"actually displayed — got: {summary}"
    )
