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
    "calendar": "日常安排",
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


def test_the_issued_action_carries_the_attested_arguments_not_the_raw_output() -> None:
    """The action is the authorisation record, so what it carries must be what
    `authorize` attested — not the model's raw output. Reusing the model's dict
    verbatim would seal a smuggled `device_id` into the record the phone
    executes from, and the seal would then be testimony to something no policy
    approved."""
    cleaned_by_authorize = {
        key: value for key, value in {**CAL_ARGS, "device_id": "smuggled"}.items()
        if key != "device_id"
    }

    class AttestingBridge(SpyBridge):
        def authorize(self, alias, arguments, device):
            self.authorized.append(alias)
            return self.registry.resolve(alias), cleaned_by_authorize

    outcome = cal_dispatcher(AttestingBridge()).resolve(
        tool="calendar.create_event",
        model_args={**CAL_ARGS, "device_id": "smuggled"},
        idempotency_key="action-key-1",
    )
    assert isinstance(outcome, DeviceActionIssued)
    assert outcome.event_fields == CAL_ARGS
    assert "device_id" not in outcome.event_fields


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


def _action_keyring(kid: str = "agent-data-2026"):
    """A real keyring so the seal is a real envelope, not a test stub.

    The projection later has to *open* what the orchestrator sealed, so the
    failing shapes (tampered envelope, wrong key) are only reproducible with
    the production crypto in the loop.
    """
    from personal_agent_core.crypto import KeyRing, generate_key

    return KeyRing([generate_key(kid, state="active")], service="personal-agent")


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
            session,
            operation,
            outcome,
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=_action_keyring(),
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
        # And it is sealed on the row in the same transition, so the poll can
        # hand it over when the response did not (review R6).
        assert operation.encrypted_device_action is not None


def test_device_action_no_longer_rides_the_chat_response_as_transient() -> None:
    """One delivery door, not two (review R6). The action is sealed on the
    operation and handed over by the projection while parked, so the worker's
    transient copy is dropped: the 200 reply, the by-id poll and a replay all
    answer the same projection, and two channels could never be made to agree
    about what was handed over."""
    from types import SimpleNamespace

    from personal_agent.api.app import _transient

    result = SimpleNamespace(
        answer=None,
        clarification=None,
        duplicate_existing=None,
        device_action={
            "action_id": "action-key-1",
            "tool": "calendar.create_event",
            "event": dict(CAL_ARGS),
        },
    )
    assert _transient(result) == {}
    # A plain turn (no device action) carries nothing new.
    plain = SimpleNamespace(
        answer="好的",
        clarification=None,
        duplicate_existing=None,
        device_action=None,
    )
    assert _transient(plain) == {"answer": "好的"}
    # An object predating the field (getattr default) keeps working.
    legacy = SimpleNamespace(answer=None, clarification=None, duplicate_existing=None)
    assert _transient(legacy) == {}


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


# --- review R8: a device action crashed in `dispatching` must not be stuck ----


def test_a_device_operation_crashed_in_dispatching_fails_safe(op_session) -> None:
    """Review R8, reproduced: `dispatching` means the action was never handed
    to the response, so the phone never received it — zero-write evidence by
    construction. Recovery must resolve it to `failed_safe`, not LEAVE it in a
    recoverable state forever, re-scanned and never settled."""
    from personal_agent.api.recovery import RecoveryAction, plan_recovery

    _ = op_session
    plan = plan_recovery("dispatching", None, quiet=True, executor="device")
    assert plan.action is RecoveryAction.RESOLVE
    assert plan.target_state == "failed_safe"
    # The reason states the evidence: the response is the only channel that
    # carries a device action, and it never left.
    assert "dispatch" in plan.reason


def test_a_device_operation_in_dispatching_is_not_quiet_yet_left_alone(
    op_session,
) -> None:
    """A live worker may still be between dispatch and the response: only the
    quiet period turns the crash into evidence."""
    from personal_agent.api.recovery import RecoveryAction, plan_recovery

    _ = op_session
    plan = plan_recovery("dispatching", None, quiet=False, executor="device")
    assert plan.action is RecoveryAction.LEAVE


def test_a_crashed_dispatching_device_operation_is_swept_by_recovery(
    op_session,
) -> None:
    """End to end: an operation parked at `dispatching` on a device tool is
    resolved to `failed_safe` by the recovery scan, and leaves the
    recoverable set."""
    from datetime import datetime, timedelta, timezone

    from personal_agent.api.recovery import recover_pending

    def _no_finance_execution(idempotency_key):
        # The `read_status` callable shape: no execution exists for any key.
        return None

    factory, now = op_session
    with factory() as session:
        operation = _make_operation(session)
        # `_make_operation` parks the operation at `dispatching` already.
        session.refresh(operation)
        operation_id = operation.operation_id

    # The quiet period passes with the worker dead.
    with factory() as session:
        results = recover_pending(
            session,
            _no_finance_execution,
            now=datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc) + timedelta(hours=1),
        )
        session.commit()

    plans = dict(results)
    assert operation_id in plans
    assert plans[operation_id].target_state == "failed_safe"
    from personal_agent.storage.models import Operation

    with factory() as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None
        assert operation.state == "failed_safe"


# --- review R6: the 202 path must still hand over the device action ----------


