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
from personal_agent_core.errors import AppError, ClarificationQuestion, ErrorCode
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


#: A query result in the shape the real `finance.query_expenses` output schema
#: produces. `resolve` must project it rather than echo the raw dict.
QUERY_TOTAL = {
    "status": "ok",
    "view": "total",
    "filters_applied": {
        "date_range": {"start": "2026-01-01", "end": "2026-12-31"},
        "categories": ["网球"],
        "name_contains": [],
        "is_family_expense": "all",
        "personal_amount_cny": None,
    },
    "metric": "personal_spend_total_cny",
    "record_count": 2,
    "personal_spend_total_cny": "1200.00",
    "source_system": "feishu_bitable",
    "evidence": {
        "kind": "aggregate_query",
        "query_id": "qry_1",
        "config_checksum": "cfg",
        "schema_snapshot_checksum": "schema",
        "scanned_pages": 1,
        "matched_count": 2,
        "started_at": "2026-08-12T00:00:00Z",
        "completed_at": "2026-08-12T00:00:01Z",
    },
}


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
    bridge = FakeBridge(result=QUERY_TOTAL)
    outcome = dispatcher(bridge).resolve(
        tool="finance.query_expenses", model_args={"view": "total"}
    )

    assert isinstance(outcome, ReadCompleted)
    assert outcome.projection is not None
    # The durable carrier is the whitelisted projection, not the raw result.
    assert json.loads(outcome.result) == outcome.projection.to_dict()
    assert json.loads(outcome.result)["view"] == "total"
    # The deterministic text fallback is derived from the projection only.
    assert outcome.answer == "共 2 条记录，个人支出合计 ¥1200.00"
    assert len(bridge.calls) == 1


