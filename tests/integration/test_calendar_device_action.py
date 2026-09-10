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

from personal_agent.api.calendar_issue import CLIENT_UPGRADE_QUESTION
from personal_agent.api.finance_dispatcher import (
    DispatcherContext,
    McpFinanceDispatcher,
)
from personal_agent.api.control_client import (
    CalendarCandidate,
    CalendarResolved,
    CalendarUnresolved,
    ControlPlaneError,
)
from personal_agent.api.orchestrator import (
    DeviceActionIssued,
    NeedsClarification,
    ResolveFailedSafe,
)
from personal_agent.policy.bridge import DeviceAuthorization
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.tool_ir import DEFAULT_CLIENT_WIRE_VERSION

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

#: The version `calendar.create_event` declares, i.e. the one a client must
#: implement before an action may be issued to it (design 2.5). Tests that are
#: not about the capability gate say it explicitly so the gate is never what
#: decides them, and the capability-gate tests say the other one.
CLIENT_WIRE_V2 = 2
CLIENT_WIRE_V1 = DEFAULT_CLIENT_WIRE_VERSION


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


class FakeControl:
    """The control plane's calendar lookup, as the dispatcher sees it.

    The device path reads no Finance execution and signs no Host Context, but
    it does ask *which calendar this name is*: the directory is in the MCP
    database and the model must not be able to choose an identifier itself.
    """

    def __init__(self, resolution=None, *, error: Exception | None = None) -> None:
        self._resolution = resolution or CalendarResolved(
            calendar_identifier="uuid-ri-chang", calendar_title="日常安排"
        )
        self._error = error
        self.lookups: list[tuple[str, str]] = []

    async def resolve_calendar(self, *, device_id: str, title: str):
        self.lookups.append((device_id, title))
        if self._error is not None:
            raise self._error
        return self._resolution


def cal_dispatcher(
    bridge: SpyBridge,
    control: FakeControl | None = None,
    *,
    client_wire_version: int = CLIENT_WIRE_V2,
) -> McpFinanceDispatcher:
    return McpFinanceDispatcher(
        bridge=bridge,
        control=control or FakeControl(),
        signing_ring=None,  # no Host Context is signed when nothing is sent
        context=DispatcherContext(
            device=CAL_DEVICE,
            user_id="henson",
            agent_id="agent-1",
            conversation_trace_id="trace-1",
            client_wire_version=client_wire_version,
        ),
    )


def _issued(attested: dict, resolution) -> dict:
    """What the phone receives for one attested request, as it receives it."""
    from personal_agent.api.calendar_issue import action_fields, parse_request

    return action_fields(
        parse_request(attested), resolution, attested=attested
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
    # The action carries the attested request plus the routing the server did:
    # the phone never picks a calendar, so the identifier can only come from
    # here. Both date fields are written out as null rather than omitted, so
    # the client can tell "not applicable" from "did not arrive".
    assert outcome.event_fields == {
        **CAL_ARGS,
        "calendar_identifier": "uuid-ri-chang",
        "calendar_title": "日常安排",
        "timezone": None,
        "start_date": None,
        "end_date": None,
    }
    assert outcome.wire_version == 2
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
    assert outcome.event_fields == {
        **CAL_ARGS,
        "calendar_identifier": "uuid-ri-chang",
        "calendar_title": "日常安排",
        "timezone": None,
        "start_date": None,
        "end_date": None,
    }
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
            wire_version=2,
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
            "wire_version": 2,
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
            wire_version=2,
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
                wire_version=2,
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=keyring,
        )
        session.commit()
        session.refresh(operation)

        projection = _operation_projection(
            keyring, operation, client_wire_version=CLIENT_WIRE_V2
        )
        # A list even for the single action a v1-shaped request issues: a
        # client that switched on the field name would need two decode paths
        # for one contract (design 2.5.4).
        assert projection["device_actions"] == [
            {
                "action_id": "action-key-1",
                "tool": "calendar.create_event",
                "wire_version": 2,
                "event": dict(CAL_ARGS),
            }
        ]


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
                wire_version=2,
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

        projection = _operation_projection(keyring, operation, client_wire_version=CLIENT_WIRE_V2)
        assert "device_actions" not in projection
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
                wire_version=2,
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=other,
        )
        session.commit()
        session.refresh(operation)

        projection = _operation_projection(keyring, operation, client_wire_version=CLIENT_WIRE_V2)
        assert "device_actions" not in projection


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
                wire_version=2,
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

        projection = _operation_projection(keyring, operation, client_wire_version=CLIENT_WIRE_V2)
        assert "device_actions" not in projection


