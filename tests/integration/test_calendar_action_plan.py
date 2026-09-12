"""One message, several calendar events: the frozen action plan (design 4.1/4.2).

"下周一10点牙医，下午3点理发" is one message and two events. The model answers
with one call per event, and the design has to answer two questions the
single-action path never had to:

- **What is written down, and when?** The whole turn's call list is frozen
  *before* any of it is issued, so a crash in the middle cannot re-ask the model
  and come back with a different list under the same keys (review R1-F5).
- **What happens when one item cannot be issued?** All-or-nothing (Henson,
  2026-09-10). A row carries one state, and "an action may already be with the
  phone" cannot sit beside "waiting for the user to answer" -- so pass 1 issues
  nothing at all, the failing item's outcome is applied to the *message's own*
  operation, and the user's answer re-runs the whole turn. From issuance
  onwards each item is independent, which is where the design's "任一项澄清/失败不影响
  其他项" holds: each settles through its own report and its own receipt.

The message's own operation is item 0 (`plan_key` = its own idempotency key), so
the single-action message -- the common case -- has no plan key at all and
behaves exactly as it did before.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import pytest
from context_envelopes import envelope_for
from test_calendar_device_action import (
    CAL_ARGS,
    CLIENT_WIRE_V1,
    CLIENT_WIRE_V2,
    FakeControl,
    SpyBridge,
    _action_keyring,
    cal_dispatcher,
    op_session,
)

from personal_agent.api.app import _operation_projection
from personal_agent.api.calendar_issue import CLIENT_UPGRADE_QUESTION
from personal_agent.api.control_client import CalendarResolved, CalendarUnresolved
from personal_agent.api.device_action_projection import open_device_action
from personal_agent.api.intent import WriteIntent
from personal_agent.api.operation_store import (
    join_action_plan,
    open_operation,
    plan_item_key,
    plan_operations,
    transition_operation,
)
from personal_agent.api.orchestrator import (
    ToolCall,
    ToolCalls,
    resume_action_plan,
    run_operation,
)
from personal_agent.api.recovery import RECOVERY_QUIET_PERIOD, apply_recovery
from personal_agent.storage.models import Device, Operation
from personal_agent_core.errors import AppError, ErrorCode, ModelFailureReason

#: The second event of the same message: a different thing, on a different day,
#: in a different calendar. Two items that differed only in a field the wire
#: drops would prove nothing about ordering, and the calendar is what the
#: server routes per item -- so the two calls name two names.
DENTIST = {
    **CAL_ARGS,
    "title": "牙医",
    "calendar": "工作",
    "start": "2026-09-13T10:00:00+08:00",
    "end": "2026-09-13T11:00:00+08:00",
}

#: What the device's directory says each name is.
CALENDARS = {
    "日常安排": CalendarResolved(
        calendar_identifier="uuid-ri-chang", calendar_title="日常安排"
    ),
    "工作": CalendarResolved(
        calendar_identifier="uuid-gong-zuo", calendar_title="工作"
    ),
}


@cache
def _envelope(text: str):
    """One real, budget-validated envelope per distinct message.

    Built by the production `ContextBuilder`, like every other orchestrator
    test: a hand-made envelope would let these tests pass against a shape the
    builder never produces.
    """
    return envelope_for(Path(tempfile.mkdtemp()), user_text=text)


def allow(*, tool, model_args):
    return dict(model_args)


def _two_calls() -> ToolCalls:
    return ToolCalls(
        calls=(
            ToolCall(tool="calendar.create_event", model_args=dict(CAL_ARGS)),
            ToolCall(tool="calendar.create_event", model_args=dict(DENTIST)),
        )
    )


def _titles(actions) -> list[str]:
    return [action["event"]["title"] for action in actions]


class PerNameControl(FakeControl):
    """The control plane answering differently for each calendar name.

    The all-or-nothing cases turn on one item routing and another not, which a
    single fixed resolution cannot express -- and it is the *calendar name* the
    server routes on, never the event title.
    """

    def __init__(self, answers: dict[str, object]) -> None:
        super().__init__()
        self._answers = answers

    async def resolve_calendar(self, *, device_id: str, title: str):
        self.lookups.append((device_id, title))
        return self._answers[title]


def _unresolved(reason: str) -> CalendarUnresolved:
    return CalendarUnresolved(reason=reason, candidates=())


def _dispatcher(answers: dict[str, object] | None = None) -> object:
    return cal_dispatcher(SpyBridge(), PerNameControl(answers or dict(CALENDARS)))


class FakeInterpreter:
    def __init__(self, calls: ToolCalls) -> None:
        self._calls = calls

    def interpret(self, *, envelope):
        return self._calls


def _run_turn(session, operation, calls: ToolCalls, dispatcher, keyring, now):
    return run_operation(
        session,
        operation,
        build_context=lambda: _envelope("下周一10点牙医，下午3点理发"),
        interpreter=FakeInterpreter(calls),
        dispatcher=dispatcher,
        authorize=allow,
        keyring=keyring,
        action_keyring=keyring,
        now=now,
    )


#: When the fixture's rows were written. One instant for the whole file, so a
#: test that needs a *quiet* row says so by moving the clock forward by the
#: recovery period rather than by picking a later date.
STAMP = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


def _device(session) -> None:
    """The enrolled phone every operation in this file belongs to."""
    session.add(
        Device(
            device_id="device-1",
            display_name="iPhone",
            public_key="K",
            device_key_thumbprint="T",
            status="active",
            scopes='["calendar.event.write"]',
            allowed_tools_version="atv-1",
            created_at=STAMP,
        )
    )
    session.flush()


def _message(session, key: str = "req-1") -> Operation:
    """A message as the API records it: written down, nothing decided yet.

    A turn driven through `run_operation` starts where a real request does. The
    device-action fixture's own operation is born at `dispatching` because it is
    fed straight to `_apply_resolve`; that state is what the freeze reaches, not
    what a message starts at.
    """
    if session.query(Device).count() == 0:
        _device(session)
    operation = open_operation(
        session,
        device_id="device-1",
        client_request_id=key,
        request_fingerprint=f"fp-{key}",
        now=STAMP,
    ).operation
    session.flush()
    return operation


# --- the message that asks for two things ------------------------------------


def test_two_events_are_frozen_then_parked_in_the_order_they_were_asked(
    op_session,
) -> None:
    """The happy path, driven through the real entry point.

    Order is the property being pinned: the phone creates the events in the
    order the user said them, and the plan's positions are what the derived
    keys are built from, so a list that arrived shuffled would issue
    different-keyed events than the ones the user's sentence describes. The
    routing is per item too -- two calendars in one message must not share one
    resolved identifier.
    """
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        bridge = SpyBridge()
        result = _run_turn(
            session,
            operation,
            _two_calls(),
            cal_dispatcher(bridge, PerNameControl(dict(CALENDARS))),
            keyring,
            now,
        )

        assert result.state == "source_in_progress"
        assert _titles(result.device_actions) == ["网球", "牙医"]
        assert [
            action["event"]["calendar_identifier"]
            for action in result.device_actions
        ] == ["uuid-ri-chang", "uuid-gong-zuo"]
        # Item 0 is the message's own operation, so its action carries the key
        # the client already knows the message by; every later item is named by
        # the position it was frozen at.
        assert [action["action_id"] for action in result.device_actions] == [
            operation.idempotency_key,
            plan_item_key(operation.idempotency_key, 1),
        ]
        assert bridge.executed == []

        rows = plan_operations(session, operation.idempotency_key)
        assert [row.plan_index for row in rows] == [0, 1]
        assert rows[0].operation_id == operation.operation_id
        assert [row.state for row in rows] == [
            "source_in_progress",
            "source_in_progress",
        ]
        assert [row.idempotency_key for row in rows] == [
            operation.idempotency_key,
            plan_item_key(operation.idempotency_key, 1),
        ]
        # Each row carries its own seal, so each item is deliverable and
        # resumable on its own -- which is the whole point of the freeze.
        for row, expected in zip(rows, ("网球", "牙医"), strict=True):
            assert row.tool == "calendar.create_event"
            assert row.encrypted_device_action is not None
            assert row.encrypted_request is not None
            opened = open_device_action(
                keyring,
                operation_id=row.operation_id,
                envelope=row.encrypted_device_action,
            )
            assert opened is not None
            assert opened["event"]["title"] == expected


# --- all-or-nothing ----------------------------------------------------------


def test_an_item_that_cannot_be_routed_withholds_the_whole_turn(op_session) -> None:
    """Nothing is written down when any item cannot be issued.

    The message's own operation takes the question, and no plan row exists --
    so the user's answer re-runs the turn from scratch rather than resuming a
    half-issued list whose remaining arguments have been sitting still. The
    item that *could* have been routed was not parked either.
    """
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        result = _run_turn(
            session,
            operation,
            _two_calls(),
            _dispatcher({**CALENDARS, "工作": _unresolved("not_found")}),
            keyring,
            now,
        )

        assert result.state == "waiting_for_clarification"
        assert "找不到名为「工作」" in result.clarification
        session.refresh(operation)
        assert operation.state == "waiting_for_clarification"
        assert operation.safe_result == result.clarification
        assert operation.plan_key is None
        assert operation.encrypted_device_action is None
        assert session.query(Operation).count() == 1
        assert plan_operations(session, operation.idempotency_key) == []


def test_the_first_item_failing_withholds_the_turn_the_same_way(op_session) -> None:
    """Which item fails changes nothing, and the user hears about that one.

    Reporting the first unmet item keeps the sentence the user reads about the
    thing they said first; the turn is withheld either way, so this is about
    which question is useful, not about what was written.
    """
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        result = _run_turn(
            session,
            operation,
            _two_calls(),
            _dispatcher({**CALENDARS, "日常安排": _unresolved("read_only")}),
            keyring,
            now,
        )

        assert result.state == "waiting_for_clarification"
        assert "「日常安排」在你的 iPhone 上是只读的" in result.clarification
        assert session.query(Operation).count() == 1
        session.refresh(operation)
        assert operation.state == "waiting_for_clarification"


def test_a_denied_item_withholds_the_whole_turn_before_anything_is_frozen(
    op_session,
) -> None:
    """Authorisation is part of pass 1, so a denial stops the turn like any
    other unmet item -- and it stops it *before* the list is written, which is
    what keeps a policy refusal from leaving parked rows behind."""
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        authorized: list[str] = []

        def deny_the_dentist(*, tool, model_args):
            authorized.append(model_args["title"])
            if model_args["title"] == "牙医":
                raise AppError(ErrorCode.SCOPE_DENIED, internal_detail="denied")
            return dict(model_args)

        result = run_operation(
            session,
            operation,
            build_context=lambda: _envelope("下周一10点牙医，下午3点理发"),
            interpreter=FakeInterpreter(_two_calls()),
            dispatcher=_dispatcher(),
            authorize=deny_the_dentist,
            keyring=keyring,
            action_keyring=keyring,
            now=now,
        )

        assert result.state == "failed_safe"
        assert result.failure_reason == "policy_denied"
        # Both items were authorised before either was resolved: the loop is per
        # item, so a denial on the second is still seen before any write.
        assert authorized == ["网球", "牙医"]
        assert session.query(Operation).count() == 1
        session.refresh(operation)
        assert operation.plan_key is None


@pytest.mark.parametrize(
    "other_tool", ["finance.log_expense", "agent.ask_clarification"]
)
def test_a_list_that_is_not_all_device_executed_keeps_its_refusal(
    op_session, other_tool: str
) -> None:
    """Several calls are a plan only when the phone executes all of them.

    Anything else -- a connector write, a read beside a write, a control call,
    a tool the catalog does not have -- keeps the refusal a multi-call response
    always got: there is no ordering of those that is safe.
    """
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        bridge = SpyBridge()
        calls = ToolCalls(
            calls=(
                ToolCall(tool="calendar.create_event", model_args=dict(CAL_ARGS)),
                ToolCall(tool=other_tool, model_args={}),
            )
        )
        result = _run_turn(
            session, operation, calls, cal_dispatcher(bridge), keyring, now
        )

        assert result.state == "failed_safe"
        assert result.failure_reason == ModelFailureReason.RESPONSE_AMBIGUOUS.value
        # Refused before anything was authorised, resolved or written.
        assert bridge.authorized == []
        assert bridge.executed == []
        assert session.query(Operation).count() == 1
        session.refresh(operation)
        assert operation.state == "failed_safe"
        assert operation.plan_key is None


def test_an_old_client_is_told_to_upgrade_instead_of_freezing_a_plan(
    op_session,
) -> None:
    """The issuance gate answers *before* the list is written down too.

    The gate is decided per item in pass 1, so a client that cannot implement
    the action gets the upgrade question and no rows -- not a frozen plan whose
    items could never be handed over.
    """
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session, key="req-v1")
        result = _run_turn(
            session,
            operation,
            _two_calls(),
            cal_dispatcher(SpyBridge(), client_wire_version=CLIENT_WIRE_V1),
            keyring,
            now,
        )

        assert result.state == "waiting_for_clarification"
        assert result.clarification == CLIENT_UPGRADE_QUESTION
        session.refresh(operation)
        assert operation.plan_key is None
        assert plan_operations(session, operation.idempotency_key) == []


# --- the freeze, and continuing from it --------------------------------------


def _freeze(session, operation, keyring, now) -> list[Operation]:
    """Write the list down and stop, the way a crash in that window would.

    The freeze is the seam: everything before it issues nothing, and everything
    after it can be redone from the rows. Stopping here is exactly the state a
    process death between two items leaves behind.
    """
    from personal_agent.api.orchestrator import _freeze_action_plan, _step

    # The turn's own walk up to the freeze, in the order the orchestrator takes
    # it: interpretation is committed, then the list is written down.
    _step(session, operation, "interpreting", now)
    _step(session, operation, "dispatching", now, tool="calendar.create_event")
    return _freeze_action_plan(
        session,
        operation,
        plan_key=operation.idempotency_key,
        intents=[
            WriteIntent(tool=call.tool, model_args=dict(call.model_args))
            for call in _two_calls().calls
        ],
        action_keyring=keyring,
        now=now,
    )


def _park_item_zero(session, row, keyring, now) -> None:
    """Park the plan's first item, the way a turn that died after it would."""
    from personal_agent.api.orchestrator import DeviceActionIssued, _apply_resolve

    _apply_resolve(
        session,
        row,
        DeviceActionIssued(
            action_id=row.idempotency_key,
            tool="calendar.create_event",
            wire_version=CLIENT_WIRE_V2,
            event_fields=dict(CAL_ARGS),
        ),
        dispatcher=None,
        keyring=keyring,
        now=now,
        action_keyring=keyring,
        intent=WriteIntent(tool="calendar.create_event", model_args=dict(CAL_ARGS)),
    )


