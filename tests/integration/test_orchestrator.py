"""DEV-026 D+F: the orchestrator and the duplicate decision flow.

Offline: a real Agent database, a real Agent key ring, and fakes for the model
interpreter and the Finance dispatcher. The safety properties under test are the
ones that do not depend on a real model or a live Base -- the state walk, the
`source_in_progress` boundary, and the Host-bound override.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pytest

from context_envelopes import envelope_for
from personal_agent.api.duplicate_flow import decide_duplicate
from personal_agent.api.operation_state import is_recoverable, is_terminal
from personal_agent.api.recovery import _RECOVERY_PATHS
from personal_agent.api.intent import WriteIntent, open_intent
from personal_agent.api.operation_store import (
    open_operation,
    request_cancel,
)
from personal_agent.api.orchestrator import (
    CommitClarificationZeroWrite,
    CommitDuplicateZeroWrite,
    CommitFailedSafe,
    CommitUnknown,
    Clarification,
    DirectAnswer,
    FailSafeInterpretation,
    NeedsClarification,
    PossibleDuplicate,
    ReadCompleted,
    Resolved,
    ResolveFailedSafe,
    ToolCall,
    Written,
    run_operation,
)
from personal_agent.context.continuation import ClarificationContext
from personal_agent.policy.bridge import VisibleTool
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Device, Operation
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode, ModelFailureReason


NOW = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id="dev-1",
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint="T",
                status="active",
                scopes="[]",
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.flush()
        yield session
    engine.dispose()


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent",
    )


# --- fakes -------------------------------------------------------------------


@lru_cache(maxsize=None)
def _envelope(text: str):
    """One real, budget-validated envelope per distinct message.

    Built by the production `ContextBuilder` over its own throwaway database: an
    envelope cannot be constructed directly, and a hand-made stand-in would let
    these tests pass against a shape the builder never produces.
    """
    return envelope_for(Path(tempfile.mkdtemp()), user_text=text)


class FakeInterpreter:
    def __init__(self, result) -> None:
        self.result = result

    def interpret(self, *, envelope):
        self.envelope = envelope
        return self.result


class FakeDispatcher:
    def __init__(self, *, resolve, commit=None) -> None:
        self._resolve = resolve
        self._commit = commit
        self.commit_calls: list[dict] = []
        self.resolve_calls: list[dict] = []

    def resolve(self, *, tool, model_args):
        self.resolve_calls.append({"tool": tool, "model_args": model_args})
        return self._resolve

    def commit(self, *, intent, idempotency_key, duplicate_override):
        self.commit_calls.append(
            {
                "intent": intent,
                "idempotency_key": idempotency_key,
                "duplicate_override": duplicate_override,
            }
        )
        return self._commit


def allow(*, tool, model_args):
    return dict(model_args)


def deny(*, tool, model_args):
    raise AppError(ErrorCode.SCOPE_DENIED, internal_detail="not granted")


def _fresh_operation(session, key="req-1") -> Operation:
    op = open_operation(
        session,
        device_id="dev-1",
        client_request_id=key,
        request_fingerprint="fp",
        now=NOW,
    ).operation
    session.flush()
    return op


def _run(session, op, *, interpreter, dispatcher, keyring, authorize=allow,
         text="午饭 45 个人支出", build_context=None, now=NOW):
    return run_operation(
        session,
        op,
        # The orchestrator only ever sees an already-assembled turn; the builder
        # itself is exercised in `test_cap001_builder.py` and in the API tests.
        build_context=build_context or (lambda: _envelope(text)),
        interpreter=interpreter,
        dispatcher=dispatcher,
        authorize=authorize,
        keyring=keyring,
        now=now,
    )


# --- the plain paths ---------------------------------------------------------


def test_a_direct_answer_succeeds_with_no_tool(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=FakeInterpreter(DirectAnswer("你好")),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        # Non-bookkeeping input: a direct answer is fine when no write is being
        # claimed. The default `_run` text is a bookkeeping shape, which the
        # DEV-040 guard refuses, so it must be overridden here.
        text="你好",
    )
    assert result.state == "succeeded"
    assert result.answer == "你好"
    session.refresh(op)
    assert op.state == "succeeded"


def test_a_bookkeeping_direct_answer_fails_safe(session, keyring) -> None:
    """`DEV-040`: prose "我来帮你记录…" on a bookkeeping request is not a write.

    The model produced exactly this for a whole day without calling any tool,
    and every operation was recorded `succeeded` with `record_id=None`. A
    `DirectAnswer` carries no side effect, so the guard refuses it; the reason
    is the stable `BOOKKEEPING_TOOL_REQUIRED` code, never a claimed success.
    """
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=FakeInterpreter(
            DirectAnswer("我来帮你记录这笔支出。现在为你提交记录。")
        ),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        text="咖啡 18 个人支出",
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "BOOKKEEPING_TOOL_REQUIRED"
    assert result.record_id is None
    session.refresh(op)
    assert op.state == "failed_safe"
    assert op.failure_reason == "BOOKKEEPING_TOOL_REQUIRED"


def test_natural_bookkeeping_shorthand_cannot_succeed_as_prose(
    session, keyring
) -> None:
    op = _fresh_operation(session)
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(DirectAnswer("已经帮你记好了")),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        text="午饭 38",
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "BOOKKEEPING_TOOL_REQUIRED"


def test_a_finance_turn_cannot_be_routed_to_a_non_finance_tool(
    session, keyring
) -> None:
    op = _fresh_operation(session)
    dispatcher = FakeDispatcher(resolve=ReadCompleted("unused"))
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(ToolCall("meta.capabilities", {})),
        dispatcher=dispatcher,
        keyring=keyring,
        text="午饭 38",
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "BOOKKEEPING_TOOL_REQUIRED"
    assert dispatcher.resolve_calls == []


def test_a_finance_query_cannot_succeed_from_model_memory(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(DirectAnswer("今年网球共 100 元")),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        text="查一下我今年打网球花了多少钱",
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "FINANCE_TOOL_REQUIRED"


def test_a_finance_query_cannot_be_routed_to_a_finance_write(
    session, keyring
) -> None:
    op = _fresh_operation(session)
    dispatcher = FakeDispatcher(resolve=ReadCompleted("unused"))
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(
            ToolCall("finance.log_expense", {"name": "网球", "amount": "100"})
        ),
        dispatcher=dispatcher,
        keyring=keyring,
        text="查一下我今年打网球花了多少钱",
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "FINANCE_TOOL_REQUIRED"
    assert dispatcher.resolve_calls == []


def test_a_finance_write_cannot_be_routed_to_a_finance_read(
    session, keyring
) -> None:
    op = _fresh_operation(session)
    dispatcher = FakeDispatcher(resolve=ReadCompleted("query-result"))
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(
            ToolCall("finance.query_expenses", {"view": "total"})
        ),
        dispatcher=dispatcher,
        keyring=keyring,
        text="午饭 38",
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "BOOKKEEPING_TOOL_REQUIRED"
    assert result.record_id is None
    assert dispatcher.resolve_calls == []


def test_a_finance_query_clarification_keeps_the_exact_tool_requirement(
    session, keyring, tmp_path
) -> None:
    envelope = envelope_for(
        tmp_path,
        user_text="今年",
        clarification=ClarificationContext(
            original_user_text="查一下网球花了多少钱",
            question="查哪一年？",
            completed_exchanges=(),
            source_operation_ids=("placeholder",),
        ),
    )
    op = _fresh_operation(session)
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(DirectAnswer("今年共 100 元")),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        build_context=lambda: envelope,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "FINANCE_TOOL_REQUIRED"


def test_an_unbound_finance_retry_stops_before_the_model(
    session, keyring, tmp_path
) -> None:
    op = _fresh_operation(session)
    interpreter = FakeInterpreter(
        ToolCall("finance.log_expense", {"name": "午饭", "amount": "38"})
    )
    envelope = envelope_for(tmp_path, user_text="重新记")
    result = _run(
        session,
        op,
        interpreter=interpreter,
        dispatcher=FakeDispatcher(resolve=ReadCompleted("unused")),
        keyring=keyring,
        build_context=lambda: envelope,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "BOOKKEEPING_TOOL_REQUIRED"
    assert not hasattr(interpreter, "envelope")


def test_a_visible_finance_query_cannot_be_called_unsupported_by_the_model(
    session, keyring, tmp_path
) -> None:
    op = _fresh_operation(session)
    envelope = envelope_for(
        tmp_path,
        user_text="查一下我今年打网球花了多少钱",
        tools=(
            VisibleTool(
                alias="finance.query_expenses",
                description="查询支出",
                input_schema={"type": "object", "properties": {}},
                risk_level="R1",
                required_scopes=("finance.expense.read",),
            ),
        ),
    )
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(
            FailSafeInterpretation("TOOL_NOT_ALLOWLISTED")
        ),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        build_context=lambda: envelope,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "FINANCE_TOOL_REQUIRED"


def test_a_visible_finance_write_cannot_be_called_unsupported_by_the_model(
    session, keyring, tmp_path
) -> None:
    op = _fresh_operation(session)
    envelope = envelope_for(
        tmp_path,
        user_text="午饭 38",
        tools=(
            VisibleTool(
                alias="finance.log_expense",
                description="记录支出",
                input_schema={"type": "object", "properties": {}},
                risk_level="R2",
                required_scopes=("finance.expense.write",),
            ),
        ),
    )
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(
            FailSafeInterpretation("TOOL_NOT_ALLOWLISTED")
        ),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        build_context=lambda: envelope,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "BOOKKEEPING_TOOL_REQUIRED"


def test_other_visible_finance_tools_do_not_make_query_look_available(
    session, keyring, tmp_path
) -> None:
    op = _fresh_operation(session)
    envelope = envelope_for(
        tmp_path,
        user_text="查一下我今年打网球花了多少钱",
        tools=(
            VisibleTool(
                alias="finance.log_expense",
                description="记录支出",
                input_schema={"type": "object", "properties": {}},
                risk_level="R2",
                required_scopes=("finance.expense.write",),
            ),
        ),
    )
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(
            FailSafeInterpretation("TOOL_NOT_ALLOWLISTED")
        ),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        build_context=lambda: envelope,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "TOOL_NOT_ALLOWLISTED"


class RaisingInterpreter:
    def interpret(self, *, envelope):
        from personal_agent.api.orchestrator import InterpreterError

        raise InterpreterError("model down")


def test_a_model_failure_fails_safe(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=RaisingInterpreter(),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "model_unavailable"


def test_a_classified_model_failure_persists_its_safe_reason(session, keyring) -> None:
    class TimeoutInterpreter:
        def interpret(self, *, envelope):
            from personal_agent.api.orchestrator import InterpreterError

            raise InterpreterError(
                "provider timed out",
                failure_reason=ModelFailureReason.PROVIDER_TIMEOUT.value,
            )

    op = _fresh_operation(session)
    result = _run(
        session,
        op,
        interpreter=TimeoutInterpreter(),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == ModelFailureReason.PROVIDER_TIMEOUT.value
    assert op.failure_reason == ModelFailureReason.PROVIDER_TIMEOUT.value


def test_a_read_completes_without_touching_the_write_states(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.query_expenses", {"view": "total"})),
        dispatcher=FakeDispatcher(resolve=ReadCompleted("本月 ¥2093")),
        keyring=keyring,
        text="查一下这个月花了多少钱",
    )
    assert result.state == "succeeded"
    assert result.answer == "本月 ¥2093"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "finance.log_expense",
            {
                "name": "午饭",
                "input_amount": "45.00",
                "input_currency": "CNY",
                "is_family_expense": False,
                "entry_kind": "expense",
                "category": "餐饮",
            },
        ),
        (
            "finance.log_income",
            {
                "income_description": "工资",
                "input_amount": "100.00",
                "input_currency": "CNY",
            },
        ),
    ],
)
def test_omitted_finance_date_uses_durable_message_receipt_day(
    session, keyring, tool, arguments
) -> None:
    """The default is receipt-bound, not the model or worker's current day."""
    op = _fresh_operation(session)
    dispatcher = FakeDispatcher(resolve=ResolveFailedSafe(reason="test_stop"))

    _run(
        session,
        op,
        interpreter=FakeInterpreter(ToolCall(tool, arguments)),
        dispatcher=dispatcher,
        keyring=keyring,
        text="午饭 45" if tool == "finance.log_expense" else "工资 100",
        now=NOW + timedelta(days=2),
    )

    assert dispatcher.resolve_calls == [
        {
            "tool": tool,
            "model_args": {**arguments, "occurred_on": "2026-07-24"},
        }
    ]


