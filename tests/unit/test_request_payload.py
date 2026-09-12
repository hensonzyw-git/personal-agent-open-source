"""Exact, sealed clarification continuation state."""

import json

import pytest

from personal_agent.api.chat_parts import (
    PARTS_SCHEMA_VERSION,
    ImageRefPart,
    TextPart,
)
from personal_agent.api.request_payload import (
    ChatRequestPayload,
    continuation_context,
    open_chat_request,
    seal_chat_request,
    with_clarification_question,
)
from personal_agent.context.continuation import (
    ClarificationExchange,
    FinanceRetryContext,
)
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError
from personal_agent_core.manifest import canonical_json


def _operation(number: int) -> str:
    return f"op_{number:032x}"


def _ring() -> KeyRing:
    return KeyRing(
        [generate_key("request-payload", state="active")],
        service="personal-agent-api",
    )


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


# --- parts (§3.1, §3.2) ------------------------------------------------------


def test_sealed_parts_round_trip() -> None:
    ring = _ring()
    payload = ChatRequestPayload(
        conversation_id="tl_1",
        text="这张账单记一下",
        parts=(TextPart("这张账单记一下"), ImageRefPart("media_1")),
    )
    sealed = seal_chat_request(ring, request_id="req_parts", payload=payload)
    assert open_chat_request(ring, request_id="req_parts", envelope=sealed) == payload


def test_a_payload_sealed_before_media_existed_still_opens() -> None:
    # §3.2: "旧 chat_request_v1 可读". Written the way the pre-media code wrote
    # it -- no `parts`, no version -- to prove the reader still accepts today
    # what it wrote yesterday.
    ring = _ring()
    sealed = ring.encrypt(
        canonical_json(
            {
                "kind": "chat_request_v1",
                "conversation_id": "tl_1",
                "text": "午饭 45",
                "clarification_of": None,
                "clarification_question": None,
                "clarification_context": None,
                "finance_retry_context": None,
                "start_new_session": False,
            }
        ).encode("utf-8"),
        table="api_requests",
        column="encrypted_request_payload",
        row_id="req_legacy",
    )

    opened = open_chat_request(ring, request_id="req_legacy", envelope=sealed)
    assert opened.text == "午饭 45"
    assert opened.parts == ()


def test_a_text_only_payload_is_sealed_without_a_parts_key() -> None:
    # The frozen-shape half of the same rule: a request with no parts must not
    # gain an empty one, or every stored payload changes shape for nothing.
    ring = _ring()
    sealed = seal_chat_request(
        ring,
        request_id="req_text",
        payload=ChatRequestPayload(conversation_id="tl_1", text="午饭 45"),
    )
    data = json.loads(
        ring.decrypt(
            sealed,
            table="api_requests",
            column="encrypted_request_payload",
            row_id="req_text",
        )
    )
    assert "parts" not in data
    assert "parts_schema_version" not in data


def test_a_parts_payload_is_sealed_with_its_schema_version() -> None:
    ring = _ring()
    sealed = seal_chat_request(
        ring,
        request_id="req_ver",
        payload=ChatRequestPayload(
            conversation_id="tl_1",
            text="",
            parts=(ImageRefPart("media_1"),),
        ),
    )
    data = json.loads(
        ring.decrypt(
            sealed,
            table="api_requests",
            column="encrypted_request_payload",
            row_id="req_ver",
        )
    )
    assert data["parts_schema_version"] == PARTS_SCHEMA_VERSION
    assert data["parts"] == [{"type": "image_ref", "media_id": "media_1"}]


def test_parts_without_a_version_are_refused_rather_than_read() -> None:
    # A parts-bearing payload with no version is malformed, not old: reading it
    # under today's rules would be inventing which rules produced it.
    ring = _ring()
    sealed = ring.encrypt(
        canonical_json(
            {
                "kind": "chat_request_v1",
                "conversation_id": "tl_1",
                "text": "",
                "parts": [{"type": "image_ref", "media_id": "media_1"}],
            }
        ).encode("utf-8"),
        table="api_requests",
        column="encrypted_request_payload",
        row_id="req_bad",
    )
    with pytest.raises(AppError):
        open_chat_request(ring, request_id="req_bad", envelope=sealed)


def test_the_two_empty_text_forms_stay_distinct_through_sealing() -> None:
    # §3.1: "保持「无 text part」与「空字符串」的封存区别." Both have text == "",
    # so if the distinction survives anywhere it survives in `parts`.
    ring = _ring()
    no_text_part = ChatRequestPayload(
        conversation_id="tl_1", text="", parts=(ImageRefPart("media_1"),)
    )
    empty_text_part = ChatRequestPayload(
        conversation_id="tl_1", text="", parts=(TextPart(""),)
    )
    left = seal_chat_request(ring, request_id="req_a", payload=no_text_part)
    right = seal_chat_request(ring, request_id="req_b", payload=empty_text_part)

    assert open_chat_request(ring, request_id="req_a", envelope=left) == no_text_part
    assert (
        open_chat_request(ring, request_id="req_b", envelope=right) == empty_text_part
    )
    assert no_text_part != empty_text_part


def test_a_clarification_question_does_not_lose_the_photo() -> None:
    # §3.2 names this exact rebuild as where media gets lost, and the failure is
    # silent: the answer to a clarifying question would simply arrive without
    # the image it was about.
    payload = ChatRequestPayload(
        conversation_id="tl_1",
        text="这张账单记一下",
        parts=(TextPart("这张账单记一下"), ImageRefPart("media_1")),
        clarification_question="这是哪一天？",
    )
    carried = with_clarification_question(payload, "记到哪个分类？")

    assert carried.parts == payload.parts
    assert carried.clarification_question == "记到哪个分类？"
    assert carried.text == payload.text
    assert carried.conversation_id == payload.conversation_id
