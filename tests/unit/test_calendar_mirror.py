"""The Apple-calendar mirror: query core, ingest core, and their handlers.

The mirror is the fact source for `calendar.query_events`. The phone owns the
real calendar; the database holds only what the phone last reported, so every
claim about freshness (`data_as_of`, `mirror_stale`) is computed here and never
inferred by the model.

The ingest is last_modified-arbitrated: a device may upload batches out of
order (foreground refresh racing a `EKEventStoreChanged` retry), so an older
`last_modified` never overwrites a newer row, an identical `last_modified`
skips, and only a `window_complete` final chunk may mark window rows absent
from the batch as deleted. Text fields are sealed at rest; identifiers and
timestamps stay plaintext because the window filter needs a real index.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from personal_agent_core.crypto import KeyEntry, KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import to_rfc3339
from personal_data_mcp.calendar.ingest import ingest_events
from personal_data_mcp.calendar.query_events import query_events
from personal_data_mcp.storage import db
from personal_data_mcp.storage.engine import create_database_engine
from personal_data_mcp.storage.models import CalendarEvent
from personal_data_mcp.storage.engine import session_factory


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
WINDOW_START = "2026-09-07T00:00:00+08:00"
WINDOW_END = "2026-09-08T00:00:00+08:00"
SECRET = b"calendar-cursor-secret" * 2  # 44 bytes — cursor HMAC accepts any length
KEY = bytes(range(32))


def _keyring() -> KeyRing:
    return KeyRing(
        [KeyEntry(kid="test", key=KEY, state="active")], service="personal_data_mcp"
    )


def _event(
    event_id: str,
    *,
    title: str = "网球",
    start: str = "2026-09-07T15:00:00+08:00",
    end: str = "2026-09-07T16:30:00+08:00",
    last_modified: str = "2026-09-06T20:00:00+08:00",
    calendar_identifier: str = "cal-1",
    all_day: bool = False,
    location: str | None = None,
    notes: str | None = None,
) -> dict:
    return {
        "event_identifier": event_id,
        "calendar_identifier": calendar_identifier,
        "title": title,
        "start": start,
        "end": end,
        "all_day": all_day,
        "location": location,
        "notes": notes,
        "last_modified": last_modified,
    }


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "calendar.sqlite")
    db.upgrade(engine, "head")
    factory = session_factory(engine)
    _SNAPSHOT_TICK[0] = 0
    yield factory
    engine.dispose()


_SNAPSHOT_TICK: list[int] = [0]


def _ingest(sessions, events, *, window_complete=True, device_id="dev-1",
            as_of=None):
    """The pre-review helper. Each call is a *new* snapshot: the default
    instant advances a minute per call, because two complete batches sharing
    one snapshot instant are a replay, not a fresh observation."""
    if as_of is None:
        _SNAPSHOT_TICK[0] += 1
        as_of = NOW - timedelta(hours=1) + timedelta(minutes=_SNAPSHOT_TICK[0])
    return ingest_events(
        {
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
            "events": events,
            "window_complete": window_complete,
            "snapshot_as_of": to_rfc3339(as_of),
        },
        sessions=sessions,
        keyring=_keyring(),
        device_id=device_id,
        now=NOW,
    )


# --- migration -------------------------------------------------------------


def _raw_row(row_key: str, **overrides) -> CalendarEvent:
    fields = {
        "calendar_identifier": "cal-1",
        "event_identifier": "ev-1",
        "start_ts": 0,
        "end_ts": 0,
        "all_day": False,
        "title": None,
        "notes": None,
        "location": None,
        "is_deleted": False,
        "last_modified_ts": 0,
        "snapshot_ts": 0,
        "synced_at": NOW,
        "created_by_agent": False,
        "device_id": "dev-1",
        "row_key": row_key,
    }
    fields.update(overrides)
    return CalendarEvent(**fields)


def test_occurrences_of_one_series_are_separate_rows(sessions) -> None:
    """The identity is the triple, not the `(calendar, event)` pair: EventKit
    hands every occurrence of a recurring event the same identifier, so a
    pair-keyed table could only ever keep one of them."""
    with sessions() as session:
        session.add(_raw_row("rk-1", start_ts=0, end_ts=0))
        session.add(_raw_row("rk-2", start_ts=1, end_ts=1))
        session.commit()

    with sessions() as session:
        assert len(session.execute(select(CalendarEvent)).scalars().all()) == 2


def test_a_repeated_triple_is_refused(sessions) -> None:
    """Same calendar, same event, same instant twice is one event uploaded
    twice — a device bug, and the primary key refuses it rather than letting
    the two rows shadow each other."""
    with sessions() as session:
        session.add(_raw_row("rk-1", start_ts=0, end_ts=0))
        session.add(_raw_row("rk-2", start_ts=0, end_ts=0))
        with pytest.raises(IntegrityError):
            session.commit()


# --- ingest ----------------------------------------------------------------


def test_ingest_upserts_a_new_event_and_seals_text(sessions) -> None:
    result = _ingest(sessions, [_event("ev-1", title="网球", notes="带球拍")])
    assert result == {"status": "ok", "upserted": 1, "skipped": 0, "marked_deleted": 0}

    with sessions() as session:
        row = session.execute(select(CalendarEvent)).scalar_one()
    assert row.event_identifier == "ev-1"
    assert not row.is_deleted
    assert row.created_by_agent is False
    assert row.device_id == "dev-1"
    # Sealed, not plaintext: title/notes never appear as raw text at rest.
    envelope = row.title
    assert isinstance(envelope, dict)
    assert set(envelope) == {"v", "kid", "nonce", "ciphertext", "tag"}
    raw = json.dumps(row.__dict__, default=str)
    assert "网球" not in raw and "球拍" not in raw


def test_ingest_replay_of_identical_batch_skips(sessions) -> None:
    _ingest(sessions, [_event("ev-1")])
    again = _ingest(sessions, [_event("ev-1")])
    assert again["upserted"] == 0
    assert again["skipped"] == 1


def test_ingest_older_last_modified_never_overwrites_newer_row(sessions) -> None:
    newer = _event("ev-1", title="改过的标题", last_modified="2026-09-06T22:00:00+08:00")
    older = _event("ev-1", title="更旧的标题", last_modified="2026-09-06T21:00:00+08:00")
    _ingest(sessions, [newer])
    result = _ingest(sessions, [older])
    assert result["upserted"] == 0
    assert result["skipped"] == 1

    with sessions() as session:
        row = session.execute(select(CalendarEvent)).scalar_one()
        keyring = _keyring()
        title = (
            keyring.decrypt(
                row.title,
                table="calendar_events",
                column="title",
                row_id=row.row_key,
            ).decode("utf-8")
        )
    assert title == "改过的标题"


def test_ingest_equal_last_modified_ties_breaks_on_sync_order(sessions) -> None:
    """Equal last_modified: the already-stored row wins (skip), because a tie
    carries no evidence that the incoming copy is newer."""
    a = _event("ev-1", title="标题A", last_modified="2026-09-06T21:00:00+08:00")
    b = _event("ev-1", title="标题B", last_modified="2026-09-06T21:00:00+08:00")
    _ingest(sessions, [a])
    result = _ingest(sessions, [b])
    assert result["skipped"] == 1


def test_window_complete_marks_absent_rows_deleted(sessions) -> None:
    _ingest(sessions, [_event("ev-1"), _event("ev-2")])
    # The phone now reports the window holds only ev-2: ev-1 was deleted there.
    result = _ingest(sessions, [_event("ev-2")], window_complete=True)
    assert result == {"status": "ok", "upserted": 0, "skipped": 1, "marked_deleted": 1}

    with sessions() as session:
        rows = session.execute(select(CalendarEvent)).scalars().all()
    by_id = {row.event_identifier: row for row in rows}
    assert by_id["ev-1"].is_deleted
    assert not by_id["ev-2"].is_deleted


def test_incomplete_window_never_marks_deletions(sessions) -> None:
    """A mid-window chunk leaves rows it did not mention alone: absence inside
    an incomplete batch is silence, not evidence of deletion."""
    _ingest(sessions, [_event("ev-1"), _event("ev-2")])
    result = _ingest(sessions, [_event("ev-2")], window_complete=False)
    assert result["marked_deleted"] == 0

    with sessions() as session:
        rows = session.execute(select(CalendarEvent)).scalars().all()
    assert not any(row.is_deleted for row in rows)


def test_late_stale_batch_cannot_revive_a_deleted_row(sessions) -> None:
    """A delayed chunk holding a pre-deletion copy must not resurrect it.

    The row was marked deleted by a `window_complete` chunk carrying a newer
    state; the stale copy's last_modified predates that, so the tombstone
    survives and the row stays hidden.
    """
    stale = _event("ev-1", last_modified="2026-09-06T20:00:00+08:00")
    _ingest(sessions, [stale], window_complete=True)
    # Newer complete state without ev-1 → ev-1 tombstoned.
    _ingest(sessions, [_event("ev-2")], window_complete=True)
    # Late chunk replays the stale copy.
    result = _ingest(sessions, [stale], window_complete=False)

    assert result["upserted"] == 0
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
    assert row.is_deleted


def test_ingest_rejects_a_batch_beyond_the_size_cap(sessions) -> None:
    events = [_event(f"ev-{index}") for index in range(201)]
    with pytest.raises(AppError) as excinfo:
        _ingest(sessions, events)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_ingest_rejects_a_window_wider_than_the_cap(sessions) -> None:
    with pytest.raises(AppError) as excinfo:
        ingest_events(
            {
                "window_start": WINDOW_START,
                "window_end": "2031-09-08T00:00:00+08:00",
                "events": [],
                "window_complete": True,
            },
            sessions=sessions,
            keyring=_keyring(),
            device_id="dev-1",
            now=NOW,
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_ingest_end_before_start_is_rejected(sessions) -> None:
    with pytest.raises(AppError) as excinfo:
        ingest_events(
            {
                "window_start": WINDOW_END,
                "window_end": WINDOW_START,
                "events": [],
                "window_complete": True,
            },
            sessions=sessions,
            keyring=_keyring(),
            device_id="dev-1",
            now=NOW,
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_ingest_event_outside_window_is_rejected_whole_batch(sessions) -> None:
    """§5.1: no silent triage. One row that contradicts the declared window
    fails the whole batch, so the device resends a coherent snapshot."""
    outside = _event(
        "ev-outside",
        start="2026-09-10T15:00:00+08:00",
        end="2026-09-10T16:30:00+08:00",
    )
    with pytest.raises(AppError) as excinfo:
        _ingest(sessions, [_event("ev-1"), outside])
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    with sessions() as session:
        assert session.execute(select(CalendarEvent)).scalars().all() == []


# --- query -----------------------------------------------------------------


def test_query_returns_window_events_with_freshness(sessions) -> None:
    _ingest(sessions, [_event("ev-1"), _event("ev-2")])
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert result["status"] == "ok"
    assert result["record_count"] == 2
    assert result["source_system"] == "apple_calendar_mirror"
    assert result["mirror_stale"] is False
    # Freshness is the *snapshot* watermark (one minute past the hour, the
    # helper's first tick), not the upload wall clock NOW.
    assert result["data_as_of"] == to_rfc3339(NOW - timedelta(minutes=59))
    titles = sorted(event["title"] for event in result["events"])
    assert titles == ["网球", "网球"]
    identifiers = sorted(event["event_identifier"] for event in result["events"])
    assert identifiers == ["ev-1", "ev-2"]


def test_query_excludes_deleted_rows(sessions) -> None:
    _ingest(sessions, [_event("ev-1")])
    _ingest(sessions, [], window_complete=True)  # ev-1 tombstoned
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert result["record_count"] == 0
    assert result["events"] == []


def test_query_window_is_overlap_semantics(sessions) -> None:
    # The upload window is the full day either side, so a straddling event is
    # a legitimate ingest; the query window is the single day under test.
    ingest_window = {"window_start": "2026-09-06T00:00:00+08:00",
                     "window_end": "2026-09-09T00:00:00+08:00"}

    def _ingest_wide(events, *, window_complete=True):
        return ingest_events(
            {
                **ingest_window,
                "events": events,
                "window_complete": window_complete,
                "snapshot_as_of": to_rfc3339(NOW - timedelta(minutes=1)),
            },
            sessions=sessions,
            keyring=_keyring(),
            device_id="dev-1",
            now=NOW,
        )

    _ingest_wide(
        [
            _event("ev-day-before", start="2026-09-06T15:00:00+08:00",
                   end="2026-09-06T16:00:00+08:00"),
            _event("ev-on-start", start="2026-09-07T00:00:00+08:00",
                   end="2026-09-07T01:00:00+08:00"),
            _event("ev-overlapping-end", start="2026-09-07T23:00:00+08:00",
                   end="2026-09-08T01:00:00+08:00"),
            _event("ev-on-end", start="2026-09-08T00:00:00+08:00",
                   end="2026-09-08T01:00:00+08:00"),
        ],
    )
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    identifiers = sorted(event["event_identifier"] for event in result["events"])
    # Overlap semantics: an event counts when it intersects the window.
    assert identifiers == ["ev-on-start", "ev-overlapping-end"]


def test_query_all_day_event_matches_its_whole_day(sessions) -> None:
    _ingest(
        sessions,
        [
            _event(
                "ev-allday",
                all_day=True,
                start="2026-09-07T00:00:00+08:00",
                end="2026-09-08T00:00:00+08:00",
            )
        ],
    )
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert result["record_count"] == 1
    assert result["events"][0]["all_day"] is True


def test_query_sorts_by_start_then_identifier(sessions) -> None:
    _ingest(
        sessions,
        [
            _event("ev-late", start="2026-09-07T18:00:00+08:00",
                   end="2026-09-07T19:00:00+08:00"),
            _event("ev-early", start="2026-09-07T09:00:00+08:00",
                   end="2026-09-07T10:00:00+08:00"),
            _event("ev-mid", start="2026-09-07T12:00:00+08:00",
                   end="2026-09-07T13:00:00+08:00"),
        ],
    )
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert [event["event_identifier"] for event in result["events"]] == [
        "ev-early",
        "ev-mid",
        "ev-late",
    ]


def test_query_paginates_with_signed_cursor(sessions) -> None:
    events = [
        _event(
            f"ev-{index:02d}",
            start=f"2026-09-07T{index:02d}:00:00+08:00",
            end=f"2026-09-07T{index:02d}:30:00+08:00",
        )
        for index in range(8)
    ]
    _ingest(sessions, events)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        result = query_events(
            {"start": WINDOW_START, "end": WINDOW_END, "cursor": cursor},
            sessions=sessions,
            keyring=_keyring(),
            cursor_secret=SECRET,
            now=NOW,
            page_size=3,
        )
        seen.extend(event["event_identifier"] for event in result["events"])
        pages += 1
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert len(seen) == 8
    assert len(set(seen)) == 8


def test_query_rejects_a_tampered_cursor(sessions) -> None:
    payload = base64.urlsafe_b64encode(
        json.dumps({"offset": 0, "exp": 9, "v": 1, "window": "x"}).encode()
    ).rstrip(b"=").decode()
    with pytest.raises(AppError) as excinfo:
        query_events(
            {"start": WINDOW_START, "end": WINDOW_END, "cursor": f"{payload}.bad"},
            sessions=sessions,
            keyring=_keyring(),
            cursor_secret=SECRET,
            now=NOW,
        )
    assert excinfo.value.code is ErrorCode.INVALID_CURSOR


def test_query_rejects_a_cursor_from_a_different_window(sessions) -> None:
    events = [
        _event(
            f"ev-{index:02d}",
            start=f"2026-09-07T{index:02d}:00:00+08:00",
            end=f"2026-09-07T{index:02d}:30:00+08:00",
        )
        for index in range(4)
    ]
    _ingest(sessions, events)
    first = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
        page_size=2,
    )
    assert first["next_cursor"] is not None
    # A cursor is bound to the canonical filters of the query that minted it.
    with pytest.raises(AppError) as excinfo:
        query_events(
            {
                "start": WINDOW_START,
                "end": "2026-09-09T00:00:00+08:00",
                "cursor": first["next_cursor"],
            },
            sessions=sessions,
            keyring=_keyring(),
            cursor_secret=SECRET,
            now=NOW,
            page_size=2,
        )
    assert excinfo.value.code is ErrorCode.INVALID_CURSOR


def test_query_empty_mirror_reports_query_time_as_of(sessions) -> None:
    """Review R7: the output schema promises `data_as_of` is never null.
    Before the first completed snapshot there is no honest instant, so the
    query's own wall clock stands in — and `mirror_stale` says what that
    placeholder means."""
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert result["record_count"] == 0
    assert result["data_as_of"] == to_rfc3339(NOW)
    assert result["mirror_stale"] is True


def test_query_marks_the_mirror_stale_by_age(sessions) -> None:
    _ingest(sessions, [_event("ev-1")])
    three_days_later = NOW + timedelta(days=3)
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=three_days_later,
    )
    assert result["mirror_stale"] is True


def test_query_reported_events_match_the_output_schema(sessions) -> None:
    _ingest(sessions, [_event("ev-1", location="球场", notes="带水")])
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    event = result["events"][0]
    assert event["location"] == "球场"
    assert event["notes"] == "带水"
    assert event["created_by_agent"] is False
    # Finance's strict decoder keys on `metric`; a calendar read must not
    # present one, or a valid result would fail closed inside that projection.
    assert "metric" not in event


def test_a_titleless_event_satisfies_the_output_schema(sessions) -> None:
    """Review R7: EventKit allows events with no title, and a mirror that
    reports `null` is a fact, not a missing field. The IR schema already
    declares `title` nullable; this pins the implementation to it — the real
    output contract must accept exactly what the mirror really produces."""
    from jsonschema import Draft202012Validator, FormatChecker

    from personal_agent_core.tool_ir import CALENDAR_QUERY_EVENTS

    _ingest(
        sessions,
        [_event("ev-1", title=None, location=None, notes=None)],
    )
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert result["events"][0]["title"] is None
    Draft202012Validator(
        CALENDAR_QUERY_EVENTS.output_schema, format_checker=FormatChecker()
    ).validate(result)


def test_the_full_query_result_validates_against_the_output_schema(
    sessions,
) -> None:
    """Review R7: the whole result — not just events — must satisfy the
    contract, including the non-null `data_as_of` promise after a real
    snapshot and the empty-mirror placeholder case."""
    from jsonschema import Draft202012Validator, FormatChecker

    from personal_agent_core.tool_ir import CALENDAR_QUERY_EVENTS

    validator = Draft202012Validator(
        CALENDAR_QUERY_EVENTS.output_schema, format_checker=FormatChecker()
    )

    # Empty mirror: data_as_of is the query-instant placeholder.
    empty = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    validator.validate(empty)

    _ingest(sessions, [_event("ev-1"), _event("ev-2", title="无题事件")])
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    validator.validate(result)
    # And the placeholder case's honesty fields: after a completed snapshot
    # the as_of is the snapshot instant and the mirror is not stale.
    assert result["mirror_stale"] is False


# --- server handlers -------------------------------------------------------


def test_create_event_handler_is_a_fail_closed_guard() -> None:
    from personal_data_mcp.server.calendar_guard import build_handler, CreateGuard

    handler = build_handler(CreateGuard())
    invocation = object.__new__(object)
    with pytest.raises(AppError) as excinfo:
        asyncio.run(handler(invocation))
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


def test_calendar_handlers_registered_with_composition_registry() -> None:
    """The three calendar handlers bind through composition: query and ingest
    get real dependencies; create_event binds the fail-closed guard, so the
    server advertises the tool while the device path intercepts execution."""
    from personal_data_mcp.server.app import build_registry
    from personal_data_mcp.server.calendar_ingest import (
        CalendarIngestDependencies,
        build_handler as build_ingest_handler,
    )
    from personal_data_mcp.server.calendar_query import (
        CalendarQueryDependencies,
        build_handler as build_query_handler,
    )

    registry = build_registry(
        calendar_query_handler=build_query_handler(
            CalendarQueryDependencies(
                sessions=object(),
                keyring=_keyring(),
                cursor_secret=SECRET,
            )
        ),
        calendar_ingest_handler=build_ingest_handler(
            CalendarIngestDependencies(
                sessions=object(),
                keyring=_keyring(),
            )
        ),
    )
    assert registry.handler("calendar.query_events") is not None
    assert registry.handler("calendar.ingest_events") is not None
    assert registry.handler("calendar.create_event") is not None
    names = {entry["name"] for entry in registry.catalog()}
    assert {"calendar.create_event", "calendar.query_events"} <= names


def test_ingest_handler_stamps_the_verified_caller_onto_rows(sessions) -> None:
    """The mirror row's `device_id` is the signed Host Context claim, never a
    payload field or a composition constant: a device cannot claim to be
    another device by re-serialising its payload, and a server-side caller is
    recorded as exactly what it is."""
    from personal_data_mcp.server.authz import VerifiedCall
    from personal_data_mcp.calendar.ingest import TABLE
    from personal_data_mcp.server.calendar_ingest import (
        CalendarIngestDependencies,
        build_handler,
    )
    from personal_data_mcp.server.handlers import ToolInvocation

    keyring = _keyring()
    handler = build_handler(
        CalendarIngestDependencies(sessions=sessions, keyring=keyring)
    )
    invocation = ToolInvocation(
        tool="calendar.ingest_events",
        arguments={
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
            "events": [_event("ek-verified", title="牙医复诊")],
            "window_complete": True,
            "snapshot_as_of": to_rfc3339(NOW - timedelta(minutes=1)),
        },
        verified_call=VerifiedCall(
            tool="calendar.ingest_events",
            idempotency_key="ik",
            request_fingerprint="fp",
            request_id="req",
            trace_id="tr",
            user_id="henson",
            device_id="device-9",
            timezone="Asia/Shanghai",
            scopes=("calendar.event.read",),
            allowed_tools_version="0.1.0",
        ),
    )
    asyncio.run(handler(invocation))
    with sessions() as session:
        row = session.execute(select(CalendarEvent)).scalar_one()
        assert row.device_id == "device-9"
        assert (
            keyring.decrypt(
                row.title, table=TABLE, column="title", row_id=row.row_key
            ).decode("utf-8")
            == "牙医复诊"
        )


# --- snapshot versioning (review R2/R3/R10) ---------------------------------
#
# The first cut arbitrated per-event `last_modified` and tombstoned from the
# last batch's membership alone. Both premises were wrong: EventKit exposes no
# per-event modification time (the device was stamping the snapshot instant
# into every row), and a window split across batches deleted everything the
# final batch did not mention. The corrected model makes the snapshot instant
# the version: every batch of one window declares the same `snapshot_as_of`,
# a complete snapshot newer than the device's watermark sweeps the window, and
# freshness is the watermark — never any single row's sync time.


def _ingest_as_of(sessions, events, *, as_of, window_complete=True, device_id="dev-1"):
    """One batch of the snapshot taken at `as_of`, uploaded at NOW."""
    return ingest_events(
        {
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
            "events": events,
            "window_complete": window_complete,
            "snapshot_as_of": to_rfc3339(as_of),
        },
        sessions=sessions,
        keyring=_keyring(),
        device_id=device_id,
        now=NOW,
    )


def test_multi_batch_window_does_not_delete_earlier_batches(sessions) -> None:
    """Review R2, reproduced: batch 1 uploads A, batch 2 uploads B and claims
    window_complete — the mirror kept only B. A snapshot sweep must see the
    whole window's membership, so both events survive."""
    as_of = NOW - timedelta(minutes=1)
    _ingest_as_of(sessions, [_event("ev-1")], as_of=as_of, window_complete=False)
    result = _ingest_as_of(sessions, [_event("ev-2")], as_of=as_of, window_complete=True)
    assert result["marked_deleted"] == 0

    with sessions() as session:
        rows = session.execute(select(CalendarEvent)).scalars().all()
    assert {row.event_identifier for row in rows if not row.is_deleted} == {"ev-1", "ev-2"}


