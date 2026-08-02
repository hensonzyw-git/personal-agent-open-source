"""DEV-027: the Agent-side Finance dispatcher, and the control-plane client.

The dispatcher is the seam between an operation and a real MCP call, so the
cases here are the ones that decide whether an operation ends up honest:

- a write's `resolve` must send **nothing**, because that is what makes a cancel
  before commit genuinely pre-submit;
- `POSSIBLE_DUPLICATE` must become a parking outcome only when a pending check is
  actually readable from the control plane, and a safe failure otherwise -- never
  a park with no answerable question, and never a silent success;
- a timeout, a transport failure and a missing record id must all be *unknown*
  commits. Reporting any of them as failed would let a real write disappear.

The control client is covered against the same kinds of hostility: a base URL
that is not loopback, a non-200, a body that is not JSON, and a body whose shape
does not carry what it claims.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from personal_agent.api.control_client import (
    ControlPlaneError,
    FinanceControlClient,
)
from personal_agent.api.finance_dispatcher import (
    DispatcherContext,
    McpFinanceDispatcher,
    tool_call_fingerprint,
)
from personal_agent.api.intent import WriteIntent
from personal_agent.api.orchestrator import (
    CommitClarificationZeroWrite,
    CommitDuplicateZeroWrite,
    CommitFailedSafe,
    CommitUnknown,
    ReadCompleted,
    Resolved,
    ResolveFailedSafe,
    Written,
)
from personal_agent.mcp_client.core import McpTimeoutError, McpTransportError
from personal_agent.policy.bridge import (
    BridgeExecutionResult,
    DeviceAuthorization,
)
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import (
    ServiceKey,
    ServiceKeyRing,
)
from cryptography.hazmat.primitives.asymmetric import ec


DEVICE = DeviceAuthorization(
    device_id="device-1",
    status="active",
    scopes=frozenset({"finance.expense.write"}),
    allowed_tools={"finance.log_expense", "finance.query_expenses"},
    allowed_tools_version="atv-1",
)
EXPENSE_ARGS = {
    "name": "午饭",
    "input_amount": "20.00",
    "input_currency": "CNY",
    "occurred_on": "2026-07-24",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}
INTENT = WriteIntent(tool="finance.log_expense", model_args=EXPENSE_ARGS)


def ring() -> ServiceKeyRing:
    key = ec.generate_private_key(ec.SECP256R1())
    return ServiceKeyRing(active=ServiceKey("svc-1", key, key.public_key()))


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


class FakeBridge:
    """Stands in for the governed bridge; records the Host Context it was given."""

    def __init__(
        self,
        *,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.registry = FakeRegistry(
            aliases
            or {
                "finance.log_expense": "finance.log_expense",
                "finance.query_expenses": "finance.query_expenses",
            }
        )
        self.result = result if result is not None else {"record_id": "rec1"}
        self.error = error
        self.calls: list[dict[str, Any]] = []
        #: Raise `error` from `authorize` instead of `execute`, i.e. refuse on
        #: this side before anything is dispatched -- what the real bridge does
        #: for a scope or allowlist denial.
        self.refuses_locally = False

    def authorize(self, alias, arguments, device):
        if self.refuses_locally and self.error is not None:
            raise self.error
        return self.registry.resolve(alias), arguments

    async def execute(self, alias, arguments, device, *, call_context):
        self.calls.append(
            {
                "alias": alias,
                "arguments": arguments,
                "host": call_context.host,
            }
        )
        if self.error is not None:
            raise self.error
        return BridgeExecutionResult(
            trusted_result=self.result, model_result=self.result
        )


class FakeControl:
    def __init__(
        self,
        *,
        pending=None,
        error: Exception | None = None,
        execution: dict[str, Any] | None = None,
        execution_error: Exception | None = None,
    ) -> None:
        self.pending = pending
        self.error = error
        #: Finance's execution row for the key, or None when it never existed.
        #: `None` is the right default here: every failure this harness injects
        #: is one Finance raises before an execution row is created.
        self.execution = execution
        self.execution_error = execution_error
        self.asked: list[str] = []
        self.asked_execution: list[str] = []

    async def get_pending_duplicate_check(self, key: str):
        self.asked.append(key)
        if self.error is not None:
            raise self.error
        return self.pending

    async def get_execution(self, key: str):
        self.asked_execution.append(key)
        if self.execution_error is not None:
            raise self.execution_error
        return self.execution


def dispatcher(bridge, control=None) -> McpFinanceDispatcher:
    return McpFinanceDispatcher(
        bridge=bridge,
        control=control or FakeControl(),
        signing_ring=ring(),
        context=DispatcherContext(
            device=DEVICE,
            user_id="henson",
            agent_id="agent-1",
            conversation_trace_id="trace-1",
        ),
    )


class Pending:
    def __init__(
        self, check_id: str, existing_summary: str = "午饭 ¥45 餐饮 · 个人支出"
    ) -> None:
        self.duplicate_check_id = check_id
        self.expires_at = "2026-07-24T12:00:00Z"
        self.existing_summary = existing_summary


# --- resolve -----------------------------------------------------------------


def test_resolving_a_write_sends_nothing_at_all() -> None:
    bridge = FakeBridge()
    outcome = dispatcher(bridge).resolve(
        tool="finance.log_expense", model_args=EXPENSE_ARGS
    )

    assert isinstance(outcome, Resolved)
    assert outcome.intent == INTENT
    # The whole point: a cancel at this moment is honestly pre-submit.
    assert bridge.calls == []


def test_resolving_a_read_executes_it_and_completes() -> None:
    bridge = FakeBridge(result={"view": "total", "personal_spend_total_cny": "1"})
    outcome = dispatcher(bridge).resolve(
        tool="finance.query_expenses", model_args={"view": "total"}
    )

    assert isinstance(outcome, ReadCompleted)
    assert json.loads(outcome.result)["view"] == "total"
    assert len(bridge.calls) == 1


def test_a_failing_read_is_a_safe_failure() -> None:
    bridge = FakeBridge(error=AppError(ErrorCode.SOURCE_UNAVAILABLE))
    outcome = dispatcher(bridge).resolve(
        tool="finance.query_expenses", model_args={"view": "total"}
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == ErrorCode.SOURCE_UNAVAILABLE.value


def test_an_unknown_alias_never_reaches_the_connector() -> None:
    bridge = FakeBridge()
    outcome = dispatcher(bridge).resolve(
        tool="finance.invented_tool", model_args={}
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert bridge.calls == []


# --- commit ------------------------------------------------------------------


def test_a_verified_write_returns_its_record_id() -> None:
    bridge = FakeBridge(result={"record_id": "recABC"})
    outcome = dispatcher(bridge).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    assert outcome == Written(record_id="recABC")
    host = bridge.calls[0]["host"]
    assert host.idempotency_key == "idem-1"
    assert host.tool == "finance.log_expense"
    assert host.duplicate_override is None
    assert host.request_fingerprint == tool_call_fingerprint(
        "finance.log_expense", EXPENSE_ARGS
    )


def test_the_override_travels_on_the_host_context_not_the_arguments() -> None:
    bridge = FakeBridge()
    dispatcher(bridge).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override="chk-1"
    )

    call = bridge.calls[0]
    assert call["host"].duplicate_override == "chk-1"
    assert "duplicate_override" not in call["arguments"]


def test_a_replayed_commit_keeps_the_same_fingerprint() -> None:
    """Otherwise a legitimate retry would be refused as a different request."""
    first = FakeBridge()
    second = FakeBridge()
    dispatcher(first).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )
    dispatcher(second).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    assert (
        first.calls[0]["host"].request_fingerprint
        == second.calls[0]["host"].request_fingerprint
    )


def test_a_duplicate_parks_with_the_id_read_from_the_control_plane() -> None:
    bridge = FakeBridge(error=AppError(ErrorCode.POSSIBLE_DUPLICATE))
    control = FakeControl(pending=Pending("chk-9"))
    outcome = dispatcher(bridge, control).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    assert isinstance(outcome, CommitDuplicateZeroWrite)
    assert outcome.duplicate_check_id == "chk-9"
    assert outcome.existing_summary == "午饭 ¥45 餐饮 · 个人支出"
    assert control.asked == ["idem-1"]


@pytest.mark.parametrize(
    "control",
    [
        FakeControl(pending=None),
        FakeControl(error=ControlPlaneError("down")),
    ],
)
def test_a_duplicate_with_no_answerable_check_fails_safe(control) -> None:
    bridge = FakeBridge(error=AppError(ErrorCode.POSSIBLE_DUPLICATE))
    outcome = dispatcher(bridge, control).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    # Nothing was written either way, and there is no decision to offer.
    assert outcome == CommitFailedSafe(reason="duplicate_check_unavailable")


def test_a_clarification_parks_without_a_write() -> None:
    bridge = FakeBridge(error=AppError(ErrorCode.CLARIFICATION_REQUIRED))
    outcome = dispatcher(bridge).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    assert isinstance(outcome, CommitClarificationZeroWrite)
    assert outcome.question


@pytest.mark.parametrize(
    "error",
    [
        McpTimeoutError("timed out"),
        McpTransportError("connection reset"),
        AppError(ErrorCode.SOURCE_COMMIT_UNKNOWN),
        AppError(ErrorCode.SOURCE_COMMITTED_MISMATCH),
    ],
)
def test_a_lost_response_is_unknown_never_failed(error) -> None:
    bridge = FakeBridge(error=error)
    outcome = dispatcher(bridge).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    assert isinstance(outcome, CommitUnknown)


@pytest.mark.parametrize(
    "result", [{}, {"record_id": ""}, {"record_id": "   "}, {"record_id": 7}]
)
def test_a_result_without_a_usable_record_id_is_unknown(result) -> None:
    """The call may still have written; only a real id proves it did not."""
    bridge = FakeBridge(result=result)
    outcome = dispatcher(bridge).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    assert outcome == CommitUnknown(reason="missing_verified_record_id")


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.SCOPE_DENIED,
        ErrorCode.INVALID_ARGUMENT,
        ErrorCode.SOURCE_SCHEMA_CHANGED,
        ErrorCode.FX_RATE_UNAVAILABLE,
    ],
)
def test_a_refusal_before_the_source_is_a_safe_failure(code) -> None:
    bridge = FakeBridge(error=AppError(code))
    outcome = dispatcher(bridge).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    assert outcome == CommitFailedSafe(reason=code.value)


# --- the control-plane client ------------------------------------------------


def control_client(handler, *, base_url: str = "http://127.0.0.1:8848"):
    return FinanceControlClient(
        base_url=base_url,
        signing_ring=ring(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://agent.example.invalid",
        "https://127.0.0.1:8848",
        "http://10.0.0.5:8848",
        "not-a-url",
    ],
)
def test_a_control_base_url_that_is_not_loopback_is_refused(base_url) -> None:
    """A control token must never leave this host."""
    with pytest.raises(ValueError):
        FinanceControlClient(base_url=base_url, signing_ring=ring())


def test_the_pending_check_is_read_with_a_resource_bound_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "status": "found",
                "duplicate_check": {
                    "duplicate_check_id": "chk-1",
                    "status": "awaiting_decision",
                    "created_at": "2026-07-24T03:00:00Z",
                    "expires_at": "2026-07-24T03:15:00Z",
                    "existing_summary": "午饭 ¥45 餐饮 · 个人支出",
                },
            },
        )

    client = control_client(handler)
    pending = asyncio.run(client.get_pending_duplicate_check("idem-1"))

    assert pending.duplicate_check_id == "chk-1"
    assert pending.existing_summary == "午饭 ¥45 餐饮 · 个人支出"
    assert seen["path"] == "/internal/v1/duplicate-checks/idem-1"
    assert seen["auth"].startswith("Bearer ")


def test_not_found_is_an_explicit_branch() -> None:
    client = control_client(
        lambda request: httpx.Response(200, json={"status": "not_found"})
    )
    assert asyncio.run(client.get_pending_duplicate_check("idem-1")) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"error": {"code": "HOST_CONTEXT_MISMATCH"}}),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"status": "found"}),
        httpx.Response(200, json={"status": "found", "duplicate_check": {}}),
        httpx.Response(
            200,
            json={
                "status": "found",
                "duplicate_check": {"duplicate_check_id": ""},
            },
        ),
        httpx.Response(200, json={"status": "surprise"}),
    ],
)
def test_an_unreadable_control_answer_is_never_silently_empty(response) -> None:
    client = control_client(lambda request: response)
    with pytest.raises(ControlPlaneError):
        asyncio.run(client.get_pending_duplicate_check("idem-1"))


def test_a_transport_failure_is_a_control_plane_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ControlPlaneError):
        asyncio.run(control_client(handler).get_pending_duplicate_check("k"))