def test_date_clarification_retries_once_then_uses_receipt_day(session, keyring) -> None:
    class DateClarifyingInterpreter:
        def __init__(self) -> None:
            self.envelopes = []

        def interpret(self, *, envelope):
            self.envelopes.append(envelope)
            if len(self.envelopes) == 1:
                return Clarification("是今天还是其他日期？", reason="date")
            return ToolCall(
                "finance.log_expense",
                {
                    "name": "午饭",
                    "input_amount": "54.90",
                    "input_currency": "CNY",
                    "is_family_expense": False,
                    "entry_kind": "expense",
                    "category": "餐饮",
                },
            )

    op = _fresh_operation(session)
    interpreter = DateClarifyingInterpreter()
    dispatcher = FakeDispatcher(resolve=ResolveFailedSafe(reason="test_stop"))

    _run(
        session,
        op,
        interpreter=interpreter,
        dispatcher=dispatcher,
        keyring=keyring,
        text="午饭 54.9 个人",
    )

    assert [item.finance_date_default_retry for item in interpreter.envelopes] == [
        False,
        True,
    ]
    assert dispatcher.resolve_calls == [
        {
            "tool": "finance.log_expense",
            "model_args": {
                "name": "午饭",
                "input_amount": "54.90",
                "input_currency": "CNY",
                "is_family_expense": False,
                "entry_kind": "expense",
                "category": "餐饮",
                "occurred_on": "2026-07-24",
            },
        }
    ]