def test_a_late_packet_from_an_older_snapshot_cannot_revive_or_delete(sessions) -> None:
    """Review R3, reproduced: after a deletion a packet stamped between the
    row's old version and the deletion resurrected it. The snapshot instant is
    the version now: a packet whose `snapshot_as_of` is not newer than the
    device's watermark may upsert rows but may never tombstone anything, and a
    copy older than the tombstone's version cannot clear it."""
    as_of_1 = NOW - timedelta(hours=3)
    as_of_2 = NOW - timedelta(hours=2)
    as_of_delete = NOW - timedelta(hours=1)
    # Snapshot 1 has both events; snapshot 2 updates ev-1; snapshot 3 (newer)
    # no longer holds ev-1 — a complete sweep tombstones it.
    _ingest_as_of(sessions, [_event("ev-1"), _event("ev-2")], as_of=as_of_1)
    _ingest_as_of(
        sessions,
        [_event("ev-1", title="改过", last_modified=to_rfc3339(as_of_2)), _event("ev-2")],
        as_of=as_of_2,
    )
    _ingest_as_of(sessions, [_event("ev-2")], as_of=as_of_delete)
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
        assert row.is_deleted

    # A late packet from *snapshot 2* (older than the deletion's watermark)
    # replays ev-1 with ev-1's old last_modified. It must not resurrect it.
    _ingest_as_of(
        sessions,
        [_event("ev-1", last_modified=to_rfc3339(as_of_2))],
        as_of=as_of_2,
        window_complete=False,
    )
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
        assert row.is_deleted

    # And the *deleting* snapshot replaying must not sweep snapshot-2 rows
    # re-uploaded after it: the watermark guards the sweep, and a stale
    # complete batch never tombstones.
    _ingest_as_of(
        sessions, [_event("ev-1", last_modified=to_rfc3339(as_of_2))], as_of=as_of_2
    )
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
        assert row.is_deleted