def test_the_issued_action_is_sealed_on_the_operation_in_the_same_transition(
    op_session,
) -> None:
    """Review R6: a device action that only ever rides the chat response is
    lost when the request times out at 202 — the operation parks at
    `source_in_progress` with the action nobody delivered. The seal must be
    written in the same committed transition that steps to
    `source_in_progress`, so the parked operation and its undelivered action
    become durable together."""
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
            session,
            operation,
            outcome,
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=_action_keyring(),
        )
        session.commit()

        assert result.state == "source_in_progress"
        session.refresh(operation)
        # The envelope is sealed (an EncryptedEnvelope column refuses
        # plaintext, and the dict here is the sealed shape).
        envelope = operation.encrypted_device_action
        assert envelope is not None
        assert {"v", "kid", "nonce", "ciphertext", "tag"} <= set(envelope)


def test_the_projection_hands_the_action_over_while_parked(op_session) -> None:
    """The parked operation's poll must carry the action: the client that
    detached at 202 polls by id, and this projection is its only door. The
    action handed over is exactly the one that was authorised."""
    from personal_agent.api.app import _operation_projection
    from personal_agent.api.orchestrator import _apply_resolve

    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _make_operation(session)
        _apply_resolve(
            session,
            operation,
            DeviceActionIssued(
                action_id="action-key-1",
                tool="calendar.create_event",
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=keyring,
        )
        session.commit()
        session.refresh(operation)

        projection = _operation_projection(keyring, operation)
        assert projection["device_action"] == {
            "action_id": "action-key-1",
            "tool": "calendar.create_event",
            "event": dict(CAL_ARGS),
        }


def test_a_settled_operation_refuses_to_hand_the_action_over(op_session) -> None:
    """Delivery-or-refusal: once the operation has settled, the action must
    not travel again. A poll answered after the device's own report carries
    the settled projection, never a re-executable action — replaying one
    would let a stale read re-arm a finished write."""
    from personal_agent.api.app import _operation_projection
    from personal_agent.api.operation_store import transition_operation
    from personal_agent.api.orchestrator import _apply_resolve

    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _make_operation(session)
        _apply_resolve(
            session,
            operation,
            DeviceActionIssued(
                action_id="action-key-1",
                tool="calendar.create_event",
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=keyring,
        )
        session.commit()
        session.refresh(operation)
        transition_operation(
            session,
            operation_id=operation.operation_id,
            current_state=operation.state,
            current_version=operation.state_version,
            target_state="needs_manual_review",
            failure_reason="device report timed out; the write may exist",
            now=now,
        )
        session.commit()
        session.refresh(operation)

        projection = _operation_projection(keyring, operation)
        assert "device_action" not in projection
        # Settlement closes the delivery window centrally: leaving
        # `source_in_progress` clears the seal, so no future settlement path
        # can forget to, and the schema CHECK backstops the mechanism.
        assert operation.encrypted_device_action is None


def test_an_unopenable_action_envelope_fails_closed(op_session) -> None:
    """A sealed action that will not open (wrong key, tampered envelope) must
    not surface as an exception on a poll and must never surface as a
    guessed action. The projection omits the field; the timeout sweep is the
    remaining witness, exactly as for an envelope that never existed."""
    from personal_agent.api.app import _operation_projection
    from personal_agent.api.orchestrator import _apply_resolve

    factory, now = op_session
    keyring = _action_keyring()
    other = _action_keyring(kid="agent-data-other")
    with factory() as session:
        operation = _make_operation(session)
        _apply_resolve(
            session,
            operation,
            DeviceActionIssued(
                action_id="action-key-1",
                tool="calendar.create_event",
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=other,
        )
        session.commit()
        session.refresh(operation)

        projection = _operation_projection(keyring, operation)
        assert "device_action" not in projection


def test_the_transient_channel_no_longer_carries_the_action(op_session) -> None:
    """One delivery channel, not two: with the seal on the operation, the
    projection is the single door. `_transient` keeping its own copy would
    mean the 200 path and the poll answer could disagree about what was
    handed over."""
    from types import SimpleNamespace

    from personal_agent.api.app import _transient

    result = SimpleNamespace(
        answer=None,
        clarification=None,
        duplicate_existing=None,
        device_action={
            "action_id": "action-key-1",
            "tool": "calendar.create_event",
            "event": dict(CAL_ARGS),
        },
    )
    assert _transient(result) == {}


def test_a_finance_operation_never_carries_a_device_action(op_session) -> None:
    """The projection hands an action over only for a device-executed tool:
    a Finance operation parked at `source_in_progress` is a governed write in
    flight, and inventing a device action for it would invite the client to
    execute something no policy authorised."""
    from personal_agent.api.app import _operation_projection
    from personal_agent.api.orchestrator import _apply_resolve

    factory, now = op_session
    keyring = _action_keyring()
    with factory() as session:
        operation = _make_operation(session)
        _apply_resolve(
            session,
            operation,
            DeviceActionIssued(
                action_id="action-key-1",
                tool="calendar.create_event",
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=keyring,
        )
        session.commit()
        session.refresh(operation)
        # Forged row: the tool does not match the executor the seal came from.
        operation.tool = "finance.log_expense"
        session.commit()
        session.refresh(operation)

        projection = _operation_projection(keyring, operation)
        assert "device_action" not in projection