@pytest.mark.parametrize(
    "wire_version",
    [
        pytest.param("absent", id="missing"),
        pytest.param("2", id="string"),
        pytest.param(True, id="bool"),
        pytest.param(0, id="below-one"),
    ],
)
def test_a_sealed_action_with_an_unreadable_wire_version_does_not_open(
    op_session, wire_version
) -> None:
    """The delivery gate compares the action's `wire_version` against the
    client's own, so an action whose version is missing or nonsense must not
    open at all. Reading it as "no requirement" would hand the v2 action --
    routing identifier, zone, all-day dates -- to the v1 client this field
    exists to keep it away from, and the seal would be testifying to a write
    the client performs differently than the one that was authorised."""
    from personal_agent.api.app import _operation_projection
    from personal_agent.api.device_action_projection import seal_device_action
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
                wire_version=2,
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=keyring,
        )
        session.commit()
        session.refresh(operation)

        # A crafted seal under the *right* key: the envelope opens cleanly, so
        # only the shape check can refuse it.
        crafted = {
            "action_id": "action-key-1",
            "tool": "calendar.create_event",
            "event": dict(CAL_ARGS),
        }
        if wire_version != "absent":
            crafted["wire_version"] = wire_version
        operation.encrypted_device_action = seal_device_action(
            keyring, operation_id=operation.operation_id, action=crafted
        )
        session.commit()
        session.refresh(operation)

        assert "device_actions" not in _operation_projection(keyring, operation, client_wire_version=CLIENT_WIRE_V2)


# --- pre-issuance policy: shapes the service never accepts -------------------

ALL_DAY_ARGS = {
    "title": "西班牙之旅",
    "start": "2026-10-01T00:00:00+08:00",
    "end": "2026-10-04T00:00:00+08:00",
    "all_day": True,
    "calendar": "出游计划",
    "start_date": "2026-10-01",
    "end_date": "2026-10-04",
}


def _refusal_with(model_args: dict, resolution):
    """Resolve one create request against a chosen routing answer."""
    bridge = SpyBridge()
    control = FakeControl(resolution)
    return (
        cal_dispatcher(bridge, control).resolve(
            tool="calendar.create_event",
            model_args=model_args,
            idempotency_key="action-key-1",
        ),
        bridge,
        control,
    )


def _refusal(model_args: dict):
    return _refusal_with(
        model_args, CalendarResolved("uuid-ri-chang", "日常安排")
    )


def test_the_flight_plan_calendar_is_refused_by_the_service() -> None:
    """【飞行计划】 is in the enum so that choosing it produces a refusal rather
    than the model inventing a calendar or funnelling a flight into
    【日常安排】. The service is the single refusal point, and the refusal
    precedes routing: the phone's directory is never even consulted about a
    calendar nothing may write to."""
    outcome, bridge, control = _refusal({**CAL_ARGS, "calendar": "飞行计划"})

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "INVALID_ARGUMENT"
    assert control.lookups == []
    assert bridge.executed == []


def test_an_unknown_timezone_is_refused() -> None:
    """`ZoneInfo` is the authority on whether a zone exists; a model that
    invents `Asia/Shangai` must be told to recompute, not have its typo
    silently resolved to Shanghai."""
    outcome, bridge, _ = _refusal({**CAL_ARGS, "timezone": "Asia/Shangai"})

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "INVALID_ARGUMENT"
    assert bridge.executed == []