def test_same_snapshot_late_batch_revives_its_own_member(sessions) -> None:
    """Out-of-order arrival *within* one snapshot: the complete batch may land
    first, then the straggler chunk of the same snapshot arrives. The
    straggler asserts the event exists — that testimony is newer evidence
    than a tombstone this same snapshot wrote, so the row comes back."""
    as_of = NOW - timedelta(minutes=2)
    # First a row exists from an older snapshot, so the sweep has something
    # to tombstone.
    _ingest_as_of(sessions, [_event("ev-1")], as_of=NOW - timedelta(hours=1))
    # The complete batch of the newer snapshot mentions only ev-2 → ev-1 is
    # tombstoned (its version is the older snapshot's instant).
    result = _ingest_as_of(sessions, [_event("ev-2")], as_of=as_of, window_complete=True)
    assert result["marked_deleted"] == 1
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
        assert row.is_deleted

    # The straggler chunk of the *same* snapshot re-asserts ev-1. Same
    # snapshot version: the sweep of this snapshot cannot have meant to
    # delete what this snapshot holds.
    result = _ingest_as_of(sessions, [_event("ev-1")], as_of=as_of, window_complete=False)
    assert result["upserted"] == 1
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
    assert not row.is_deleted


def test_tombstone_revival_requires_a_newer_snapshot(sessions) -> None:
    """A row tombstoned by snapshot N comes back only through a snapshot
    *newer* than the tombstone's version asserting it again."""
    as_of_delete = NOW - timedelta(hours=1)
    # Seed the window with ev-1 from an older snapshot, then delete it: the
    # newer complete snapshot no longer holds it, so the sweep tombstones it.
    _ingest_as_of(sessions, [_event("ev-1")], as_of=NOW - timedelta(hours=3))
    _ingest_as_of(sessions, [_event("ev-2")], as_of=as_of_delete)
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
    assert row.is_deleted  # swept by the empty-but-complete snapshot

    # A replay of the deleting snapshot cannot revive it.
    _ingest_as_of(sessions, [_event("ev-2")], as_of=as_of_delete)
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
    assert row.is_deleted

    # A genuinely newer snapshot holding ev-1 revives it.
    as_of_new = NOW - timedelta(minutes=1)
    _ingest_as_of(
        sessions,
        [_event("ev-1", last_modified=to_rfc3339(as_of_new)), _event("ev-2")],
        as_of=as_of_new,
    )
    with sessions() as session:
        row = session.execute(
            select(CalendarEvent).where(CalendarEvent.event_identifier == "ev-1")
        ).scalar_one()
    assert not row.is_deleted