def test_a_frozen_plan_resumes_from_its_own_rows_without_a_model(op_session) -> None:
    """The freeze's whole purpose: the second item's arguments come from its own
    row, so a crash between two items cannot re-ask the model and get a
    different list under the same keys."""
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        rows = _freeze(session, operation, keyring, now)
        assert [row.state for row in rows] == ["dispatching", "dispatching"]
        assert all(row.encrypted_request is not None for row in rows)

        bridge = SpyBridge()
        result = resume_action_plan(
            session,
            operation,
            dispatcher=cal_dispatcher(bridge, PerNameControl(dict(CALENDARS))),
            authorize=allow,
            keyring=keyring,
            action_keyring=keyring,
            now=now,
        )

        assert result is not None
        assert result.state == "source_in_progress"
        # The titles are the frozen ones, and neither item went through a model
        # or a connector to get here.
        assert _titles(result.device_actions) == ["网球", "牙医"]
        assert [action["action_id"] for action in result.device_actions] == [
            operation.idempotency_key,
            plan_item_key(operation.idempotency_key, 1),
        ]
        assert bridge.executed == []
        assert [row.state for row in plan_operations(session, operation.plan_key)] == [
            "source_in_progress",
            "source_in_progress",
        ]


def test_a_resume_with_nothing_left_to_do_is_a_no_op(op_session) -> None:
    """A replay calls this on every message, so it has to answer `None` for
    everything that is not an unfinished plan: a finished one, and every
    ordinary operation that never had a plan key."""
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        _run_turn(
            session, operation, _two_calls(), _dispatcher(), keyring, now
        )
        session.refresh(operation)
        assert operation.plan_key is not None

        def resume(target):
            return resume_action_plan(
                session,
                target,
                dispatcher=_dispatcher(),
                authorize=allow,
                keyring=keyring,
                action_keyring=keyring,
                now=now,
            )

        assert resume(operation) is None
        assert resume(_message(session, key="req-2")) is None