def test_date_default_retry_refuses_a_second_clarification(session, keyring) -> None:
    class RepeatingInterpreter:
        def __init__(self) -> None:
            self.calls = 0

        def interpret(self, *, envelope):
            self.calls += 1
            return Clarification("请补充信息", reason="date" if self.calls == 1 else "other")

    op = _fresh_operation(session)
    interpreter = RepeatingInterpreter()
    result = _run(
        session,
        op,
        interpreter=interpreter,
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        text="午饭 54.9 个人",
    )

    assert interpreter.calls == 2
    assert result.state == "failed_safe"
    assert result.failure_reason == ErrorCode.BOOKKEEPING_TOOL_REQUIRED.value
    assert op.safe_result is None


def test_explicit_finance_date_is_not_overwritten_or_repaired(session, keyring) -> None:
    op = _fresh_operation(session)
    arguments = {
        "name": "午饭",
        "input_amount": "45.00",
        "input_currency": "CNY",
        "occurred_on": "2026-07-23",
        "is_family_expense": False,
        "entry_kind": "expense",
        "category": "餐饮",
    }
    dispatcher = FakeDispatcher(resolve=ResolveFailedSafe(reason="test_stop"))

    _run(
        session,
        op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", arguments)),
        dispatcher=dispatcher,
        keyring=keyring,
        text="昨天午饭 45",
    )

    assert dispatcher.resolve_calls == [
        {"tool": "finance.log_expense", "model_args": arguments}
    ]