def test_empty_complete_snapshot_sweeps_and_advances_the_watermark(sessions) -> None:
    """The calendar really is empty: one complete empty batch tombstones the
    window and *advances the watermark*, so freshness (R10) reflects a real
    observation of nothing, not a stale guess."""
    _ingest_as_of(sessions, [_event("ev-1")], as_of=NOW - timedelta(hours=2))
    result = _ingest_as_of(sessions, [], as_of=NOW - timedelta(hours=1))
    assert result["marked_deleted"] == 1

    from personal_data_mcp.calendar.ingest import device_watermark

    watermark = device_watermark(sessions, device_id="dev-1")
    assert watermark == NOW - timedelta(hours=1)


def test_freshness_is_the_completed_snapshot_watermark(sessions) -> None:
    """Review R10, reproduced: uploading only a first incomplete batch made the
    whole mirror report fresh. Freshness is the last *completed* snapshot's
    instant — an incomplete upload is honest silence, and the query says so."""
    _ingest_as_of(sessions, [_event("ev-1")], as_of=NOW, window_complete=False)
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert result["mirror_stale"] is True
    # No completed snapshot yet: the schema's non-null promise (R7) makes the
    # query wall clock the placeholder, and mirror_stale carries the honesty.
    assert result["data_as_of"] == to_rfc3339(NOW)

    # Completing the snapshot makes it fresh, and `data_as_of` is the snapshot
    # instant — not the upload instant.
    as_of = NOW - timedelta(minutes=5)
    _ingest_as_of(sessions, [_event("ev-1")], as_of=as_of, window_complete=True)
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert result["mirror_stale"] is False
    assert result["data_as_of"] == to_rfc3339(as_of)


