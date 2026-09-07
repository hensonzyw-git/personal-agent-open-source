"""The device-action dispatch fork for `calendar.create_event`.

The calendar write does not cross the MCP bridge: the iPhone's EventKit is the
executor. The dispatcher's job on this path is to *authorise exactly like any
other governed write* (scope, allowlist, write switch, schema), then stop —
returning the issued action instead of calling a connector. The orchestrator
commits `source_in_progress` before the response leaves, so the phone may
write the event the moment the message is in flight; settlement happens later
through the device-action result endpoint, and recovery refuses to reconcile
these operations against Finance's execution store (no such row can exist).

The failure shapes pinned here are the ones §5.1 names for a boundary a model
chooses but a device executes: a denial or a reported failure is *zero-write
evidence from the fact source itself* (failed_safe), while a report that never
arrived is not evidence of anything except silence (needs_manual_review, by
the timeout sweep, never failed_safe — that would claim no-write).
"""

from __future__ import annotations

import pytest

from personal_agent.api.finance_dispatcher import (
    DispatcherContext,
    McpFinanceDispatcher,
)
from personal_agent.api.orchestrator import (
    DeviceActionIssued,
    ResolveFailedSafe,
)
from personal_agent.policy.bridge import DeviceAuthorization
from personal_agent_core.errors import AppError, ErrorCode

CAL_ARGS = {
    "title": "网球",
    "start": "2026-09-12T15:00:00+08:00",
    "end": "2026-09-12T16:30:00+08:00",
    "all_day": False,
}

CAL_DEVICE = DeviceAuthorization(
    device_id="device-1",
    status="active",
    scopes=frozenset({"calendar.event.write"}),
    allowed_tools={"calendar.create_event"},
    allowed_tools_version="atv-1",
)


class FakeEntry:
    def __init__(self, remote_name: str) -> None:
        self.remote_name = remote_name


class FakeRegistry:
    def __init__(self, aliases: dict[str, str]) -> None:
        self._aliases = aliases

    def resolve(self, alias: str) -> FakeEntry:
        if alias not in self._aliases:
            raise KeyError(alias)
        return FakeEntry(self._aliases[alias])


class SpyBridge:
    """Records every `execute` call; the device fork must make none."""

    def __init__(self, aliases: dict[str, str] | None = None):
        self.registry = FakeRegistry(
            aliases or {"calendar.create_event": "calendar.create_event"}
        )
        self.executed: list[str] = []
        self.authorized: list[str] = []

    def authorize(self, alias, arguments, device):
        self.authorized.append(alias)
        return self.registry.resolve(alias), arguments

    async def execute(self, alias, arguments, device, *, call_context):
        self.executed.append(alias)
        raise AssertionError("device-executed tool must never reach bridge.execute")


def cal_dispatcher(bridge: SpyBridge) -> McpFinanceDispatcher:
    return McpFinanceDispatcher(
        bridge=bridge,
        control=None,  # device path never reads the control plane on resolve
        signing_ring=None,  # no Host Context is signed when nothing is sent
        context=DispatcherContext(
            device=CAL_DEVICE,
            user_id="henson",
            agent_id="agent-1",
            conversation_trace_id="trace-1",
        ),
    )


# --- dispatcher resolve fork -------------------------------------------------


def test_calendar_create_event_authorizes_and_issues_a_device_action() -> None:
    bridge = SpyBridge()
    outcome = cal_dispatcher(bridge).resolve(
        tool="calendar.create_event",
        model_args=dict(CAL_ARGS),
        idempotency_key="action-key-1",
    )
    assert isinstance(outcome, DeviceActionIssued)
    assert outcome.tool == "calendar.create_event"
    assert outcome.event_fields == CAL_ARGS
    # Authorisation genuinely ran — scope, allowlist and schema are still the
    # governed path even though no connector is behind this tool.
    assert bridge.authorized == ["calendar.create_event"]
    # And nothing was dispatched anywhere: the phone is the executor.
    assert bridge.executed == []