def test_tool_text_disposition_is_a_content_free_audit_record(session, keyring, caplog) -> None:
    """Provider prose beside one call never becomes arguments or a result."""
    caplog.set_level(logging.INFO, logger="personal_agent.api.orchestrator")
    op = _fresh_operation(session)
    dispatcher = FakeDispatcher(resolve=ReadCompleted("本月 ¥2093"))
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(
            ToolCall(
                "finance.query_expenses",
                {"view": "total"},
                suppressed_untrusted_text=True,
            )
        ),
        dispatcher=dispatcher,
        keyring=keyring,
        text="查一下这个月花了多少钱",
    )

    assert result.state == "succeeded"
    assert result.answer == "本月 ¥2093"
    assert dispatcher.resolve_calls == [
        {"tool": "finance.query_expenses", "model_args": {"view": "total"}}
    ]
    assert "response_disposition=tool_text_suppressed_untrusted" in caplog.text
    session.refresh(op)
    assert op.safe_result == "本月 ¥2093"


def test_internal_tool_text_disposition_is_logged_without_changing_outcome(
    session, keyring, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="personal_agent.api.orchestrator")
    op = _fresh_operation(session)
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(
            FailSafeInterpretation(
                ErrorCode.BATCH_ATOMICITY_UNAVAILABLE.value,
                suppressed_untrusted_text=True,
            )
        ),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        text="两笔合计记账",
    )

    assert result.state == "failed_safe"
    assert result.failure_reason == ErrorCode.BATCH_ATOMICITY_UNAVAILABLE.value
    assert "response_disposition=tool_text_suppressed_untrusted" in caplog.text


def test_a_resolved_write_walks_through_verification_to_success(session, keyring):
    op = _fresh_operation(session)
    intent = WriteIntent("finance.log_expense", {"name": "午饭", "amount": "45"})
    dispatcher = FakeDispatcher(resolve=Resolved(intent), commit=Written("recABC"))
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=dispatcher,
        keyring=keyring,
    )
    assert result.state == "succeeded"
    assert result.record_id == "recABC"
    session.refresh(op)
    assert op.state == "succeeded"
    assert op.safe_result == "recABC"
    # A plain write is not an override.
    assert dispatcher.commit_calls[0]["duplicate_override"] is None
    # The idempotency key handed to Finance is the client UUID.
    assert dispatcher.commit_calls[0]["idempotency_key"] == op.idempotency_key