def test_an_all_day_event_carrying_a_zone_is_refused() -> None:
    """The EventKit probe froze this: an all-day event is a floating date with
    no owning zone, so a zone on one has no meaning to store and no meaning to
    execute."""
    outcome, _, _ = _refusal({**ALL_DAY_ARGS, "timezone": "Europe/Madrid"})

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "INVALID_ARGUMENT"


def test_an_all_day_instant_that_is_not_local_midnight_is_refused() -> None:
    """The dates are the source of truth for an all-day event, and the model's
    own midnight arithmetic is not trusted: an instant that disagrees with the
    date it claims to be would make the action mean two things at once, and
    the phone would have to pick one. This is a recomputation, not a question
    for the user."""
    outcome, _, _ = _refusal(
        {**ALL_DAY_ARGS, "start": "2026-10-01T09:00:00+08:00"}
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "INVALID_ARGUMENT"


def test_an_all_day_end_that_is_not_midnight_of_its_exclusive_date_is_refused() -> None:
    """The end is the day *after* the last one. A model that hands back the
    last day's midnight has misread the exclusive rule, and executing it would
    silently shorten the trip by a day."""
    outcome, _, _ = _refusal({**ALL_DAY_ARGS, "end": "2026-10-03T00:00:00+08:00"})

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "INVALID_ARGUMENT"


def test_an_all_day_event_with_no_days_in_it_is_refused() -> None:
    outcome, _, _ = _refusal(
        {
            **ALL_DAY_ARGS,
            "end": "2026-10-01T00:00:00+08:00",
            "end_date": "2026-10-01",
        }
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "INVALID_ARGUMENT"


def test_a_timed_event_carrying_all_day_dates_is_refused() -> None:
    outcome, _, _ = _refusal(
        {**CAL_ARGS, "start_date": "2026-09-12", "end_date": "2026-09-13"}
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "INVALID_ARGUMENT"


def test_an_interval_that_ends_before_it_starts_is_refused() -> None:
    outcome, _, _ = _refusal({**CAL_ARGS, "end": "2026-09-12T14:00:00+08:00"})

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "INVALID_ARGUMENT"


# --- routing: the world does not allow it, so it becomes a question ----------


def test_an_unroutable_calendar_name_becomes_a_question_not_a_failure() -> None:
    """A name the phone does not have is not a malformed request: the user
    asked for something reasonable and only they can say what they meant. No
    action is issued, so the operation parks on a question instead of failing
    as though the model had erred."""
    outcome, bridge, _ = _refusal_with(
        CAL_ARGS, CalendarUnresolved(reason="not_found")
    )

    assert isinstance(outcome, NeedsClarification)
    assert "日常安排" in outcome.reason
    assert "找不到" in outcome.reason
    assert bridge.executed == []


def test_a_name_on_two_accounts_is_asked_about_by_source() -> None:
    """Picking either would write to a calendar the user did not choose, so
    both are offered -- and offered by *source*, because "two calendars called
    日常安排" is only answerable if the user can tell them apart."""
    outcome, _, _ = _refusal_with(
        CAL_ARGS,
        CalendarUnresolved(
            reason="ambiguous",
            candidates=(
                CalendarCandidate(title="日常安排", source_title="iCloud"),
                CalendarCandidate(title="日常安排", source_title="Gmail"),
            ),
        ),
    )

    assert isinstance(outcome, NeedsClarification)
    assert "iCloud" in outcome.reason and "Gmail" in outcome.reason


def test_a_read_only_match_is_refused_with_its_reason() -> None:
    outcome, _, _ = _refusal_with(
        CAL_ARGS,
        CalendarUnresolved(
            reason="read_only",
            candidates=(CalendarCandidate(title="球赛", source_title="订阅"),),
        ),
    )

    assert isinstance(outcome, NeedsClarification)
    assert "只读" in outcome.reason


def test_a_device_that_never_synced_its_directory_gets_a_different_answer() -> None:
    """`directory_empty` is the one miss with a remedy the user controls, and
    it must not be reported as "no such calendar": that would send them
    looking for a calendar that may well exist."""
    outcome, _, _ = _refusal_with(
        CAL_ARGS, CalendarUnresolved(reason="directory_empty")
    )

    assert isinstance(outcome, NeedsClarification)
    assert "同步" in outcome.reason


def test_the_lookup_is_bound_to_the_device_that_is_acting() -> None:
    """The directory is per-device (design 2.1). Resolving against another
    device's calendars would seal an identifier the acting phone cannot write
    to, and the failure would surface on the phone, not here."""
    _, _, control = _refusal(CAL_ARGS)

    assert control.lookups == [("device-1", "日常安排")]


def test_an_unreadable_directory_is_a_safe_failure_not_a_question() -> None:
    """No question a user can answer makes an unreachable service reachable,
    and no action may be issued from a routing decision that was never made."""
    bridge = SpyBridge()
    control = FakeControl(error=ControlPlaneError("the control plane is down"))

    outcome = cal_dispatcher(bridge, control).resolve(
        tool="calendar.create_event",
        model_args=dict(CAL_ARGS),
        idempotency_key="action-key-1",
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "SOURCE_UNAVAILABLE"
    assert bridge.executed == []


# --- the action the phone receives -------------------------------------------


def test_an_all_day_action_carries_dates_and_never_a_zone() -> None:
    outcome, _, _ = _refusal_with(
        ALL_DAY_ARGS, CalendarResolved("uuid-chu-you", "出游计划")
    )

    assert isinstance(outcome, DeviceActionIssued)
    assert outcome.event_fields["calendar_identifier"] == "uuid-chu-you"
    assert outcome.event_fields["calendar_title"] == "出游计划"
    assert outcome.event_fields["timezone"] is None
    assert outcome.event_fields["start_date"] == "2026-10-01"
    assert outcome.event_fields["end_date"] == "2026-10-04"


def test_a_timed_action_keeps_the_zone_the_model_resolved() -> None:
    """A Tokyo departure is a Tokyo instant; recording it as Shanghai would
    move it by an hour, which is the defect the zone field exists to close."""
    outcome, _, _ = _refusal_with(
        {**CAL_ARGS, "timezone": "Asia/Tokyo", "calendar": "出游计划"},
        CalendarResolved("uuid-chu-you", "出游计划"),
    )

    assert isinstance(outcome, DeviceActionIssued)
    assert outcome.event_fields["timezone"] == "Asia/Tokyo"
    assert outcome.event_fields["start_date"] is None
    assert outcome.event_fields["end_date"] is None


def test_the_resolved_identifier_wins_over_anything_the_request_carried() -> None:
    """The identifier is the routing decision, and the routing decision is the
    server's. A request that arrives carrying one -- however it got there --
    must not be able to name the calendar the phone writes to."""
    forged = {**CAL_ARGS, "calendar_identifier": "uuid-attacker", "calendar_title": "x"}

    class ForgingBridge(SpyBridge):
        def authorize(self, alias, arguments, device):
            self.authorized.append(alias)
            return self.registry.resolve(alias), forged

    outcome = cal_dispatcher(ForgingBridge()).resolve(
        tool="calendar.create_event",
        model_args=dict(CAL_ARGS),
        idempotency_key="action-key-1",
    )

    assert isinstance(outcome, DeviceActionIssued)
    assert outcome.event_fields["calendar_identifier"] == "uuid-ri-chang"
    assert outcome.event_fields["calendar_title"] == "日常安排"


def test_the_action_wire_version_is_the_contracts_not_the_requesters() -> None:
    """The version says what the action's fields mean, so it is a property of
    the tool the Host is issuing -- never of the client, and never of anything
    a caller can supply."""
    from personal_agent_core.tool_ir import TOOL_CONTRACTS

    contract = next(c for c in TOOL_CONTRACTS if c.name == "calendar.create_event")
    outcome, _, _ = _refusal(CAL_ARGS)

    assert isinstance(outcome, DeviceActionIssued)
    assert outcome.wire_version == contract.wire_version


def test_every_device_tool_has_an_issuance_policy() -> None:
    """The dispatch fork is derived from the IR, so a second device tool ships
    into it automatically -- and would otherwise be issued with nothing having
    checked it. A missing policy must fail loudly instead."""
    from personal_agent.api.calendar_issue import issuance_policy
    from personal_agent_core.tool_ir import TOOL_CONTRACTS

    for contract in TOOL_CONTRACTS:
        if contract.executor == "device":
            assert callable(issuance_policy(contract.name))

    with pytest.raises(AppError) as excinfo:
        issuance_policy("calendar.no_such_tool")
    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR


# --- design 2.5: the client capability gate ----------------------------------
#
# A v1 client ignores the fields that say *which* calendar an action targets and
# falls back to its default writable one, so "an old client ignores unknown
# fields" is not a safe compatibility story here -- it is the write-the-wrong-
# calendar path. Two gates close it: the issuance gate refuses to create the
# action, and the delivery gate refuses to hand over one that somehow exists.


def test_an_old_client_is_told_to_upgrade_rather_than_issued_an_action() -> None:
    """The issuance gate. A client that cannot implement this action is given
    no action at all -- and the refusal is a question, not a failure: nothing
    was written, the model did nothing wrong, and only the user can clear it."""
    outcome = cal_dispatcher(
        SpyBridge(), client_wire_version=CLIENT_WIRE_V1
    ).resolve(
        tool="calendar.create_event",
        model_args=dict(CAL_ARGS),
        idempotency_key="action-key-1",
    )

    assert isinstance(outcome, NeedsClarification)
    assert outcome.reason == CLIENT_UPGRADE_QUESTION


def test_the_issuance_gate_refuses_before_anything_is_validated_or_routed() -> None:
    """Design 2.5.2 puts this check *before* `authorize`. Order is the safety
    property here: authorising first would mean the refusal travelled through
    the governed path, and routing first would mean the service had already
    read the user's calendar directory on behalf of a client that can never
    receive the answer. Neither may happen."""
    bridge = SpyBridge()
    control = FakeControl()
    cal_dispatcher(bridge, control, client_wire_version=CLIENT_WIRE_V1).resolve(
        tool="calendar.create_event",
        model_args=dict(CAL_ARGS),
        idempotency_key="action-key-1",
    )

    assert bridge.authorized == []
    assert bridge.executed == []
    assert control.lookups == []


def test_the_gate_is_about_the_device_fork_and_not_about_the_caller() -> None:
    """A version header is a claim about *action semantics*, and only a
    device-executed tool has any. So the gate must not quietly become a general
    version requirement that would stop a v1 client from logging an expense."""
    from personal_agent_core.tool_ir import TOOL_CONTRACTS

    device_tools = {
        contract.name for contract in TOOL_CONTRACTS if contract.executor == "device"
    }

    assert "finance.log_expense" not in device_tools
    # Its contract declares the oldest version, so no client can ever be too old
    # for it -- which is what makes the gate a no-op outside the device fork.
    expense = next(c for c in TOOL_CONTRACTS if c.name == "finance.log_expense")
    assert expense.wire_version <= CLIENT_WIRE_V1


def test_a_client_exactly_at_the_required_version_is_served() -> None:
    """The comparison is inclusive. An off-by-one here would lock out every
    correctly-upgraded client, and the failure would look like the server
    ignoring the user."""
    from personal_agent_core.tool_ir import TOOL_CONTRACTS

    contract = next(c for c in TOOL_CONTRACTS if c.name == "calendar.create_event")
    outcome = cal_dispatcher(
        SpyBridge(), client_wire_version=contract.wire_version
    ).resolve(
        tool="calendar.create_event",
        model_args=dict(CAL_ARGS),
        idempotency_key="action-key-1",
    )

    assert isinstance(outcome, DeviceActionIssued)


def test_the_delivery_gate_withholds_a_v2_action_from_a_v1_caller(op_session) -> None:
    """The delivery gate (design 2.5.3), and the reason it is not redundant with
    the issuance gate: the two read the version of *different* requests. Between
    issuing and delivering, the same phone can be restored or downgraded. The
    action is withheld rather than degraded, and the operation stays parked so
    the timeout sweep -- not a claim of no-write -- owns the outcome."""
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
                wire_version=2,
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=keyring,
        )
        session.commit()
        session.refresh(operation)

        withheld = _operation_projection(
            keyring, operation, client_wire_version=CLIENT_WIRE_V1
        )
        # Absent entirely -- not empty, not null: a client that cannot execute
        # the action must not be told there is one.
        assert "device_actions" not in withheld
        # The same row, one request later, from a client that can read it.
        assert "device_actions" in _operation_projection(
            keyring, operation, client_wire_version=CLIENT_WIRE_V2
        )
        # Withholding is not settling: the write may exist, and the operation
        # keeps saying so rather than claiming otherwise.
        assert operation.state == "source_in_progress"


def test_the_delivery_gate_reads_the_action_not_the_contract(op_session) -> None:
    """The comparison uses the *sealed* action's `wire_version`. That is the
    version of the fields actually in the envelope; the contract's is whatever
    the IR says today, so a later IR bump would raise it past an action sealed
    under the older semantics -- and the gate would then deliver fields the
    client was never compared against."""
    from personal_agent.api.app import _operation_projection
    from personal_agent.api.device_action_projection import seal_device_action
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
                wire_version=2,
                event_fields=dict(CAL_ARGS),
            ),
            dispatcher=None,
            keyring=None,
            now=now,
            action_keyring=keyring,
        )
        session.commit()
        session.refresh(operation)

        # A seal at a version no client in this build implements.
        operation.encrypted_device_action = seal_device_action(
            keyring,
            operation_id=operation.operation_id,
            action={
                "action_id": "action-key-1",
                "tool": "calendar.create_event",
                "wire_version": 3,
                "event": dict(CAL_ARGS),
            },
        )
        assert "device_actions" not in _operation_projection(
            keyring, operation, client_wire_version=CLIENT_WIRE_V2
        )


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param(None, 1, id="absent"),
        pytest.param("", 1, id="empty"),
        pytest.param("two", 1, id="not-a-number"),
        pytest.param("2.0", 1, id="not-an-integer"),
        pytest.param("0", 1, id="zero"),
        pytest.param("-3", 1, id="negative"),
        pytest.param("2", 2, id="plain"),
        pytest.param(" 2 ", 2, id="padded"),
        pytest.param("3", 3, id="newer-than-this-build"),
    ],
)
def test_a_capability_header_is_read_strictly(raw, expected) -> None:
    """Every unreadable value is version 1 -- never an error, never a guess. A
    client that predates the header must keep working on the endpoints that
    carry no action, and a mangled value must not be promoted into a contract
    the client does not implement. A version *newer* than this build passes
    through unchanged: it is the client's claim, and what to do about it is the
    comparison's job, not the parser's."""
    from personal_agent_core.tool_ir import parse_client_wire_version

    assert parse_client_wire_version(raw) == expected


def test_the_version_comparison_is_the_one_both_gates_use() -> None:
    """One predicate, so the two gates cannot drift into one of them failing
    open -- which would hand a v2 action to a client that writes it into the
    wrong calendar."""
    from personal_agent_core.tool_ir import (
        client_supports_wire_version,
        parse_client_wire_version,
    )

    assert client_supports_wire_version(client=2, required=2)
    assert client_supports_wire_version(client=3, required=2)
    assert not client_supports_wire_version(client=1, required=2)
    # An unreadable header flows through both gates as the oldest contract.
    assert not client_supports_wire_version(
        client=parse_client_wire_version("nonsense"), required=2
    )