def test_device_action_idempotency_key_is_supplied_by_the_host() -> None:
    """The orchestrator reuses the operation's own idempotency key as the
    action_id, so one message can produce at most one device side effect and
    a replay of the same key re-issues the same action id."""
    bridge = SpyBridge()
    outcome = cal_dispatcher(bridge).resolve(
        tool="calendar.create_event",
        model_args=dict(CAL_ARGS),
        idempotency_key="op-key-123",
    )
    assert isinstance(outcome, DeviceActionIssued)
    assert outcome.action_id == "op-key-123"


def test_device_tool_denial_is_a_safe_failure_not_an_issue() -> None:
    """A scope or allowlist refusal happens before anything is issued; the
    device is never told to write, so this is provably zero-write."""
    from personal_agent.policy.bridge import GovernedToolBridge  # noqa: F401

    class RefusingBridge(SpyBridge):
        def authorize(self, alias, arguments, device):
            raise AppError(
                ErrorCode.SCOPE_DENIED,
                internal_detail=f"{alias} needs calendar.event.write",
            )

    outcome = cal_dispatcher(RefusingBridge()).resolve(
        tool="calendar.create_event", model_args=dict(CAL_ARGS)
    )
    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "SCOPE_DENIED"


def test_every_contract_declares_its_executor_derived_fork() -> None:
    """The fork is derived from the IR, never hand-listed: if a second device
    tool ever ships, the dispatcher must already send it to the phone, and if
    the calendar tool flips back to `mcp` this must fail loudly."""
    from personal_agent_core.tool_ir import TOOL_CONTRACTS

    device_tools = {
        contract.name for contract in TOOL_CONTRACTS if contract.executor == "device"
    }
    assert device_tools == {"calendar.create_event"}


# --- orchestrator _apply_resolve fork ----------------------------------------


@pytest.fixture()
def op_session(tmp_path):
    from datetime import datetime, timezone

    from personal_agent.storage.engine import create_all, create_database_engine, session_factory

    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    factory = session_factory(engine)
    now = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
    yield factory, now
    engine.dispose()


def _make_operation(session) -> object:
    from datetime import datetime, timezone

    from personal_agent.storage.models import ApiRequest, Device, Operation

    stamp = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
    session.add(
        Device(
            device_id="device-1",
            display_name="iPhone",
            public_key="K",
            device_key_thumbprint="T",
            status="active",
            scopes='["calendar.event.write"]',
            allowed_tools_version="atv-1",
            created_at=stamp,
        )
    )
    session.add(
        ApiRequest(
            request_id="req-1",
            device_id="device-1",
            client_request_id="req-1",
            request_fingerprint="fp",
            received_at=stamp,
        )
    )
    session.flush()
    operation = Operation(
        operation_id="op-1",
        request_id="req-1",
        trace_id="trace-1",
        idempotency_key="action-key-1",
        tool="calendar.create_event",
        state="dispatching",
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(operation)
    session.commit()
    return operation


def test_apply_resolve_moves_device_action_to_source_in_progress(op_session) -> None:
    """Post-submit semantics: `source_in_progress` commits *before* the chat
    response leaves, so the phone may create the event while the message is in
    flight and a crash after this point cannot fake a cancellation."""
    from personal_agent.api.orchestrator import _apply_resolve

    factory, now = op_session
    with factory() as session:
        operation = _make_operation(session)
        outcome = DeviceActionIssued(
            action_id="action-key-1",
            tool="calendar.create_event",
            event_fields=dict(CAL_ARGS),
        )
        result = _apply_resolve(
            session, operation, outcome, dispatcher=None, keyring=None, now=now
        )
        session.commit()

        assert result.state == "source_in_progress"
        session.refresh(operation)
        assert operation.state == "source_in_progress"
        assert operation.tool == "calendar.create_event"
        # The issued action rides the run result as the response payload.
        assert result.device_action == {
            "action_id": "action-key-1",
            "tool": "calendar.create_event",
            "event": dict(CAL_ARGS),
        }


def test_device_action_commit_is_a_composition_error(op_session) -> None:
    """The two-phase protocol's phase 2 has no meaning for a device tool: the
    write is issued in phase 1. Reaching `commit` with a device intent means
    the fork failed to intercept, and it must be loud."""
    from datetime import datetime, timezone

    from personal_agent.api.intent import WriteIntent
    from personal_agent.api.orchestrator import _commit

    factory, now = op_session
    with factory() as session:
        operation = _make_operation(session)
        _step_to_source_in_progress = None
        intent = WriteIntent(tool="calendar.create_event", model_args=dict(CAL_ARGS))
        with pytest.raises(AppError) as excinfo:
            _commit(
                session,
                operation,
                intent=intent,
                dispatcher=cal_dispatcher(SpyBridge()),
                duplicate_override=None,
                keyring=None,
                now=datetime(2026, 9, 7, 8, 1, tzinfo=timezone.utc),
            )
        assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


# --- recovery guard -----------------------------------------------------------


def test_recovery_skips_device_executor_operations(op_session) -> None:
    """A device-action operation parked at `source_in_progress` has no Finance
    execution twin — the authoritative twin is the phone's PATCH. `plan_recovery`
    must refuse to reconcile it against Finance's execution store, or a Finance
    outage (status None) would read as proof the phone never wrote."""
    from personal_agent.api.recovery import RecoveryAction, plan_recovery

    _ = op_session
    plan = plan_recovery(
        "source_in_progress",
        None,
        quiet=True,
        executor="device",
    )
    assert plan.action is RecoveryAction.LEAVE
    assert plan.reason is not None


def test_device_action_timeout_sweep_targets_needs_manual_review(op_session) -> None:
    """A device report that never arrived is silence, not evidence. The sweep
    must park the operation at `needs_manual_review` — never `failed_safe`,
    which would claim the phone wrote nothing when the write may exist."""
    from personal_agent.api.operation_store import sweep_timed_out_device_actions
    from datetime import datetime, timedelta, timezone

    factory, now = op_session
    with factory() as session:
        operation = _make_operation(session)
        session.refresh(operation)
        from personal_agent.api.operation_store import transition_operation

        transition_operation(
            session,
            operation_id=operation.operation_id,
            current_state=operation.state,
            current_version=operation.state_version,
            target_state="source_in_progress",
            tool="calendar.create_event",
            now=datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc),
        )
        session.commit()

        # 15 minutes pass with no device report.
        result = sweep_timed_out_device_actions(
            session,
            now=datetime(2026, 9, 7, 8, 16, tzinfo=timezone.utc),
        )
        session.commit()
        session.refresh(operation)
        assert result == [(operation.operation_id, "needs_manual_review")]
        assert operation.state == "needs_manual_review"