def test_each_state_transition_records_its_actual_time(session, keyring) -> None:
    op = _fresh_operation(session)
    intent = WriteIntent("finance.log_expense", {"name": "午饭", "amount": "45"})
    moments = iter(
        [NOW + timedelta(seconds=offset) for offset in range(1, 6)]
    )

    result = run_operation(
        session,
        op,
        build_context=lambda: _envelope("午饭 45 个人支出"),
        interpreter=FakeInterpreter(
            ToolCall("finance.log_expense", {"name": "午饭"})
        ),
        dispatcher=FakeDispatcher(
            resolve=Resolved(intent), commit=Written("recABC")
        ),
        authorize=allow,
        keyring=keyring,
        now=lambda: next(moments),
    )

    assert result.state == "succeeded"
    session.refresh(op)
    assert op.state_version == 6
    assert op.updated_at == NOW + timedelta(seconds=5)


def test_source_in_progress_is_durable_before_the_external_commit(
    session, keyring
) -> None:
    op = _fresh_operation(session)
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})

    class CrashingDispatcher(FakeDispatcher):
        def commit(self, **kwargs):
            raise RuntimeError("process died after Finance accepted the call")

    with pytest.raises(RuntimeError):
        _run(
            session,
            op,
            interpreter=FakeInterpreter(
                ToolCall("finance.log_expense", {"name": "午饭"})
            ),
            dispatcher=CrashingDispatcher(resolve=Resolved(intent)),
            keyring=keyring,
        )

    # Roll back everything after the deliberate commit boundary. Recovery must
    # still see that source submission may have happened.
    session.rollback()
    session.refresh(op)
    assert op.state == "source_in_progress"


def test_a_blank_record_id_never_becomes_success(session, keyring) -> None:
    op = _fresh_operation(session)
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    result = _run(
        session,
        op,
        interpreter=FakeInterpreter(
            ToolCall("finance.log_expense", {"name": "午饭"})
        ),
        dispatcher=FakeDispatcher(resolve=Resolved(intent), commit=Written("  ")),
        keyring=keyring,
    )
    assert result.state == "needs_manual_review"
    assert result.record_id is None
    session.refresh(op)
    assert op.state == "needs_manual_review"
    assert op.safe_result is None
    assert op.failure_reason == "missing_verified_record_id"


def test_policy_denial_fails_safe_with_a_stable_reason(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "x"})),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
        authorize=deny,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "policy_denied"


def test_a_clarification_parks_the_operation(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "x"})),
        dispatcher=FakeDispatcher(resolve=NeedsClarification("个人还是家庭支出？")),
        keyring=keyring,
    )
    assert result.state == "waiting_for_clarification"
    assert result.clarification == "个人还是家庭支出？"


def test_a_resolve_failure_fails_safe(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "x"})),
        dispatcher=FakeDispatcher(resolve=ResolveFailedSafe("FX_RATE_UNAVAILABLE")),
        keyring=keyring,
    )
    assert result.state == "failed_safe"
    assert result.failure_reason == "FX_RATE_UNAVAILABLE"


def test_a_commit_unknown_stays_recoverable_instead_of_resolving(
    session, keyring
) -> None:
    """An unknown commit must not be resolved by the worker that lost sight of it.

    This used to end at terminal `needs_manual_review`, and terminal is the whole
    problem: recovery never re-reads a terminal operation, so when Finance later
    reconciled the same idempotency key to `succeeded`, the Agent kept showing
    "needs manual review" forever. The §13.2 drill on 2026-08-04 produced exactly
    that pair -- Finance verified `recvrlVQllHop0`, the Agent said `record_id=null`.

    Staying at `source_in_progress` is what hands the verdict to the one component
    that can still read Finance afterwards.
    """
    op = _fresh_operation(session)
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(resolve=Resolved(intent), commit=CommitUnknown("timeout")),
        keyring=keyring,
    )
    assert result.state == "source_in_progress"
    # The reason reaches the caller, but is not stamped onto the row: it would
    # age `updated_at` (delaying recovery) and describe a failure that has not
    # been established.
    assert result.failure_reason == "timeout"
    session.refresh(op)
    assert op.state == "source_in_progress"
    assert op.failure_reason is None
    assert is_recoverable(op.state)