def test_a_plan_that_never_got_an_action_is_not_resurrected(op_session) -> None:
    """A crash inside the freeze window, seen by the recovery scan.

    Both rows sat at `dispatching` and no action was ever sealed for either, so
    neither reached the phone: recovery resolves them zero-write, and the resume
    a replay triggers finds nothing to continue. Nothing is written twice and
    nothing is parked forever.
    """
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        rows = _freeze(session, operation, keyring, now)

        for row in rows:
            apply_recovery(
                session,
                row,
                None,
                now=now + RECOVERY_QUIET_PERIOD,
                quiet=True,
                executor="device",
            )

        assert [row.state for row in plan_operations(session, operation.plan_key)] == [
            "failed_safe",
            "failed_safe",
        ]
        assert (
            resume_action_plan(
                session,
                operation,
                dispatcher=_dispatcher(),
                authorize=allow,
                keyring=keyring,
                action_keyring=keyring,
                now=now,
            )
            is None
        )


def test_recovery_leaves_an_already_parked_sibling_alone(op_session) -> None:
    """One item parked, one item never issued: the sweep settles the one that
    was never handed over and does not touch the one the phone may already be
    writing. The plan is left visibly partial, which is the honest shape -- a
    late failure cannot un-issue an action that already left the building."""
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        rows = _freeze(session, operation, keyring, now)

        _park_item_zero(session, rows[0], keyring, now)
        assert rows[0].state == "source_in_progress"
        assert rows[1].state == "dispatching"

        apply_recovery(
            session,
            rows[1],
            None,
            now=now + RECOVERY_QUIET_PERIOD,
            quiet=True,
            executor="device",
        )

        session.refresh(rows[1])
        assert rows[1].state == "failed_safe"
        assert rows[1].encrypted_device_action is None
        session.refresh(rows[0])
        assert rows[0].state == "source_in_progress"
        assert rows[0].encrypted_device_action is not None
        assert (
            resume_action_plan(
                session,
                operation,
                dispatcher=_dispatcher(),
                authorize=allow,
                keyring=keyring,
                action_keyring=keyring,
                now=now,
            )
            is None
        )


