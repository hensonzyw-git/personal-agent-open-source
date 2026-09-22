"""Sealed chat request payload and controlled clarification continuation.

The raw request is persisted before model work, as required by design 5.2.1,
but never in plaintext. A clarification continuation carries the exact
unresolved transcript -- the original message, every completed question/answer
exchange and the pending question. The permanent conversation archive is not
substituted for that non-compressible state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from personal_agent.api.chat_parts import (
    PARTS_SCHEMA_VERSION,
    Parts,
    open_chat_parts,
    same_parts,
    seal_chat_parts,
)
from personal_agent.context.continuation import (
    ClarificationContext,
    ClarificationExchange,
    FinanceRetryContext,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json


_TABLE = "api_requests"
_COLUMN = "encrypted_request_payload"
_KIND = "chat_request_v1"


@dataclass(frozen=True)
class ChatRequestPayload:
    conversation_id: str
    #: The request's effective text. For a parts request this is
    #: `parts_text(parts)`, which is `""` when the user sent images only.
    text: str
    clarification_of: str | None = None
    clarification_context: ClarificationContext | None = None
    clarification_question: str | None = None
    finance_retry_context: FinanceRetryContext | None = None
    #: A user-confirmed instruction to abandon only pre-submit work and attach
    #: this message to a fresh semantic Session.
    start_new_session: bool = False
    #: The original `parts` structure (§3.2). Empty for a pre-media request,
    #: and the only thing that distinguishes "no text part" from an empty one:
    #: both give `text == ""`, so the distinction lives here or nowhere.
    parts: Parts = ()
    dal_reply_context: dict[str, str] | None = None
    device_authority: dict[str, Any] | None = None


def seal_chat_request(
    keyring: KeyRing, *, request_id: str, payload: ChatRequestPayload
) -> dict[str, Any]:
    context = payload.clarification_context
    retry = payload.finance_retry_context
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
                "completed_exchanges": [
                    {"question": item.question, "answer": item.answer}
                    for item in context.completed_exchanges
                ],
                "source_operation_ids": list(context.source_operation_ids),
            }
            if context is not None
            else None
        ),
        "finance_retry_context": (
            {
                "original_user_text": retry.original_user_text,
                "completed_exchanges": [
                    {"question": item.question, "answer": item.answer}
                    for item in retry.completed_exchanges
                ],
                "source_operation_id": retry.source_operation_id,
                "source_failure_reason": retry.source_failure_reason,
            }
            if retry is not None
            else None
        ),
        "start_new_session": payload.start_new_session,
    }
    if payload.device_authority is not None:
        data["device_authority"] = validate_device_authority(payload.device_authority)
    if payload.dal_reply_context is not None:
        data["dal_reply_context"] = validate_reply_context(payload.dal_reply_context)
    if payload.parts:
        # §3.2 seals the schema version alongside the original structure, and
        # only for a parts request: a text-only payload keeps exactly the shape
        # it has had since before media existed, so nothing has to migrate.
        data["parts_schema_version"] = PARTS_SCHEMA_VERSION
        data["parts"] = seal_chat_parts(payload.parts)
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
        raw_exchanges = raw_context.get("completed_exchanges", [])
        raw_source_ids = raw_context.get("source_operation_ids", [])
        if not isinstance(raw_exchanges, list) or not isinstance(
            raw_source_ids, list
        ):
            raise _invalid("sealed clarification context is malformed")
        exchanges: list[ClarificationExchange] = []
        for raw_exchange in raw_exchanges:
            if not isinstance(raw_exchange, dict):
                raise _invalid("sealed clarification context is malformed")
            exchange_question = raw_exchange.get("question")
            answer = raw_exchange.get("answer")
            if not isinstance(exchange_question, str) or not isinstance(
                answer, str
            ):
                raise _invalid("sealed clarification context is malformed")
            exchanges.append(
                ClarificationExchange(
                    question=exchange_question,
                    answer=answer,
                )
            )
        if any(not isinstance(item, str) for item in raw_source_ids):
            raise _invalid("sealed clarification context is malformed")
        context = ClarificationContext(
            original_user_text=original,
            question=question,
            completed_exchanges=tuple(exchanges),
            source_operation_ids=tuple(raw_source_ids),
        )
    retry_context = _open_retry_context(data.get("finance_retry_context"))
    if context is not None and retry_context is not None:
        raise _invalid("sealed chat request has conflicting continuation contexts")
    return ChatRequestPayload(
        conversation_id=conversation_id,
        text=text,
        clarification_of=_optional_str(data.get("clarification_of")),
        clarification_context=context,
        clarification_question=_optional_str(data.get("clarification_question")),
        finance_retry_context=retry_context,
        start_new_session=_optional_bool(data.get("start_new_session"), False),
        parts=_open_parts(data),
        dal_reply_context=validate_reply_context(data.get("dal_reply_context")),
        device_authority=validate_device_authority(data.get("device_authority")),
    )


def _open_parts(data: dict[str, Any]) -> Parts:
    """The sealed parts, or none for a payload written before media existed.

    A payload that carries parts must carry the version that produced them: a
    missing version on a parts-bearing payload is a malformed record rather
    than an old one, and fails closed instead of being read under today's rules.
    """
    raw_parts = data.get("parts")
    if raw_parts is None:
        return ()
    return open_chat_parts(raw_parts, schema_version=data.get("parts_schema_version"))


def describes_request(
    payload: ChatRequestPayload,
    *,
    conversation_id: str,
    text: str,
    parts: Parts,
    clarification_of: str | None,
    start_new_session: bool,
    dal_reply_context: dict[str, str] | None = None,
) -> bool:
    """Whether a sealed payload describes the request now in hand (§3.2).

    This is the parts request's counterpart to comparing
    :func:`~personal_agent.api.operation_store.chat_request_fingerprint`, and it
    exists because the fingerprint cannot be computed on a replay: its input
    carries the measured digest, and re-reading a digest is a read of live media
    that §3.2 forbids there. Everything the fingerprint covers is covered here
    -- the same fields, and the parts compared as an ordered structure -- with
    the digest standing in for its (immutable) `media_id`.

    A payload with no parts is not a match, whatever the wire says: a request
    that arrived as `text` and is retried as `parts` (or the reverse) is a
    changed request under a reused key, which is a conflict rather than a
    replay. Text-only requests never reach here; they keep the frozen
    fingerprint comparison they have always had.
    """
    if not payload.parts:
        return False
    return (
        payload.conversation_id == conversation_id
        and payload.text == text
        and payload.clarification_of == clarification_of
        and payload.start_new_session == start_new_session
        and payload.dal_reply_context == dal_reply_context
        and same_parts(payload.parts, parts)
    )


def with_clarification_question(
    payload: ChatRequestPayload, question: str
) -> ChatRequestPayload:
    """The same request, one question further along.

    Built with `dataclasses.replace` rather than field by field, because §3.2
    names this exact function as the place media gets lost: a reconstruction
    that lists fields silently drops every field added after it was written,
    and nothing fails until a clarification answer loses its photo.
    """
    return replace(payload, clarification_question=question)


def continuation_context(
    payload: ChatRequestPayload, *, source_operation_id: str
) -> ClarificationContext:
    question = payload.clarification_question
    if not isinstance(question, str) or not question.strip():
        raise _invalid("clarification source has no pending question")
    previous = payload.clarification_context
    if previous is None:
        retry = payload.finance_retry_context
        if retry is not None:
            # The retry source may itself be a clarification answer. Preserve
            # the complete answered chain when this retry needs one more fact.
            # Empty source ids select the conservative legacy mode: no raw
            # event is hidden unless the transcript/reference chain is exact.
            return ClarificationContext(
                original_user_text=retry.original_user_text,
                question=question,
                completed_exchanges=retry.completed_exchanges,
                source_operation_ids=(),
            )
        return ClarificationContext(
            original_user_text=payload.text,
            question=question,
            source_operation_ids=(source_operation_id,),
        )
    return ClarificationContext(
        original_user_text=previous.original_user_text,
        question=question,
        completed_exchanges=(
            *previous.completed_exchanges,
            ClarificationExchange(
                question=previous.question,
                answer=payload.text,
            ),
        ),
        # A legacy v1 context has no source refs. Keep the whole upgraded chain
        # in that conservative mode: adding only the newest ref would make the
        # transcript/reference cardinality inconsistent and could hide part of
        # the old raw history.
        source_operation_ids=(
            (*previous.source_operation_ids, source_operation_id)
            if previous.source_operation_ids
            else ()
        ),
    )


def _open_retry_context(value: Any) -> FinanceRetryContext | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid("sealed finance retry context is malformed")
    original = value.get("original_user_text")
    source_id = value.get("source_operation_id")
    reason = value.get("source_failure_reason")
    raw_exchanges = value.get("completed_exchanges", [])
    if (
        not isinstance(original, str)
        or not isinstance(source_id, str)
        or not isinstance(reason, str)
        or not isinstance(raw_exchanges, list)
    ):
        raise _invalid("sealed finance retry context is malformed")
    exchanges: list[ClarificationExchange] = []
    for raw_exchange in raw_exchanges:
        if not isinstance(raw_exchange, dict):
            raise _invalid("sealed finance retry context is malformed")
        question = raw_exchange.get("question")
        answer = raw_exchange.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str):
            raise _invalid("sealed finance retry context is malformed")
        exchanges.append(ClarificationExchange(question=question, answer=answer))
    return FinanceRetryContext(
        original_user_text=original,
        completed_exchanges=tuple(exchanges),
        source_operation_id=source_id,
        source_failure_reason=reason,
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid("sealed chat request is malformed")
    return value


def _optional_bool(value: Any, default: bool) -> bool:
    """Read a backward-compatible optional boolean without coercion."""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid("sealed chat request is malformed")
    return value


def _invalid(detail: str) -> AppError:
    return AppError(ErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def validate_reply_context(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"event_id", "token"}
        or any(not isinstance(v, str) or not v for v in value.values())
        or len(value["event_id"]) > 128 or len(value["token"]) > 16384):
        raise _invalid("invalid development reply context")
    return dict(value)


def validate_device_authority(value):
    if value is None:return None
    if (not isinstance(value,dict) or set(value)!={'subject_id','key_thumbprint','scopes','client_wire_version'}
        or not isinstance(value['subject_id'],str) or not value['subject_id'].startswith('device:')
        or not isinstance(value['key_thumbprint'],str) or not value['key_thumbprint']
        or type(value['client_wire_version']) is not int or value['client_wire_version']<1
        or not isinstance(value['scopes'],list) or any(not isinstance(x,str) for x in value['scopes'])):
        raise _invalid('sealed device authority malformed')
    return value
