"""The ingest barrier: what a rebuilt mirror refuses, and what it never reopens.

Design 14.2's rollback empties the calendar mirror and rebuilds it from the
phone. The defect class this file exists for is the one four review rounds
converged on (R2-F12, R3-F15, R4-F16/F17, R6-F20/F21, R7-F22): a batch captured
*before* the rebuild arriving *after* it, writing a mirror that no longer holds
the rest of its window, or writing a complete-window watermark over a window
that was only partly re-uploaded.

The defenses are three, and each is tested against the shapes that would make
it useless rather than against the shape it was designed for:

- the protocol floor is **one-way** -- so the tests that matter are the ones
  where a *success* happens (`v2` batch accepted, complete window accepted) and
  the floor still does not move, plus a guard that the module has no transition
  that could move it down;
- `rebuild_pending` is presentation, so the test is that clearing it clears
  nothing else;
- `rebuild_instant` is a warning, so the tests are that a batch at exactly that
  instant is still decided by the version alone -- **the zero-skew case**, which
  is the one a tolerance-based rule cannot get right (R6-F20).

Every assertion here was confirmed red against a deliberately broken
implementation before it was accepted; the delivery notes record which break
each one caught.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from personal_agent_core.crypto import KeyEntry, KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import to_rfc3339
from personal_data_mcp.calendar import policy
from personal_data_mcp.calendar.ingest import ingest_events
from personal_data_mcp.storage import db
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import (
    CalendarDeviceSync,
    CalendarEvent,
    CalendarIngestPolicy,
)


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
#: The instant the controlled recovery ran. One minute before the upload clock,
#: so a batch captured at the rebuild instant is still a legal upload (the
#: ingest refuses a snapshot in the future), and the two instants can be made
#: *equal* to model a zero-skew clock.
REBUILD_AT = NOW - timedelta(minutes=1)
WINDOW_START = "2026-09-07T00:00:00+08:00"
WINDOW_END = "2026-09-08T00:00:00+08:00"
KEY = bytes(range(32))

#: Every public transition in `policy`, named so the ratchet test can apply all
#: of them. A guard test compares this tuple against the module, so a transition
#: added later fails the guard rather than escaping the ratchet test.
TRANSITIONS = (
    "begin_rebuild",
    "complete_rebuild",
    "enter_maintenance",
    "leave_maintenance",
)


def _keyring() -> KeyRing:
    return KeyRing(
        [KeyEntry(kid="test", key=KEY, state="active")], service="personal_data_mcp"
    )


def _event(
    event_id: str,
    *,
    timezone_name: str | None = None,
    all_day: bool = False,
) -> dict:
    """One uploaded event, in either wire shape.

    `timezone_name` is what a v2 client adds and a v1 client cannot (design
    5.2). It is *not* what the barrier reads -- the barrier compares the
    declared protocol version, precisely so a client cannot change its
    admission by changing its payload -- so a test that sends the v2 shape with
    the v1 declaration is testing the case that matters.
    """
    event = {
        "event_identifier": event_id,
        "calendar_identifier": "cal-1",
        "title": "网球",
        "start": "2026-09-07T15:00:00+08:00",
        "end": "2026-09-07T16:30:00+08:00",
        "all_day": all_day,
        "location": None,
        "notes": None,
        "last_modified": "2026-09-06T20:00:00+08:00",
    }
    if timezone_name is not None:
        event["timezone"] = timezone_name
    return event


@pytest.fixture()
def sessions(tmp_path: Path):
    """A database at head, so migration 0009 has inserted the policy row.

    Migrated rather than `create_all`, because the deployed database is built
    by migrations and these tests are about the deployed shape. The row is
    deleted by hand in one test below to exercise the reader's fail-closed
    path.
    """
    engine = create_database_engine(tmp_path / "barrier.sqlite")
    db.upgrade(engine, "head")
    yield session_factory(engine)
    engine.dispose()


def _ingest(
    sessions,
    events=(),
    *,
    version: int = 1,
    device_id: str = "dev-1",
    as_of: datetime | None = None,
    window_complete: bool = True,
):
    return ingest_events(
        {
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
            "events": list(events),
            "window_complete": window_complete,
            "snapshot_as_of": to_rfc3339(as_of if as_of is not None else REBUILD_AT),
        },
        sessions=sessions,
        keyring=_keyring(),
        device_id=device_id,
        client_wire_version=version,
        now=NOW,
    )


def _arm_rebuild(sessions, *, device_id: str = "dev-1") -> None:
    """Steps (a)-(c) of the controlled recovery: stop, rebuild, stay stopped.

    Ingest is left in maintenance, which is the state the runbook actually
    produces -- step (d) re-opens it, and the design keeps that a separate,
    deliberate act. A test that wants the channel open has to say so by
    calling `_reopen`, which is the step an operator performs.
    """
    with sessions() as session:
        policy.begin_rebuild(session, device_id=device_id, now=REBUILD_AT)
        session.commit()


def _reopen(sessions) -> None:
    """Step (d): the operator re-opens ingest, still behind the ratchet."""
    with sessions() as session:
        policy.leave_maintenance(session, now=NOW)
        session.commit()


def _policy(sessions) -> CalendarIngestPolicy:
    with sessions() as session:
        row = policy.read_policy(session)
        return CalendarIngestPolicy(
            policy_id=row.policy_id,
            min_ingest_protocol=row.min_ingest_protocol,
            ingest_mode=row.ingest_mode,
            updated_at=row.updated_at,
        )


def _device(sessions, device_id: str = "dev-1") -> CalendarDeviceSync | None:
    with sessions() as session:
        row = session.get(CalendarDeviceSync, device_id)
        if row is None:
            return None
        return CalendarDeviceSync(
            device_id=row.device_id,
            watermark_ts=row.watermark_ts,
            window_start_ts=row.window_start_ts,
            window_end_ts=row.window_end_ts,
            updated_at=row.updated_at,
            rebuild_pending=row.rebuild_pending,
            rebuild_instant=row.rebuild_instant,
        )


def _event_count(sessions) -> int:
    with sessions() as session:
        return session.query(CalendarEvent).count()


def _refusal(callable_) -> AppError:
    with pytest.raises(AppError) as excinfo:
        callable_()
    return excinfo.value


# --- the migration's row ----------------------------------------------------


def test_the_migration_leaves_exactly_one_policy_row(sessions) -> None:
    with sessions() as session:
        rows = session.execute(
            text(
                "SELECT policy_id, min_ingest_protocol, ingest_mode "
                "FROM calendar_ingest_policy"
            )
        ).all()
    assert rows == [(1, 1, "normal")]


def test_ingest_fails_closed_when_the_policy_row_is_gone(sessions) -> None:
    """The one fail-open shape: no row reads as "no floor" and v1 is let in.

    Nothing deletes this row -- the migration writes it and no code path
    touches it -- so the test deletes it by hand, which is exactly what a
    restored snapshot missing the row, or a future "cleanup", would look like.
    """
    with sessions() as session:
        session.execute(text("DELETE FROM calendar_ingest_policy"))
        session.commit()
    error = _refusal(lambda: _ingest(sessions, [_event("ev-1")]))
    assert error.code is ErrorCode.INTERNAL_ERROR
    assert _event_count(sessions) == 0


def test_a_schema_built_without_migrations_carries_the_policy_row(tmp_path) -> None:
    """`create_all` is the other way a Finance database comes into existence.

    It is not a test-only surface: the Finance write CLI calls it at startup,
    and the loopback fixture that runs the **real MCP process** builds its
    calendar database with it. Both therefore used to produce a shape the
    migration never produces -- the policy table present with no row in it --
    and every calendar upload failed closed at 500. The offline barrier suite
    migrated, so it could not see the difference; the composition test that
    drives the real process over a socket is what caught it.

    So the invariant is asserted where it was broken: the row has to exist, and
    a first batch has to be accepted through the same schema.
    """
    engine = create_database_engine(tmp_path / "created.sqlite")
    create_all(engine)
    try:
        sessions = session_factory(engine)
        with sessions() as session:
            row = policy.read_policy(session)
            assert (row.min_ingest_protocol, row.ingest_mode) == (1, "normal")
        result = _ingest(sessions, [_event("ev-1")])
        assert result["upserted"] == 1
    finally:
        engine.dispose()


def test_migration_0009_round_trips(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "round-trip.sqlite")
    db.upgrade(engine, "head")
    inspector = sa_inspect(engine)
    assert "calendar_ingest_policy" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("calendar_device_sync")}
    assert {"rebuild_pending", "rebuild_instant"} <= columns

    db.downgrade(engine, "0008_calendar_directory_retirement")
    inspector = sa_inspect(engine)
    assert "calendar_ingest_policy" not in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("calendar_device_sync")}
    assert not {"rebuild_pending", "rebuild_instant"} & columns

    # Up again, and the row is back: the ratchet's storage is part of the
    # schema, so descending and re-ascending cannot leave a database whose
    # ingest would refuse to run.
    db.upgrade(engine, "head")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM calendar_ingest_policy")
        ).scalar_one() == 1
    engine.dispose()


# --- the compatibility matrix ------------------------------------------------


def test_before_a_rebuild_both_declared_versions_are_accepted(sessions) -> None:
    """The pre-rebuild state is unchanged: the barrier must not narrow the
    channel for a client that never had a version to declare."""
    _ingest(sessions, [_event("ev-1")], version=1)
    _ingest(sessions, [_event("ev-2")], version=2)
    assert _event_count(sessions) == 2


def test_after_a_rebuild_a_declared_v1_client_is_refused(sessions) -> None:
    _arm_rebuild(sessions)
    error = _refusal(lambda: _ingest(sessions, [_event("ev-1")], version=1))
    assert error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert _event_count(sessions) == 0


def test_a_rebuilt_mirror_refuses_everything_until_it_is_reopened(sessions) -> None:
    """Between step (c) and step (d) the channel is shut, v2 included.

    Worth pinning because it is the state an operator sits in while verifying
    the empty tables: a rebuild that silently re-opened the channel would put
    the verification window back under upload traffic.
    """
    _arm_rebuild(sessions)
    error = _refusal(
        lambda: _ingest(
            sessions, [_event("ev-1", timezone_name="Asia/Shanghai")], version=2
        )
    )
    assert error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert _event_count(sessions) == 0


def test_after_a_rebuild_a_declared_v2_client_is_accepted(sessions) -> None:
    """The floor closes v1 and *opens* nothing else: v2 is what the rebuilt
    mirror is meant to be rebuilt from, so a floor that refused it too would
    be a mirror nothing can repopulate."""
    _arm_rebuild(sessions)
    _reopen(sessions)
    result = _ingest(sessions, [_event("ev-1", timezone_name="Asia/Shanghai")], version=2)
    assert result["status"] == "ok"
    assert _event_count(sessions) == 1


def test_the_payload_cannot_upgrade_a_client(sessions) -> None:
    """A v2-shaped body sent by a client declaring 1 is still refused.

    This is the shape R4-F16 killed: a v1 upload carries no field an old ingest
    can arbitrate on, so admission is decided by the *declared* version and
    never by what the body happens to contain. A device that could promote
    itself by editing its own payload has no barrier in front of it.
    """
    _arm_rebuild(sessions)
    error = _refusal(
        lambda: _ingest(
            sessions,
            [_event("ev-1", timezone_name="Asia/Shanghai")],
            version=1,
        )
    )
    assert error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert _event_count(sessions) == 0


def test_a_refused_batch_writes_nothing_at_all(sessions) -> None:
    """`app.py` promises no execution record before verification; the same
    shape holds here. A refusal that left a watermark would be a refusal that
    changed the mirror."""
    _arm_rebuild(sessions)
    _refusal(lambda: _ingest(sessions, [_event("ev-1")], version=1))
    assert _event_count(sessions) == 0
    device = _device(sessions)
    assert device.watermark_ts == policy.NO_WATERMARK_TS
    assert device.window_start_ts is None
    assert device.window_end_ts is None


def test_a_refused_batch_writes_no_directory_rows_either(sessions) -> None:
    """The directory rides in the same unit as the events (design 2.1), so a
    gate that let it through would leave a refused batch's calendars named in
    the lookup a create routes through -- state from a batch that was told it
    wrote nothing."""
    _arm_rebuild(sessions)
    with pytest.raises(AppError):
        ingest_events(
            {
                "window_start": WINDOW_START,
                "window_end": WINDOW_END,
                "events": [_event("ev-1")],
                "window_complete": True,
                "snapshot_as_of": to_rfc3339(REBUILD_AT),
                "calendars": [
                    {"calendar_identifier": "cal-1", "title": "网球", "is_subscribed": True}
                ],
            },
            sessions=sessions,
            keyring=_keyring(),
            device_id="dev-1",
            client_wire_version=1,
            now=NOW,
        )
    with sessions() as session:
        assert session.execute(
            text("SELECT COUNT(*) FROM calendar_directory")
        ).scalar_one() == 0


def test_the_redundant_rebuild_line_holds_when_the_floor_is_somehow_low(sessions) -> None:
    """`rebuild_pending`'s refusal is redundant while the ratchet is up, and the
    redundancy is the point: this branch is what a restored snapshot or a
    hand-edited floor leaves standing. It cannot be reached through this
    module's own transitions -- `begin_rebuild` raises both -- so the state is
    built by hand, which is exactly how it would arise in production.
    """
    _arm_rebuild(sessions)
    with sessions() as session:
        session.execute(text("UPDATE calendar_ingest_policy SET min_ingest_protocol = 1"))
        session.commit()
    assert _policy(sessions).min_ingest_protocol == policy.PROTOCOL_BEFORE_REBUILD
    assert _device(sessions).rebuild_pending is True

    error = _refusal(lambda: _ingest(sessions, [_event("ev-1")], version=1))
    assert error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert _event_count(sessions) == 0


def test_completing_a_rebuild_for_an_unknown_device_is_an_error(sessions) -> None:
    """"Nothing to clear" and "the row was never created" are the same state,
    and only one of them is reachable by design. Raising keeps a real bug from
    reading as a quiet pass."""
    with sessions() as session:
        with pytest.raises(AppError) as excinfo:
            policy.complete_rebuild(session, device_id="dev-never-seen")
        session.rollback()
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


# --- maintenance -------------------------------------------------------------


def test_maintenance_refuses_both_declared_versions(sessions) -> None:
    with sessions() as session:
        policy.enter_maintenance(session, now=NOW)
        session.commit()
    assert _refusal(lambda: _ingest(sessions, [_event("ev-1")], version=1)).code is (
        ErrorCode.SOURCE_UNAVAILABLE
    )
    assert _refusal(
        lambda: _ingest(sessions, [_event("ev-2", timezone_name="Asia/Shanghai")], version=2)
    ).code is ErrorCode.SOURCE_UNAVAILABLE
    assert _event_count(sessions) == 0


def test_maintenance_switch_round_trips_and_moves_nothing_else(sessions) -> None:
    with sessions() as session:
        policy.enter_maintenance(session, now=NOW)
        session.commit()
    assert _policy(sessions).ingest_mode == policy.MODE_MAINTENANCE
    with sessions() as session:
        policy.leave_maintenance(session, now=NOW)
        session.commit()
    assert _policy(sessions).ingest_mode == policy.MODE_NORMAL
    # The switch is a switch. Leaving maintenance re-opens the channel to the
    # clients the floor admits, and the floor is not one of the things it moves.
    assert _policy(sessions).min_ingest_protocol == policy.PROTOCOL_BEFORE_REBUILD


# --- the ratchet is one-way --------------------------------------------------


def test_no_policy_transition_lowers_the_ratchet(sessions) -> None:
    """Every public transition, applied after a rebuild, leaves the floor at 2.

    The list is pinned by the guard below rather than hand-checked, because the
    realistic defect is a transition added later that quietly resets the floor
    -- which is the operation design 14.2 removed the *entry point* for.
    """
    _arm_rebuild(sessions)
    for name in TRANSITIONS:
        with sessions() as session:
            if name == "begin_rebuild":
                policy.begin_rebuild(session, device_id="dev-1", now=NOW)
            elif name == "complete_rebuild":
                policy.complete_rebuild(session, device_id="dev-1")
            else:
                getattr(policy, name)(session, now=NOW)
            session.commit()
        assert _policy(sessions).min_ingest_protocol == (
            policy.PROTOCOL_AFTER_REBUILD
        ), name


def test_every_policy_transition_is_in_the_ratchet_test() -> None:
    """The guard that makes the test above exhaustive.

    A new public function in `policy` that writes the floor would be covered by
    neither test until someone remembered this file. Adding one now fails here
    instead.
    """
    public = {
        name
        for name, member in vars(policy).items()
        if not name.startswith("_")
        and inspect.isfunction(member)
        and member.__module__ == policy.__name__
    }
    assert public == set(TRANSITIONS) | {"read_policy", "check_ingest_allowed"}


def test_a_successful_v2_first_batch_does_not_reopen_v1(sessions) -> None:
    """R7-F22, first must-add.

    The legal sequence the earlier revision got wrong: rebuild, a v2 batch that
    succeeds but does not complete its window (so no watermark), then the old
    v1 window's last batch arrives. The epoch -- or here, the version -- proves
    the batch that carries it and cannot vouch for a request that does not.
    """
    _arm_rebuild(sessions)
    _reopen(sessions)
    _ingest(
        sessions,
        [_event("ev-new", timezone_name="Asia/Shanghai")],
        version=2,
        window_complete=False,
    )
    error = _refusal(lambda: _ingest(sessions, [_event("ev-old")], version=1))
    assert error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert _policy(sessions).min_ingest_protocol == policy.PROTOCOL_AFTER_REBUILD
    # No watermark: the v2 batch completed nothing, and the v1 batch was
    # refused rather than allowed to complete the window it was half of.
    assert _device(sessions).watermark_ts == policy.NO_WATERMARK_TS


def test_a_completed_rebuild_does_not_reopen_v1(sessions) -> None:
    """R7-F22, second must-add.

    The mirror is genuinely rebuilt here -- a v2 complete window wrote the
    watermark and cleared `rebuild_pending` -- and the old v1 shape is still
    refused. This is the test that separates the two states: clearing the
    prompt is not re-opening the channel.
    """
    _arm_rebuild(sessions)
    _reopen(sessions)
    _ingest(
        sessions,
        [_event("ev-new", timezone_name="Asia/Shanghai")],
        version=2,
        as_of=NOW - timedelta(minutes=2),
    )
    device = _device(sessions)
    assert device.rebuild_pending is False
    assert device.watermark_ts == int((NOW - timedelta(minutes=2)).timestamp())

    error = _refusal(lambda: _ingest(sessions, [_event("ev-old")], version=1))
    assert error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert _policy(sessions).min_ingest_protocol == policy.PROTOCOL_AFTER_REBUILD
    assert device.rebuild_instant is not None


def test_complete_rebuild_clears_only_the_flag(sessions) -> None:
    _arm_rebuild(sessions)
    with sessions() as session:
        policy.complete_rebuild(session, device_id="dev-1")
        session.commit()
    device = _device(sessions)
    assert device.rebuild_pending is False
    # The warning stamp outlives the prompt on purpose: it is what makes "an
    # old window arrived after the recovery" readable to an operator later.
    assert device.rebuild_instant == int(REBUILD_AT.timestamp())
    assert _policy(sessions).min_ingest_protocol == policy.PROTOCOL_AFTER_REBUILD


def test_begin_rebuild_creates_a_row_for_a_device_that_never_synced(sessions) -> None:
    """A device that enrolled and never completed a snapshot has no row, and
    still has to read as mid-rebuild -- otherwise the first window it completes
    looks like a first-ever sync and nobody is told the mirror was rebuilt."""
    _arm_rebuild(sessions, device_id="dev-fresh")
    device = _device(sessions, "dev-fresh")
    assert device is not None
    assert device.rebuild_pending is True
    assert device.watermark_ts == policy.NO_WATERMARK_TS


def test_the_rebuild_clears_the_coverage_bounds(sessions) -> None:
    """A watermark's bounds say which window it vouched for. A zeroed watermark
    that kept them would leave a query reading the pre-rebuild window as
    covered -- the one claim the rebuild made false (second review F7)."""
    _ingest(sessions, [_event("ev-1")], version=1)
    assert _device(sessions).window_start_ts is not None
    _arm_rebuild(sessions)
    device = _device(sessions)
    assert device.window_start_ts is None
    assert device.window_end_ts is None


# --- the warning is not a predicate -----------------------------------------


def test_a_zero_skew_old_batch_is_refused(sessions, caplog) -> None:
    """R6-F20's counterexample, as a test.

    The clock has zero skew: the old window's snapshot instant is *exactly* the
    rebuild instant. Any tolerance-shaped rule admits this batch (a tolerance
    wide enough for a slow device clock is wide enough for it), which is why
    the version decides and the instant only gets logged. v1 here means the
    batch is refused -- and the log says why it was worth knowing.
    """
    _arm_rebuild(sessions)
    with caplog.at_level("WARNING", logger="personal_data_mcp.calendar.ingest"):
        error = _refusal(
            lambda: _ingest(sessions, [_event("ev-old")], version=1, as_of=REBUILD_AT)
        )
    assert error.code is ErrorCode.SOURCE_UNAVAILABLE
    assert any(
        "captured at or before this device's rebuild" in record.message
        for record in caplog.records
    )


def test_the_warning_does_not_block_an_admitted_batch(sessions, caplog) -> None:
    """The same instant, the same warning, and a v2 client is let through.

    One test cannot pin "time is not a predicate": the pair is what does it --
    identical `snapshot_as_of`, opposite outcomes, decided by the declaration
    alone.
    """
    _arm_rebuild(sessions)
    _reopen(sessions)
    with caplog.at_level("WARNING", logger="personal_data_mcp.calendar.ingest"):
        result = _ingest(
            sessions,
            [_event("ev-new", timezone_name="Asia/Shanghai")],
            version=2,
            as_of=REBUILD_AT,
        )
    assert result["status"] == "ok"
    assert any(
        "captured at or before this device's rebuild" in record.message
        for record in caplog.records
    )


def test_an_old_batch_refused_by_maintenance_is_still_logged(sessions, caplog) -> None:
    """The warning's whole purpose is telling an operator that the controlled
    recovery did not take. A batch that is refused is exactly that evidence, so
    the warning has to be emitted before the gate, not after it."""
    _arm_rebuild(sessions)
    with caplog.at_level("WARNING", logger="personal_data_mcp.calendar.ingest"):
        _refusal(lambda: _ingest(sessions, [_event("ev-old")], version=1, as_of=REBUILD_AT))
    assert any(
        "captured at or before this device's rebuild" in record.message
        for record in caplog.records
    )


def test_a_batch_after_the_rebuild_is_not_warned_about(sessions, caplog) -> None:
    """Negative control for the three above: a warning that fires on everything
    is not evidence of anything."""
    _arm_rebuild(sessions)
    _reopen(sessions)
    with caplog.at_level("WARNING", logger="personal_data_mcp.calendar.ingest"):
        _ingest(
            sessions,
            [_event("ev-new", timezone_name="Asia/Shanghai")],
            version=2,
            as_of=NOW,
        )
    assert not [
        record
        for record in caplog.records
        if "captured at or before this device's rebuild" in record.message
    ]