def test_a_commit_unknown_is_not_terminal_so_recovery_can_still_claim_it(
    session, keyring
) -> None:
    """The property the fix exists for, stated against the state machine itself."""
    op = _fresh_operation(session)
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(resolve=Resolved(intent), commit=CommitUnknown("timeout")),
        keyring=keyring,
    )
    session.refresh(op)
    assert not is_terminal(op.state)
    # And the projection recovery will later perform is a legal walk, so the
    # self-healing path is reachable rather than merely intended.
    assert ("source_in_progress", "succeeded") in _RECOVERY_PATHS


def test_a_commit_failed_safe_fails_safe(session, keyring) -> None:
    op = _fresh_operation(session)
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(resolve=Resolved(intent), commit=CommitFailedSafe("bad")),
        keyring=keyring,
    )
    assert result.state == "failed_safe"


# --- proven zero-write outcomes learned during the single MCP call -----------


def _commit_outcome(session, keyring, outcome, key="req-1"):
    """Drive an operation into `source_in_progress`, then return that outcome."""
    op = _fresh_operation(session, key=key)
    intent = WriteIntent("finance.log_expense", {"name": "午饭", "amount": "45"})
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(resolve=Resolved(intent), commit=outcome),
        keyring=keyring,
    )
    return op, result


def test_a_duplicate_learned_during_the_call_parks_on_proven_zero_writes(
    session, keyring
) -> None:
    intent = WriteIntent("finance.log_expense", {"name": "午饭", "amount": "45"})
    op, result = _commit_outcome(
        session,
        keyring,
        CommitDuplicateZeroWrite("dup-mcp", "午饭 ¥45 餐饮"),
    )
    assert result.state == "waiting_for_duplicate_decision"
    assert result.duplicate_check_id == "dup-mcp"
    session.refresh(op)
    assert op.state == "waiting_for_duplicate_decision"
    # The intent is sealed so `write anyway` can resume without the model.
    assert op.api_request.encrypted_request_payload is not None
    assert open_intent(
        keyring,
        request_id=op.request_id,
        envelope=op.api_request.encrypted_request_payload,
    ) == intent


def test_a_resolver_question_learned_during_the_call_parks_the_operation(
    session, keyring
) -> None:
    op, result = _commit_outcome(
        session, keyring, CommitClarificationZeroWrite("东京 还是 东京01？")
    )
    assert result.state == "waiting_for_clarification"
    assert result.clarification == "东京 还是 东京01？"
    session.refresh(op)
    assert op.state == "waiting_for_clarification"


def test_parking_on_proven_zero_writes_restores_an_honest_cancel(
    session, keyring
) -> None:
    # This is the point of requiring evidence: the operation passed through
    # source_in_progress, but Finance proved nothing was written, so a user who
    # now abandons it is told the truth rather than "a write may exist".
    op, _ = _commit_outcome(
        session, keyring, CommitDuplicateZeroWrite("dup-2", "午饭 ¥45")
    )
    outcome = request_cancel(session, operation_id=op.operation_id, now=NOW)
    assert outcome.cancelled is True
    assert outcome.state == "cancelled_pre_submit"


def test_an_unknown_commit_still_cannot_park(session, keyring) -> None:
    # The contrast that makes the rule meaningful: an unknown commit has no
    # zero-write proof, so it may not take either parking edge. It now holds at
    # `source_in_progress` rather than escalating to a terminal review, but the
    # property under test is unchanged and is the one that matters -- parking
    # claims "nothing was written", and nothing here proves that.
    op, result = _commit_outcome(session, keyring, CommitUnknown("timeout"))
    assert result.state == "source_in_progress"
    session.refresh(op)
    assert op.state == "source_in_progress"
    assert op.state not in {
        "waiting_for_duplicate_decision",
        "waiting_for_clarification",
        "cancelled_pre_submit",
    }
    assert op.duplicate_check_id is None


# --- the duplicate decision flow ---------------------------------------------


def _park_a_duplicate(session, keyring, *, key="req-1"):
    op = _fresh_operation(session, key=key)
    intent = WriteIntent("finance.log_expense", {"name": "午饭", "amount": "45"})
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(
            resolve=PossibleDuplicate("dup-1", intent, "午饭 ¥45 餐饮")
        ),
        keyring=keyring,
    )
    return op, result


