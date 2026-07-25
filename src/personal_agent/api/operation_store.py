"""Idempotent operation bookkeeping for the Agent API, per design 5.2.1.

Every `/v1/chat/messages` call is anchored here before any model or MCP work
happens: an `api_request` and its single `operation` are created atomically, keyed
by `(device_id, client_request_id)`. That ordering is the whole point -- if the
process dies between accepting a request and doing anything with it, the record
already exists, so a retry with the same key resumes rather than duplicates.

Two idempotency rules are enforced as structure, not caller discipline:

- **Same key, same request -> same operation.** A replay returns the existing
  operation and its id; nothing new is created.
- **Same key, different request -> conflict.** A caller must not be able to
  smuggle new text into an operation that may already have committed a write, so
  a fingerprint mismatch is a hard `IDEMPOTENCY_CONFLICT`.

State moves are compare-and-swap on `(state, state_version)`, exactly like the
Finance execution store, so a resumed worker cannot overwrite newer state with
stale state. Cancellation never rewrites an accounting outcome: it sets a flag,
and only *offers* a clean `cancelled_pre_submit` when the operation has provably
not reached a source submit (design 5.2.2).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from personal_agent.api.operation_state import (
    StaleOperationVersionError,
    assert_transition,
    can_cancel_pre_submit,
    is_terminal,
)
from personal_agent.storage.models import ApiRequest, Operation
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json


def chat_request_fingerprint(
    *,
    conversation_id: str,
    text: str,
    clarification_of: str | None = None,
) -> str:
    """A canonical fingerprint of one chat request's meaning.

    Only the fields that define what the user asked are included; transport and
    diagnostic fields (`client_sent_at`, headers) are deliberately excluded, so a
    genuine retry of the same message matches and a changed message does not.
    """
    payload = {
        "conversation_id": conversation_id,
        "text": text,
        "clarification_of": clarification_of,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OpenedOperation:
    """The operation for a request, and whether this call created it."""

    operation: Operation
    created: bool


def open_operation(
    session,
    *,
    device_id: str,
    client_request_id: str,
    request_fingerprint: str,
    now: datetime,
    encrypted_request_payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> OpenedOperation:
    """Create, or return, the single operation for this client request.

    The client's UUIDv4 `client_request_id` is reused verbatim as the operation's
    `idempotency_key`, which becomes the Finance MCP key and the Feishu
    `client_token` downstream (design 5.2.1's "one side effect per message"
    reuse). A concurrent duplicate loses the unique-constraint race and is read
    back rather than raising.
    """
    existing = _existing_operation(session, device_id, client_request_id)
    if existing is not None:
        return _reuse(existing, request_fingerprint, client_request_id)

    request_id = f"req_{uuid.uuid4().hex}"
    operation_id = f"op_{uuid.uuid4().hex}"
    try:
        with session.begin_nested():
            session.add(
                ApiRequest(
                    request_id=request_id,
                    device_id=device_id,
                    client_request_id=client_request_id,
                    request_fingerprint=request_fingerprint,
                    encrypted_request_payload=encrypted_request_payload,
                    received_at=now,
                )
            )
            operation = Operation(
                operation_id=operation_id,
                request_id=request_id,
                trace_id=trace_id or new_traceparent(),
                idempotency_key=client_request_id,
                state="accepted",
                state_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
        return OpenedOperation(operation=operation, created=True)
    except IntegrityError:
        # Another worker won the (device_id, client_request_id) race. The winner's
        # row is authoritative; read it back and treat this as a replay.
        contender = _existing_operation(session, device_id, client_request_id)
        if contender is None:  # pragma: no cover - the constraint just fired
            raise
        return _reuse(contender, request_fingerprint, client_request_id)


def new_traceparent() -> str:
    """Create the W3C traceparent persisted for one operation end to end."""
    return f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"


def _existing_operation(
    session, device_id: str, client_request_id: str
) -> Operation | None:
    api_request = (
        session.query(ApiRequest)
        .filter(
            ApiRequest.device_id == device_id,
            ApiRequest.client_request_id == client_request_id,
        )
        .one_or_none()
    )
    if api_request is None:
        return None
    operation = (
        session.query(Operation)
        .filter(Operation.request_id == api_request.request_id)
        .one_or_none()
    )
    if operation is None:
        # Other idempotent API actions, such as a duplicate dismiss, may reserve
        # a client key without creating a tool operation. That key cannot later
        # be reused for chat.
        raise AppError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            internal_detail=(
                f"client_request_id {client_request_id} is already bound "
                "to a non-chat request"
            ),
        )
    return operation


def _reuse(
    operation: Operation, request_fingerprint: str, client_request_id: str
) -> OpenedOperation:
    if operation.api_request.request_fingerprint != request_fingerprint:
        raise AppError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            internal_detail=(
                f"client_request_id {client_request_id} is already bound to a "
                "different request"
            ),
        )
    return OpenedOperation(operation=operation, created=False)


def transition_operation(
    session,
    *,
    operation_id: str,
    current_state: str,
    current_version: int,
    target_state: str,
    now: datetime,
    failure_reason: str | None = None,
    tool: str | None = None,
    duplicate_check_id: str | None = None,
    safe_result: str | None = None,
    zero_write_proven: bool = False,
) -> int:
    """Move one operation forward, returning its new `state_version`.

    Validated against the safety table first, then applied as a compare-and-swap
    on `(state, state_version)`; a `rowcount` other than one means another worker
    moved it first.

    `zero_write_proven` is passed through to the safety table and defaults to
    False, so parking a possibly-submitted operation is refused unless the caller
    explicitly carries the fact source's zero-write evidence.
    """
    assert_transition(
        current_state, target_state, zero_write_proven=zero_write_proven
    )

    values: dict[str, Any] = {
        "state": target_state,
        "state_version": current_version + 1,
        "updated_at": now,
    }
    if failure_reason is not None:
        values["failure_reason"] = failure_reason
    if tool is not None:
        values["tool"] = tool
    if duplicate_check_id is not None:
        values["duplicate_check_id"] = duplicate_check_id
    if safe_result is not None:
        values["safe_result"] = safe_result

    result = session.execute(
        update(Operation)
        .where(
            Operation.operation_id == operation_id,
            Operation.state == current_state,
            Operation.state_version == current_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise StaleOperationVersionError(
            f"{operation_id} is no longer at {current_state}/v{current_version}; "
            "another worker moved it first"
        )
    return current_version + 1


@dataclass(frozen=True)
class CancelOutcome:
    """The result of a cancel request: the honest state, and whether it cancelled.

    `cancelled` is true only when the operation reached a clean
    `cancelled_pre_submit`. When the write may already be in flight, `cancelled`
    is false and `state` is unchanged -- the flag is recorded and the background
    verification continues, so the client is never told a possible write was
    rolled back.
    """

    state: str
    cancelled: bool


def request_cancel(session, *, operation_id: str, now: datetime) -> CancelOutcome:
    """Record a cancel request, cancelling cleanly only when it is safe to.

    This is the "cancel after submit does not lie" rule (design 5.2.2) in code.
    """
    operation = session.get(Operation, operation_id)
    if operation is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"no such operation {operation_id}",
        )
    # The flag is always recorded, including for an operation that raced past
    # submit or already reached a terminal outcome.
    operation.cancel_requested = True
    operation.updated_at = now
    session.flush()
    # Refresh after the flag write. If another worker advanced the state between
    # our initial read and this write, cancellation must decide from that newer
    # state rather than attempting a stale pre-submit transition.
    session.refresh(operation)

    if is_terminal(operation.state):
        # Already resolved; a cancel is a no-op that reports the real outcome.
        return CancelOutcome(state=operation.state, cancelled=False)

    if not can_cancel_pre_submit(operation.state):
        # The write may have been submitted; only Finance MCP can resolve it.
        return CancelOutcome(state=operation.state, cancelled=False)

    new_version = transition_operation(
        session,
        operation_id=operation_id,
        current_state=operation.state,
        current_version=operation.state_version,
        target_state="cancelled_pre_submit",
        now=now,
    )
    session.refresh(operation)
    assert operation.state_version == new_version
    return CancelOutcome(state="cancelled_pre_submit", cancelled=True)


def mark_detached(session, *, operation_id: str, now: datetime) -> None:
    """Record that the client stopped waiting. This never changes the outcome."""
    result = session.execute(
        update(Operation)
        .where(Operation.operation_id == operation_id)
        .values(client_detached=True, updated_at=now)
    )
    if result.rowcount != 1:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"no such operation {operation_id}",
        )


def get_operation(session, operation_id: str) -> Operation | None:
    return session.get(Operation, operation_id)