def test_a_resume_that_loses_one_item_leaves_the_plan_visibly_partial(
    op_session,
) -> None:
    """Policy can move between the freeze and the resume -- the kill switch, the
    device's scopes, the calendar's writability. A row denied on resume fails
    safe on its own, its sibling is issued, and a second resume is a no-op
    rather than a retry."""
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        _freeze(session, operation, keyring, now)

        def allow_only_the_first(*, tool, model_args):
            if model_args["title"] == "牙医":
                raise AppError(ErrorCode.TOOL_NOT_ALLOWLISTED, internal_detail="x")
            return dict(model_args)

        result = resume_action_plan(
            session,
            operation,
            dispatcher=_dispatcher(),
            authorize=allow_only_the_first,
            keyring=keyring,
            action_keyring=keyring,
            now=now,
        )

        assert result is not None
        assert _titles(result.device_actions) == ["网球"]
        rows = plan_operations(session, operation.plan_key)
        assert [row.state for row in rows] == ["source_in_progress", "failed_safe"]
        assert rows[1].failure_reason == "policy_denied"
        assert (
            resume_action_plan(
                session,
                operation,
                dispatcher=_dispatcher(),
                authorize=allow,
                keyring=keyring,
                action_keyring=keyring,
                now=now,
            )
            is None
        )