def test_a_duplicate_parks_the_operation_and_seals_the_intent(session, keyring):
    op, result = _park_a_duplicate(session, keyring)
    assert result.state == "waiting_for_duplicate_decision"
    assert result.duplicate_check_id == "dup-1"
    assert result.duplicate_existing == "午饭 ¥45 餐饮"
    session.refresh(op)
    assert op.state == "waiting_for_duplicate_decision"
    assert op.duplicate_check_id == "dup-1"
    # The intent is sealed for later, never stored in the clear.
    envelope = op.api_request.encrypted_request_payload
    assert envelope is not None and "ciphertext" in envelope


def test_dismiss_cancels_the_parked_operation(session, keyring) -> None:
    op, _ = _park_a_duplicate(session, keyring)
    outcome = decide_duplicate(
        session, keyring,
        duplicate_check_id="dup-1",
        decision="dismiss",
        device_id="dev-1",
        new_client_request_id="req-decision",
        now=NOW,
    )
    assert outcome.new_operation is None
    session.refresh(op)
    assert op.state == "cancelled_pre_submit"


def test_write_anyway_spawns_an_override_operation_that_writes(session, keyring):
    parked, _ = _park_a_duplicate(session, keyring)
    outcome = decide_duplicate(
        session, keyring,
        duplicate_check_id="dup-1",
        decision="write_anyway",
        device_id="dev-1",
        new_client_request_id="req-override",
        now=NOW,
    )
    new_op = outcome.new_operation
    assert new_op is not None
    assert new_op.duplicate_check_id == "dup-1"
    session.refresh(parked)
    assert parked.state == "cancelled_pre_submit"

    # Running the new operation re-issues the exact write, with the override.
    dispatcher = FakeDispatcher(resolve=None, commit=Written("recDUP"))
    result = run_operation(
        session, new_op,
        # An override never asks a model, so it assembles no context at all.
        build_context=None,
        interpreter=FakeInterpreter(DirectAnswer("unused")),
        dispatcher=dispatcher,
        authorize=allow,
        keyring=keyring,
        now=NOW,
    )
    assert result.state == "succeeded"
    assert result.record_id == "recDUP"
    # The override reached Finance as a Host value, and the write intent survived
    # the reseal.
    call = dispatcher.commit_calls[0]
    assert call["duplicate_override"] == "dup-1"
    assert call["intent"].model_args == {"name": "午饭", "amount": "45"}
    # The model interpreter was never consulted on the override path.
    assert dispatcher.resolve_calls == []


def test_write_anyway_is_idempotent_on_replay(session, keyring) -> None:
    _park_a_duplicate(session, keyring)
    first = decide_duplicate(
        session, keyring,
        duplicate_check_id="dup-1", decision="write_anyway",
        device_id="dev-1", new_client_request_id="req-override", now=NOW,
    )
    second = decide_duplicate(
        session, keyring,
        duplicate_check_id="dup-1", decision="write_anyway",
        device_id="dev-1", new_client_request_id="req-override", now=NOW,
    )
    assert second.new_operation is not None
    assert (
        second.new_operation.operation_id == first.new_operation.operation_id
    )


def test_dismiss_is_idempotent_on_replay(session, keyring) -> None:
    parked, _ = _park_a_duplicate(session, keyring)
    first = decide_duplicate(
        session, keyring,
        duplicate_check_id="dup-1", decision="dismiss",
        device_id="dev-1", new_client_request_id="req-dismiss", now=NOW,
    )
    second = decide_duplicate(
        session, keyring,
        duplicate_check_id="dup-1", decision="dismiss",
        device_id="dev-1", new_client_request_id="req-dismiss", now=NOW,
    )
    assert first == second
    session.refresh(parked)
    assert parked.state == "cancelled_pre_submit"


def test_a_decision_key_cannot_be_reused_for_another_check(session, keyring) -> None:
    _park_a_duplicate(session, keyring)
    decide_duplicate(
        session, keyring,
        duplicate_check_id="dup-1", decision="write_anyway",
        device_id="dev-1", new_client_request_id="req-override", now=NOW,
    )
    with pytest.raises(AppError) as exc:
        decide_duplicate(
            session, keyring,
            duplicate_check_id="dup-other", decision="write_anyway",
            device_id="dev-1", new_client_request_id="req-override", now=NOW,
        )
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_a_decision_on_an_unknown_check_is_rejected(session, keyring) -> None:
    with pytest.raises(AppError) as exc:
        decide_duplicate(
            session, keyring,
            duplicate_check_id="nope", decision="dismiss",
            device_id="dev-1", new_client_request_id="req-x", now=NOW,
        )
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT


