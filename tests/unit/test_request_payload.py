"""Exact, sealed clarification continuation state."""

from personal_agent.api.request_payload import (
    ChatRequestPayload,
    continuation_context,
    open_chat_request,
    seal_chat_request,
)
from personal_agent.context.continuation import (
    ClarificationExchange,
    FinanceRetryContext,
)
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.manifest import canonical_json


def _operation(number: int) -> str:
    return f"op_{number:032x}"


def test_repeated_clarifications_preserve_the_exact_question_answer_chain() -> None:
    first = continuation_context(
        ChatRequestPayload(
            conversation_id="tl_1",
            text="午饭 45",
            clarification_question="个人还是家庭？",
        ),
        source_operation_id=_operation(1),
    )
    second = continuation_context(
        ChatRequestPayload(
            conversation_id="tl_1",
            text="个人",
            clarification_context=first,
            clarification_question="现金还是刷卡？",
        ),
        source_operation_id=_operation(2),
    )
    third = continuation_context(
        ChatRequestPayload(
            conversation_id="tl_1",
            text="刷卡",
            clarification_context=second,
            clarification_question="发生在今天吗？",
        ),
        source_operation_id=_operation(3),
    )

    assert third.original_user_text == "午饭 45"
    assert [
        (item.question, item.answer) for item in third.completed_exchanges
    ] == [
        ("个人还是家庭？", "个人"),
        ("现金还是刷卡？", "刷卡"),
    ]
    assert third.question == "发生在今天吗？"
    assert third.source_operation_ids == (
        _operation(1),
        _operation(2),
        _operation(3),
    )


def test_sealed_payload_round_trips_the_structured_continuation() -> None:
    ring = KeyRing(
        [generate_key("request-payload", state="active")],
        service="personal-agent-api",
    )
    context = continuation_context(
        ChatRequestPayload(
            conversation_id="tl_1",
            text="午饭 45",
            clarification_question="个人还是家庭？",
        ),
        source_operation_id=_operation(1),
    )
    payload = ChatRequestPayload(
        conversation_id="tl_1",
        text="个人",
        clarification_of=_operation(1),
        clarification_context=context,
    )
    sealed = seal_chat_request(ring, request_id="req_1", payload=payload)

    assert open_chat_request(
        ring, request_id="req_1", envelope=sealed
    ) == payload


def test_legacy_sealed_clarification_remains_replayable() -> None:
    ring = KeyRing(
        [generate_key("request-payload", state="active")],
        service="personal-agent-api",
    )
    legacy = {
        "kind": "chat_request_v1",
        "conversation_id": "tl_1",
        "text": "个人",
        "clarification_of": _operation(1),
        "clarification_question": "现金还是刷卡？",
        "clarification_context": {
            "original_user_text": "午饭 45",
            "question": "个人还是家庭？",
        },
    }
    sealed = ring.encrypt(
        canonical_json(legacy).encode("utf-8"),
        table="api_requests",
        column="encrypted_request_payload",
        row_id="req_legacy",
    )

    opened = open_chat_request(
        ring, request_id="req_legacy", envelope=sealed
    )

    assert opened.clarification_context is not None
    assert opened.clarification_context.source_operation_ids == ()
    assert opened.clarification_context.completed_exchanges == ()

    continued = continuation_context(
        opened,
        source_operation_id=_operation(2),
    )
    assert continued.source_operation_ids == ()
    assert [
        (item.question, item.answer)
        for item in continued.completed_exchanges
    ] == [("个人还是家庭？", "个人")]
    assert continued.question == "现金还是刷卡？"


def test_sealed_payload_round_trips_a_finance_retry_context() -> None:
    ring = KeyRing(
        [generate_key("request-payload", state="active")],
        service="personal-agent-api",
    )
    retry = FinanceRetryContext(
        original_user_text="午饭 38",
        completed_exchanges=(
            ClarificationExchange(question="个人还是家庭？", answer="个人"),
        ),
        source_operation_id=_operation(9),
        source_failure_reason="model_unavailable",
    )
    payload = ChatRequestPayload(
        conversation_id="tl_1",
        text="重新记",
        finance_retry_context=retry,
    )

    sealed = seal_chat_request(ring, request_id="req_retry", payload=payload)

    assert open_chat_request(
        ring, request_id="req_retry", envelope=sealed
    ) == payload


def test_retry_clarification_preserves_the_original_finance_facts() -> None:
    payload = ChatRequestPayload(
        conversation_id="tl_1",
        text="重新记",
        clarification_question="个人还是家庭？",
        finance_retry_context=FinanceRetryContext(
            original_user_text="午饭 38",
            source_operation_id=_operation(9),
            source_failure_reason="BOOKKEEPING_TOOL_REQUIRED",
        ),
    )

    continued = continuation_context(payload, source_operation_id=_operation(10))

    assert continued.original_user_text == "午饭 38"
    assert continued.question == "个人还是家庭？"
    assert continued.source_operation_ids == ()