# --- delivery ----------------------------------------------------------------


def test_the_message_row_hands_over_the_whole_plan_and_an_item_only_itself(
    op_session,
) -> None:
    """The user polls the message, so that row is where the list appears; an
    item's own row answers with its own action, because a phone reporting one
    result should not be handed its siblings' actions back."""
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        _run_turn(
            session, operation, _two_calls(), _dispatcher(), keyring, now
        )
        session.refresh(operation)
        rows = plan_operations(session, operation.plan_key)

        whole = _operation_projection(
            keyring, operation, client_wire_version=CLIENT_WIRE_V2
        )["device_actions"]
        assert _titles(whole) == ["网球", "牙医"]

        mine = _operation_projection(
            keyring, rows[1], client_wire_version=CLIENT_WIRE_V2
        )["device_actions"]
        assert _titles(mine) == ["牙医"]


def test_the_plan_is_still_delivered_after_the_first_item_settles(op_session) -> None:
    """The anchor can settle before its siblings are reported.

    The phone creates the first event, reports it, and only then needs the
    second action again -- a response it lost, a reinstall, a poll. If the
    message row stopped answering for the plan the moment it left
    `source_in_progress`, that second action would be issued to nobody.
    """
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        _run_turn(
            session, operation, _two_calls(), _dispatcher(), keyring, now
        )
        session.refresh(operation)
        rows = plan_operations(session, operation.plan_key)

        for target in ("verifying", "succeeded"):
            session.refresh(rows[0])
            transition_operation(
                session,
                operation_id=rows[0].operation_id,
                current_state=rows[0].state,
                current_version=rows[0].state_version,
                target_state=target,
                now=now,
            )
        session.refresh(operation)
        assert operation.state == "succeeded"

        handed = _operation_projection(
            keyring, operation, client_wire_version=CLIENT_WIRE_V2
        )["device_actions"]
        assert _titles(handed) == ["牙医"]