def test_a_query_result_with_an_unknown_view_fails_closed() -> None:
    bridge = FakeBridge(result={**QUERY_TOTAL, "view": "pie_chart"})
    outcome = dispatcher(bridge).resolve(
        tool="finance.query_expenses", model_args={"view": "total"}
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "query_result_unreadable"
    # A refused projection carries no result field at all, so the raw result
    # cannot leak; only the fixed reason survives.
    assert not hasattr(outcome, "result")


def test_a_query_result_with_unknown_fields_fails_closed() -> None:
    bridge = FakeBridge(result={**QUERY_TOTAL, "provider_prose": "已写入成功"})
    outcome = dispatcher(bridge).resolve(
        tool="finance.query_expenses", model_args={"view": "total"}
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "query_result_unreadable"


def test_a_query_result_with_wrong_field_types_fails_closed() -> None:
    bridge = FakeBridge(result={**QUERY_TOTAL, "record_count": "two"})
    outcome = dispatcher(bridge).resolve(
        tool="finance.query_expenses", model_args={"view": "total"}
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "query_result_unreadable"


def test_a_query_result_that_is_not_json_fails_closed() -> None:
    bridge = FakeBridge(result="not a dict")
    outcome = dispatcher(bridge).resolve(
        tool="finance.query_expenses", model_args={"view": "total"}
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "query_result_unreadable"


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


# --- review R4: the read fork is derived from the IR, not hand-listed --------

#: A query result in the shape the real `calendar.query_events` output schema
#: produces. It is a governed read, but its projection is the calendar one, so
#: it must never enter the write flow (reproduced: `READ_TOOLS` was a
#: hand-listed set that did not hold it, and `resolve` returned a WriteIntent,
#: parking the operation at CommitUnknown("missing_verified_record_id")) and it
#: must never echo raw JSON as `answer`.
CALENDAR_QUERY = {
    "status": "ok",
    "events": [
        {
            "event_identifier": "ev-1",
            "calendar_identifier": "cal-1",
            "title": "网球",
            "start": "2026-09-07T15:00:00+08:00",
            "end": "2026-09-07T16:30:00+08:00",
            "all_day": False,
            "location": None,
            "notes": None,
            "created_by_agent": False,
        }
    ],
    "record_count": 1,
    "next_cursor": None,
    "data_as_of": "2026-09-07T07:30:00+00:00",
    "mirror_stale": False,
    "source_system": "apple_calendar_mirror",
}


def _calendar_bridge(result: Any) -> FakeBridge:
    return FakeBridge(
        result=result,
        aliases={"calendar.query_events": "calendar.query_events"},
    )


def test_resolving_a_calendar_query_executes_it_and_completes() -> None:
    bridge = _calendar_bridge(CALENDAR_QUERY)
    outcome = dispatcher(bridge).resolve(
        tool="calendar.query_events",
        model_args={
            "start": "2026-09-07T00:00:00+08:00",
            "end": "2026-09-08T00:00:00+08:00",
        },
    )

    # Not a WriteIntent: a governed read must execute, never park in the
    # commit flow waiting for a record id it will never have.
    assert isinstance(outcome, ReadCompleted)
    assert outcome.projection is not None
    assert outcome.projection["record_count"] == 1
    assert outcome.projection["events"][0]["title"] == "网球"
    # The durable carrier is the whitelisted projection, not the raw result.
    assert json.loads(outcome.result) == outcome.projection
    assert json.loads(outcome.result)["data_as_of"] == "2026-09-07T07:30:00+00:00"
    # The deterministic text fallback is derived from the projection only.
    assert outcome.answer == "共 1 条日程，数据截至 2026-09-07T07:30:00+00:00"
    assert len(bridge.calls) == 1


def test_a_calendar_query_result_with_unknown_fields_fails_closed() -> None:
    bridge = _calendar_bridge(
        {**CALENDAR_QUERY, "provider_prose": "放心，日历我改过了"}
    )
    outcome = dispatcher(bridge).resolve(
        tool="calendar.query_events",
        model_args={
            "start": "2026-09-07T00:00:00+08:00",
            "end": "2026-09-08T00:00:00+08:00",
        },
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "query_result_unreadable"
    assert not hasattr(outcome, "result")


def test_a_calendar_query_result_with_wrong_field_types_fails_closed() -> None:
    bridge = _calendar_bridge({**CALENDAR_QUERY, "record_count": "one"})
    outcome = dispatcher(bridge).resolve(
        tool="calendar.query_events",
        model_args={
            "start": "2026-09-07T00:00:00+08:00",
            "end": "2026-09-08T00:00:00+08:00",
        },
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "query_result_unreadable"


def test_a_calendar_query_result_from_a_foreign_source_fails_closed() -> None:
    # `source_system` is the projection's identity: anything other than the
    # Apple mirror is not a result this projection may show.
    bridge = _calendar_bridge({**CALENDAR_QUERY, "source_system": "模型说的"})
    outcome = dispatcher(bridge).resolve(
        tool="calendar.query_events",
        model_args={
            "start": "2026-09-07T00:00:00+08:00",
            "end": "2026-09-08T00:00:00+08:00",
        },
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == "query_result_unreadable"


def test_a_failing_calendar_read_is_a_safe_failure() -> None:
    bridge = _calendar_bridge(None)
    bridge.error = AppError(ErrorCode.SOURCE_UNAVAILABLE)
    outcome = dispatcher(bridge).resolve(
        tool="calendar.query_events",
        model_args={
            "start": "2026-09-07T00:00:00+08:00",
            "end": "2026-09-08T00:00:00+08:00",
        },
    )

    assert isinstance(outcome, ResolveFailedSafe)
    assert outcome.reason == ErrorCode.SOURCE_UNAVAILABLE.value


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


def test_a_closed_finance_question_is_preserved_for_the_continuation() -> None:
    bridge = FakeBridge(
        error=AppError(
            ErrorCode.CLARIFICATION_REQUIRED,
            clarification_question=ClarificationQuestion.EXPENSE_CATEGORY,
        )
    )
    outcome = dispatcher(bridge).commit(
        intent=INTENT, idempotency_key="idem-1", duplicate_override=None
    )

    assert outcome == CommitClarificationZeroWrite(
        ClarificationQuestion.EXPENSE_CATEGORY.value
    )


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


# --- DEV-039: the write kill switch, seen from the dispatcher ----------------


def test_a_kill_switch_refusal_at_the_agent_layer_is_a_safe_failure() -> None:
    """Refused before the bridge dispatched, so zero writes is knowable here.

    Routing it through Finance would be strictly worse: the control plane is
    exactly what an operator may be in the middle of stopping, and "writes are
    off" must not degrade into "unknown, needs manual review" because the
    service the operator just quietened could not be asked.
    """
    bridge = FakeBridge(error=AppError(ErrorCode.WRITES_DISABLED))
    bridge.refuses_locally = True
    control = FakeControl()
    outcome = dispatcher(bridge, control).commit(
        intent=INTENT, idempotency_key="idem-switch", duplicate_override=None
    )

    assert outcome == CommitFailedSafe(reason=ErrorCode.WRITES_DISABLED.value)
    assert control.asked_execution == []
    assert bridge.calls == []


def test_a_kill_switch_refusal_from_finance_is_decided_by_the_execution_row() -> None:
    """The switch flipped between the two layers, or the Agent was bypassed.

    Finance refuses before the handler runs, so there is no execution row -- and
    the dispatcher reaches that conclusion by *reading* the row rather than by
    special-casing another error code. A new code that never learns about this
    branch still gets the right answer.
    """
    bridge = FakeBridge(error=AppError(ErrorCode.WRITES_DISABLED))
    control = FakeControl(execution=None)
    outcome = dispatcher(bridge, control).commit(
        intent=INTENT, idempotency_key="idem-switch-2", duplicate_override=None
    )

    assert outcome == CommitFailedSafe(reason=ErrorCode.WRITES_DISABLED.value)
    assert control.asked_execution == ["idem-switch-2"]