def test_freshness_is_per_device_watermark(sessions) -> None:
    """Freshness reads the completed snapshots of every device: the newest
    completed snapshot across devices is `data_as_of`, and a device's own
    watermark is what its own uploads produced."""
    _ingest_as_of(sessions, [_event("ev-1")], as_of=NOW, device_id="dev-1")
    _ingest_as_of(
        sessions, [_event("ev-3")], as_of=NOW - timedelta(days=3), device_id="dev-2"
    )
    result = query_events(
        {"start": WINDOW_START, "end": WINDOW_END},
        sessions=sessions,
        keyring=_keyring(),
        cursor_secret=SECRET,
        now=NOW,
    )
    assert result["mirror_stale"] is False

    from personal_data_mcp.calendar.ingest import device_watermark

    assert device_watermark(sessions, device_id="dev-2") == NOW - timedelta(days=3)
    assert device_watermark(sessions, device_id="dev-none") is None


def test_ingest_without_snapshot_as_of_is_refused(sessions) -> None:
    """The schema made `snapshot_as_of` required: a batch that cannot say when
    its snapshot was taken has no version, and versionless evidence must not
    arbitrate anything."""
    with pytest.raises(AppError) as excinfo:
        ingest_events(
            {
                "window_start": WINDOW_START,
                "window_end": WINDOW_END,
                "events": [_event("ev-1")],
                "window_complete": False,
            },
            sessions=sessions,
            keyring=_keyring(),
            device_id="dev-1",
            now=NOW,
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