def test_the_delivery_gate_is_applied_to_the_whole_plan(op_session) -> None:
    """A phone that was restored, downgraded or replaced between issuing and
    reading gets no part of the list. Handing over the items it *can* decode
    would be worse than withholding: it would create some of the events and
    silently drop the rest."""
    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _message(session)
        _run_turn(
            session, operation, _two_calls(), _dispatcher(), keyring, now
        )
        session.refresh(operation)

        projection = _operation_projection(
            keyring, operation, client_wire_version=CLIENT_WIRE_V1
        )
        assert "device_actions" not in projection
        assert projection["state"] == "source_in_progress"


# --- the plan's own invariants ----------------------------------------------


def test_a_message_cannot_be_joined_to_a_second_plan(op_session) -> None:
    """A different plan key on one operation is a wiring error the row cannot
    express. It is answered by a read-back rather than a silent rebind, and the
    same plan arriving twice is the same fact, not a conflict."""
    factory, _ = op_session
    stamp = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
    with factory() as session:
        operation = _message(session)
        join_action_plan(
            session, operation_id=operation.operation_id, plan_key="plan-a", now=stamp
        )
        join_action_plan(
            session, operation_id=operation.operation_id, plan_key="plan-a", now=stamp
        )

        with pytest.raises(AppError) as excinfo:
            join_action_plan(
                session,
                operation_id=operation.operation_id,
                plan_key="plan-b",
                now=stamp,
            )

        assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
        assert "plan-a" in excinfo.value.internal_detail
        session.refresh(operation)
        assert (operation.plan_key, operation.plan_index) == ("plan-a", 0)