def test_sweep_leaves_a_fresh_device_action_alone(op_session) -> None:
    from personal_agent.api.operation_store import (
        sweep_timed_out_device_actions,
        transition_operation,
    )
    from datetime import datetime, timezone

    factory, now = op_session
    with factory() as session:
        operation = _make_operation(session)
        session.refresh(operation)
        transition_operation(
            session,
            operation_id=operation.operation_id,
            current_state=operation.state,
            current_version=operation.state_version,
            target_state="source_in_progress",
            tool="calendar.create_event",
            now=datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc),
        )
        session.commit()

        result = sweep_timed_out_device_actions(
            session,
            now=datetime(2026, 9, 7, 8, 5, tzinfo=timezone.utc),
        )
        session.refresh(operation)
        assert result == []
        assert operation.state == "source_in_progress"


def test_sweep_never_touches_finance_operations(op_session) -> None:
    """Finance writes sitting at `source_in_progress` belong to the Finance
    reconciler, which reads the execution store; the device sweep must leave
    them exactly alone."""
    from personal_agent.api.operation_store import (
        sweep_timed_out_device_actions,
        transition_operation,
    )
    from datetime import datetime, timezone

    factory, now = op_session
    with factory() as session:
        operation = _make_operation(session)
        session.refresh(operation)
        # Same parked state, but a connector-executed tool.
        session.execute(
            operation.__table__.update()
            .where(operation.__table__.c.operation_id == operation.operation_id)
            .values(tool="finance.log_expense")
        )
        transition_operation(
            session,
            operation_id=operation.operation_id,
            current_state="dispatching",
            current_version=operation.state_version,
            target_state="source_in_progress",
            tool="finance.log_expense",
            now=datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc),
        )
        session.commit()

        result = sweep_timed_out_device_actions(
            session,
            now=datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc),
        )
        session.refresh(operation)
        assert result == []
        assert operation.state == "source_in_progress"
