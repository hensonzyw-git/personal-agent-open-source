"""Sealed chat request payload and controlled clarification continuation.

The raw request is persisted before model work, as required by design 5.2.1,
but never in plaintext. A clarification continuation carries only the previous
unresolved user turn and the exact question; the permanent conversation archive
is not replayed into the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from personal_agent.runtime.model_gateway import ClarificationContext
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json


_TABLE = "api_requests"
_COLUMN = "encrypted_request_payload"
_KIND = "chat_request_v1"


@dataclass(frozen=True)
class ChatRequestPayload:
    conversation_id: str
    text: str
    clarification_of: str | None = None
    clarification_context: ClarificationContext | None = None
    clarification_question: str | None = None


def seal_chat_request(
    keyring: KeyRing, *, request_id: str, payload: ChatRequestPayload
) -> dict[str, Any]:
    context = payload.clarification_context
    data: dict[str, Any] = {
        "kind": _KIND,
        "conversation_id": payload.conversation_id,
        "text": payload.text,
        "clarification_of": payload.clarification_of,
        "clarification_question": payload.clarification_question,
        "clarification_context": (
            {
                "original_user_text": context.original_user_text,
                "question": context.question,
            }
            if context is not None
            else None
        ),
    }
    return keyring.encrypt(
        canonical_json(data).encode("utf-8"),
        table=_TABLE,
        column=_COLUMN,
        row_id=request_id,
    )


def open_chat_request(
    keyring: KeyRing, *, request_id: str, envelope: dict[str, Any] | None
) -> ChatRequestPayload:
    if envelope is None:
        raise _invalid("clarification source has no sealed request")
    try:
        plaintext = keyring.decrypt(
            envelope, table=_TABLE, column=_COLUMN, row_id=request_id
        )
        data = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt/foreign payload fails closed
        raise _invalid("sealed chat request could not be opened") from exc
    if not isinstance(data, dict) or data.get("kind") != _KIND:
        raise _invalid("sealed payload is not a chat request")
    conversation_id = data.get("conversation_id")
    text = data.get("text")
    if not isinstance(conversation_id, str) or not isinstance(text, str):
        raise _invalid("sealed chat request is malformed")
    raw_context = data.get("clarification_context")
    context = None
    if raw_context is not None:
        if not isinstance(raw_context, dict):
            raise _invalid("sealed clarification context is malformed")
        original = raw_context.get("original_user_text")
        question = raw_context.get("question")
        if not isinstance(original, str) or not isinstance(question, str):
            raise _invalid("sealed clarification context is malformed")
        context = ClarificationContext(original_user_text=original, question=question)
    return ChatRequestPayload(
        conversation_id=conversation_id,
        text=text,
        clarification_of=_optional_str(data.get("clarification_of")),
        clarification_context=context,
        clarification_question=_optional_str(data.get("clarification_question")),
    )


def with_clarification_question(
    payload: ChatRequestPayload, question: str
) -> ChatRequestPayload:
    return ChatRequestPayload(
        conversation_id=payload.conversation_id,
        text=payload.text,
        clarification_of=payload.clarification_of,
        clarification_context=payload.clarification_context,
        clarification_question=question,
    )


def continuation_context(payload: ChatRequestPayload) -> ClarificationContext:
    question = payload.clarification_question
    if not isinstance(question, str) or not question.strip():
        raise _invalid("clarification source has no pending question")
    original = payload.text
    if payload.clarification_context is not None:
        original = (
            f"{payload.clarification_context.original_user_text}\n"
            f"用户补充：{payload.text}"
        )
    return ClarificationContext(original_user_text=original, question=question)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid("sealed chat request is malformed")
    return value


def _invalid(detail: str) -> AppError:
    return AppError(ErrorCode.INVALID_ARGUMENT, internal_detail=detail)