def test_a_parked_duplicate_can_still_be_cancelled_cleanly(session, keyring) -> None:
    op, _ = _park_a_duplicate(session, keyring)
    outcome = request_cancel(session, operation_id=op.operation_id, now=NOW)
    assert outcome.cancelled is True
    assert outcome.state == "cancelled_pre_submit"


# --- the §13.2 drill defect, closed end to end --------------------------------
#
# 2026-08-04, production ECS: the drill killed Finance while a write sat at
# `prepared`. Finance restarted, reconciled the same idempotency key and verified
# record `recvrlVQllHop0`. The Agent operation, however, had already been resolved
# to terminal `needs_manual_review` the moment the MCP call died -- so recovery
# never looked at it again and the two state machines disagreed permanently.
#
# The unit tests above pin the orchestrator half (it no longer resolves). These
# pin the property that actually matters and that neither module can demonstrate
# alone: after an unknown commit, recovery reaches Finance's real answer.


def _unknown_commit(session, keyring, key="req-1"):
    """Drive one operation to an unknown commit and return it."""
    op = _fresh_operation(session, key=key)
    intent = WriteIntent("finance.log_expense", {"name": "午饭", "amount": "45"})
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(
            resolve=Resolved(intent), commit=CommitUnknown("source_commit_unknown")
        ),
        keyring=keyring,
    )
    session.refresh(op)
    return op, result


def _finance(state: str, *, record_id=None, verified=False):
    from personal_agent.api.recovery import FinanceExecutionStatus

    return FinanceExecutionStatus(
        state=state, record_id=record_id, receipt_verified=verified
    )


def test_an_unknown_commit_self_heals_to_finance_s_verified_success(
    session, keyring
) -> None:
    """The drill's exact pair, now ending in agreement instead of contradiction."""
    from personal_agent.api.recovery import recover_pending

    op, result = _unknown_commit(session, keyring)
    assert result.state == "source_in_progress"

    # Finance did what it did on the ECS: reconciled the same key to a verified
    # success. Recovery runs once the operation has been quiet longer than any
    # live worker's ceiling.
    resolved = dict(
        recover_pending(
            session,
            lambda key: _finance(
                "succeeded", record_id="recvrlVQllHop0", verified=True
            ),
            now=NOW + timedelta(hours=1),
        )
    )
    assert resolved[op.operation_id].target_state == "succeeded"
    session.refresh(op)
    assert op.state == "succeeded"
    assert op.safe_result == "recvrlVQllHop0"


def test_an_unknown_commit_is_not_adopted_while_the_worker_may_still_own_it(
    session, keyring
) -> None:
    """The counterpart property: self-healing must not become a new race.

    An operation that just went quiet is indistinguishable by state from one a
    live worker still holds, so recovery must leave it -- the 2026-08-03 P0 in
    the other direction. Without this, making the state recoverable would trade
    a stuck operation for a concurrent one.
    """
    from personal_agent.api.recovery import RecoveryAction, recover_pending

    op, _ = _unknown_commit(session, keyring)
    resolved = dict(
        recover_pending(
            session,
            lambda key: _finance("succeeded", record_id="recX", verified=True),
            now=NOW,  # not quiet
        )
    )
    # The scan sees it and deliberately declines it, rather than not seeing it.
    assert resolved[op.operation_id].action is RecoveryAction.LEAVE
    session.refresh(op)
    assert op.state == "source_in_progress"
    assert op.safe_result is None


def test_an_unknown_commit_with_no_finance_execution_still_escalates(
    session, keyring
) -> None:
    """Self-healing is not optimism.

    Past submit with no execution at Finance, once quiet, is the case a human
    must look at. It must still reach `needs_manual_review` -- and now it does so
    on evidence read after the fact, rather than on the worker's own blindness.
    """
    from personal_agent.api.recovery import recover_pending

    op, _ = _unknown_commit(session, keyring)
    resolved = dict(
        recover_pending(session, lambda key: None, now=NOW + timedelta(hours=1))
    )
    assert resolved[op.operation_id].target_state == "needs_manual_review"
    session.refresh(op)
    assert op.state == "needs_manual_review"


def test_an_unknown_commit_projects_a_finance_failure_as_a_safe_failure(
    session, keyring
) -> None:
    from personal_agent.api.recovery import recover_pending

    op, _ = _unknown_commit(session, keyring)
    resolved = dict(
        recover_pending(
            session, lambda key: _finance("failed_safe"), now=NOW + timedelta(hours=1)
        )
    )
    assert resolved[op.operation_id].target_state == "failed_safe"
    session.refresh(op)
    assert op.state == "failed_safe"
