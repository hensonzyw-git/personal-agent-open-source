"""DEV-026 D+F: the orchestrator and the duplicate decision flow.

Offline: a real Agent database, a real Agent key ring, and fakes for the model
interpreter and the Finance dispatcher. The safety properties under test are the
ones that do not depend on a real model or a live Base -- the state walk, the
`source_in_progress` boundary, and the Host-bound override.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pytest

from context_envelopes import envelope_for
from personal_agent.api.duplicate_flow import decide_duplicate
from personal_agent.api.intent import WriteIntent, open_intent
from personal_agent.api.operation_store import (
    open_operation,
    request_cancel,
    transition_operation,
)
from personal_agent.api.orchestrator import (
    CommitClarificationZeroWrite,
    CommitDuplicateZeroWrite,
    CommitFailedSafe,
    CommitUnknown,
    DirectAnswer,
    NeedsClarification,
    PossibleDuplicate,
    ReadCompleted,
    Resolved,
    ResolveFailedSafe,
    ToolCall,
    Written,
    run_operation,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Device, Operation
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode


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
         text="午饭 45 个人支出", build_context=None):
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
        now=NOW,
    )


# --- the plain paths ---------------------------------------------------------


def test_a_direct_answer_succeeds_with_no_tool(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=FakeInterpreter(DirectAnswer("你好")),
        dispatcher=FakeDispatcher(resolve=None),
        keyring=keyring,
    )
    assert result.state == "succeeded"
    assert result.answer == "你好"
    session.refresh(op)
    assert op.state == "succeeded"


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


def test_a_read_completes_without_touching_the_write_states(session, keyring) -> None:
    op = _fresh_operation(session)
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.query_expenses", {"view": "total"})),
        dispatcher=FakeDispatcher(resolve=ReadCompleted("本月 ¥2093")),
        keyring=keyring,
    )
    assert result.state == "succeeded"
    assert result.answer == "本月 ¥2093"


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


def test_a_commit_unknown_needs_manual_review(session, keyring) -> None:
    op = _fresh_operation(session)
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    result = _run(
        session, op,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(resolve=Resolved(intent), commit=CommitUnknown("timeout")),
        keyring=keyring,
    )
    assert result.state == "needs_manual_review"
    session.refresh(op)
    assert op.state == "needs_manual_review"


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
    # zero-write proof, so it escalates instead of parking.
    op, result = _commit_outcome(session, keyring, CommitUnknown("timeout"))
    assert result.state == "needs_manual_review"
    session.refresh(op)
    assert op.state == "needs_manual_review"


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
