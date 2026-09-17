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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from personal_agent.api.chat_parts import ChatPart, TextPart
from personal_agent.api.operation_state import (
    StaleOperationVersionError,
    assert_transition,
    can_cancel_pre_submit,
    is_terminal,
)
from personal_agent.storage.models import ApiRequest, Operation
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json
from personal_agent_core.tool_ir import DEVICE_EXECUTED_TOOL_NAMES


def chat_request_fingerprint(
    *,
    conversation_id: str,
    text: str,
    clarification_of: str | None = None,
    start_new_session: bool = False,
    parts: Sequence[ChatPart] = (),
    dal_reply_context: dict[str, str] | None = None,
) -> str:
    """A canonical fingerprint of one chat request's meaning.

    Only the fields that define what the user asked are included; transport and
    diagnostic fields (`client_sent_at`, headers) are deliberately excluded, so a
    genuine retry of the same message matches and a changed message does not.

    **The payload for a text-only request is frozen.** Every chat request sealed
    before media existed is compared against this exact construction on replay,
    so the `parts` key is added only when parts are present: an always-present
    empty key would change every one of those fingerprints and turn every
    existing sealed request into a spurious `IDEMPOTENCY_CONFLICT`. The literal
    values are pinned by `test_the_pre_media_fingerprint_is_frozen`.

    `text` is the request's *effective* text -- for a parts request that is
    :func:`~personal_agent.api.chat_parts.parts_text`, which is `""` when the
    user sent images and nothing else.

    The parts carry the ordered `(media_id, content_sha256)` of §3.2, and the
    digest must be the server's measured one: the client submits only media ids
    and never declares an authoritative hash (§5.1). Requiring it rather than
    defaulting it means no caller can fingerprint a request using a value the
    client supplied, because there is nothing to supply.
    """
    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "text": text,
        "clarification_of": clarification_of,
        "start_new_session": start_new_session,
    }
    if dal_reply_context is not None:
        payload["dal_reply_context"] = dal_reply_context
    if parts:
        payload["parts"] = [_fingerprint_part(part) for part in parts]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _fingerprint_part(part: ChatPart) -> dict[str, str]:
    """One part as the fingerprint sees it, the measured digest included.

    Order is meaning, not presentation: the same image before and after a text
    part are different requests, and so are two different images under one id.
    """
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    if part.content_sha256 is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=(
                "a media part must carry the server's measured sha256 before it "
                "can be fingerprinted"
            ),
        )
    return {
        "type": "image_ref",
        "media_id": part.media_id,
        "content_sha256": part.content_sha256,
    }
#: The namespace every frozen plan item's key is derived in. Fixed and
#: published for the same reason the override's is: the derivation *is* the
#: idempotency mechanism, so it cannot depend on anything that varies between
#: the first attempt and a retry.
_PLAN_NAMESPACE: Final[uuid.UUID] = uuid.UUID("0f3c2b6a-1d2e-5f47-9a8b-6c5d4e3f2a10")


def plan_item_key(plan_key: str, index: int) -> str:
    """The one key a later item of this plan may ever be created under.

    Derived from the message's own idempotency key and the item's position in
    the frozen list, never from the model's call order in some later turn --
    which is exactly the drift design 4.1 exists to prevent. The client never
    supplies it and cannot guess it: `plan_key` is the message's opaque request
    id, which no other device can read. `require_uuid4` guards the
    client-facing channel; this key never travels through it.

    The first item needs no derivation and is deliberately not given one: it is
    the message's own operation, whose `idempotency_key` is already that
    message's stable, unique request id. Deriving a second name for it would
    mean a plan whose first action the client could not match to the message it
    came from -- and, for the single-action message that is the common case, a
    new key where production already has one. So `index` here is >= 1, and the
    chain is: the message key anchors the plan, the later items hang off it.
    """
    return str(uuid.uuid5(_PLAN_NAMESPACE, f"{plan_key}:{index}"))


def plan_item_fingerprint(*, plan_key: str, index: int, tool: str, args: dict) -> str:
    """A canonical fingerprint of one frozen item's meaning.

    The plan's last line of defence (design 4.1 item 4): if a key ever collides
    with an item that described something else, `open_operation` refuses rather
    than silently re-binding the key to new arguments. Carrying the position and
    the plan as well as the arguments is what makes a collision between two
    different plans visible instead of merely improbable.
    """
    payload = {
        "plan_key": plan_key,
        "plan_index": index,
        "tool": tool,
        "args": args,
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
    parent_operation_id: str | None = None,
) -> OpenedOperation:
    """Create, or return, the single operation for this client request.

    The client's UUIDv4 `client_request_id` is reused verbatim as the operation's
    `idempotency_key`, which becomes the Finance MCP key and the Feishu
    `client_token` downstream (design 5.2.1's "one side effect per message"
    reuse). A concurrent duplicate loses the unique-constraint race and is read
    back rather than raising.
    """
    return _open_operation(
        session,
        device_id=device_id,
        client_request_id=client_request_id,
        request_fingerprint=request_fingerprint,
        now=now,
        state="accepted",
        encrypted_request_payload=encrypted_request_payload,
        trace_id=trace_id,
        parent_operation_id=parent_operation_id,
    )


def _open_operation(
    session,
    *,
    device_id: str,
    client_request_id: str,
    request_fingerprint: str,
    now: datetime,
    state: str,
    encrypted_request_payload: dict[str, Any] | None,
    trace_id: str | None,
    parent_operation_id: str | None = None,
    plan_key: str | None = None,
    plan_index: int | None = None,
) -> OpenedOperation:
    """Create, or read back, the one operation this key may ever name."""
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
                parent_operation_id=parent_operation_id,
                plan_key=plan_key,
                plan_index=plan_index,
                state=state,
                state_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
        return OpenedOperation(operation=operation, created=True)
    except IntegrityError:
        # Two different constraints can fire here and they mean opposite things.
        #
        # `(device_id, client_request_id)`: another worker for *this* device won
        # the race. Its row is authoritative; read it back and treat this as a
        # replay.
        contender = _existing_operation(session, device_id, client_request_id)
        if contender is not None:
            return _reuse(contender, request_fingerprint, client_request_id)

        # `operations.idempotency_key`: the key is globally unique, so another
        # *device* already owns it. That is a client error, not a race, and on
        # 2026-08-03 it reached production as a bare HTTP 500 with a null body --
        # the re-read above was by (device, key), found nothing, and re-raised.
        # Nothing was dispatched, so the refusal is a proven zero write.
        if _operation_for_key(session, client_request_id) is not None:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                internal_detail=(
                    f"client_request_id {client_request_id} is already bound to "
                    "another device's request"
                ),
            ) from None
        raise  # pragma: no cover - a constraint this code does not know about


def join_action_plan(
    session, *, operation_id: str, plan_key: str, now: datetime
) -> None:
    """Mark this operation as item 0 of the plan it anchors (design 4.1).

    The message's own operation is the plan's first item rather than a separate
    anchor row. It already exists by the time the list is frozen -- it is the
    request the user is talking to -- so deriving a second key for it would mean
    a plan whose first action the client cannot match to the message it came
    from, and, for the single-action message that is the common case, a new key
    where production already has one. What it gains here is *membership*, so
    `plan_operations` returns the whole turn in order including its first item.

    Written once. A second join is the same fact arriving twice, and a different
    plan key on one operation is a wiring error the row cannot express; both are
    answered by the read-back rather than by a silent rebind.
    """
    result = session.execute(
        update(Operation)
        .where(Operation.operation_id == operation_id, Operation.plan_key.is_(None))
        .values(plan_key=plan_key, plan_index=0, updated_at=now)
    )
    if result.rowcount == 1:
        return
    # A raw read, not `Session.get`: the identity map would answer with the
    # row as this session loaded it, before the UPDATE above.
    joined = session.execute(
        select(Operation.plan_key).where(Operation.operation_id == operation_id)
    ).scalar_one_or_none()
    if joined != plan_key:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail=(
                f"operation {operation_id} belongs to plan {joined!r}, "
                f"not {plan_key!r}"
            ),
        )


def open_plan_item(
    session,
    *,
    device_id: str,
    plan_key: str,
    plan_index: int,
    request_fingerprint: str,
    now: datetime,
    trace_id: str | None = None,
) -> OpenedOperation:
    """Create one already-interpreted item of a frozen action plan (design 4.1).

    This is the one operation that is born at `dispatching` rather than
    `accepted`, and deliberately so. `accepted` means "the user's request is
    recorded and nothing has been decided"; for a plan item the decision is
    already made -- the model proposed it and the Host attested it, and the
    whole point of freezing is that the answer will never be re-derived. Walking
    the row through `interpreting` would claim a model turn that this row never
    has: the turn belongs to the message it was frozen from, and that message
    owns the operation the user is talking to.

    Its attested arguments are sealed onto the row by the caller, in the same
    transaction that creates it (the seal is bound to the row's own
    `operation_id`, which does not exist until this returns). It is what resume
    reads: a crash between two items must continue from the list that was
    written down, not from a second model turn that could come back with a
    different list.

    The derived key is `plan_item_key(plan_key, index)`, so it can only ever
    name this one position of this one message, and the fingerprint is the
    last-line guard that refuses a collision instead of re-binding a key to
    different arguments.
    """
    return _open_operation(
        session,
        device_id=device_id,
        client_request_id=plan_item_key(plan_key, plan_index),
        request_fingerprint=request_fingerprint,
        now=now,
        state="dispatching",
        encrypted_request_payload=None,
        trace_id=trace_id,
        plan_key=plan_key,
        plan_index=plan_index,
    )


def plan_operations(session, plan_key: str) -> list[Operation]:
    """Every item of one frozen plan, in the order the model proposed them."""
    return (
        session.query(Operation)
        .filter(Operation.plan_key == plan_key)
        .order_by(Operation.plan_index)
        .all()
    )


def new_traceparent() -> str:
    """Create the W3C traceparent persisted for one operation end to end."""
    return f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"


def _operation_for_key(session, client_request_id: str) -> Operation | None:
    """Any operation holding this idempotency key, whichever device owns it.

    Deliberately not scoped by device: it answers "did the globally unique key
    collide", which is the question `_existing_operation` cannot answer.
    """
    return (
        session.query(Operation)
        .filter(Operation.idempotency_key == client_request_id)
        .one_or_none()
    )


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
    encrypted_result_record: dict[str, Any] | None = None,
    encrypted_device_action: dict[str, Any] | None = None,
    encrypted_request: dict[str, Any] | None = None,
    device_result: str | None = None,
    zero_write_proven: bool = False,
    run_submission=None,
) -> int:
    """Move one operation forward, returning its new `state_version`.

    Validated against the safety table first, then applied as a compare-and-swap
    on `(state, state_version)`; a `rowcount` other than one means another worker
    moved it first.

    `zero_write_proven` is passed through to the safety table and defaults to
    False, so parking a possibly-submitted operation is refused unless the caller
    explicitly carries the fact source's zero-write evidence.
    """
    # Roll back the claim too if validation/CAS fails and a caller catches it.
    with session.begin_nested():
        if target_state == "source_in_progress":
            # A v2 run cannot bypass Task/fence authority through the legacy entry.
            # Claim persistence and this operation CAS share the caller transaction.
            from personal_agent.runtime.task_control import guard_submission
            guard_submission(session, operation_id, run_submission, now)

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
        if encrypted_result_record is not None:
            values["encrypted_result_record"] = encrypted_result_record
        # The device-action seal is set on entry to `source_in_progress` (with the
        # explicit argument) and cleared on leaving it (the automatic branch):
        # delivery and refusal are both expressed by this one column, so a
        # settlement that forgot to refuse delivery cannot happen at the store
        # level. The schema CHECK is the backstop, not the mechanism. (R6.)
        if encrypted_device_action is not None:
            values["encrypted_device_action"] = encrypted_device_action
        elif current_state == "source_in_progress" and target_state != (
            "source_in_progress"
        ):
            values["encrypted_device_action"] = None
        # The retained request has no such lifecycle: it is written once, on the
        # same transition that issues the action, and kept afterwards -- settlement
        # is when the override that needs it becomes possible, not when it stops
        # being needed. Nothing clears it, so there is deliberately no `else`.
        if encrypted_request is not None:
            values["encrypted_request"] = encrypted_request
        if device_result is not None:
            values["device_result"] = device_result

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


#: How long a device-executed action may sit at `source_in_progress` without
#: the device's report before the sweep parks it. Chosen against the phone's
#: own report budget (it PATCHes as soon as EventKit answers, and the app
#: retries on next foreground) with a wide margin; not derived from any
#: connector timeout, because no connector is involved.
DEVICE_REPORT_TIMEOUT: Final[timedelta] = timedelta(minutes=15)

#: Device-executed tools, derived from the IR — never hand-listed.
_DEVICE_EXECUTED_TOOLS: Final[frozenset[str]] = DEVICE_EXECUTED_TOOL_NAMES


def sweep_timed_out_device_actions(
    session, *, now: datetime
) -> list[tuple[str, str]]:
    """Park device actions whose report never arrived at `needs_manual_review`.

    The timeout is fail-closed about what it can *know*: no report means the
    phone may or may not have written, and `needs_manual_review` is the one
    state whose meaning matches that uncertainty. `failed_safe` is exactly the
    claim "nothing was written" — a claim silence cannot support — so this
    sweep can never produce one.

    Only device-executed tools are touched: a Finance write parked at
    `source_in_progress` belongs to the Finance reconciler, which projects the
    execution store's truth and must not be raced by a wall-clock guess. The
    CAS transition makes concurrent sweeps one-shot: two workers scanning the
    same row produce exactly one move; the loser raises `Stale` and skips.
    """
    cutoff = now - DEVICE_REPORT_TIMEOUT
    parked = (
        session.query(Operation)
        .filter(
            Operation.state == "source_in_progress",
            Operation.tool.in_(sorted(_DEVICE_EXECUTED_TOOLS)),
            Operation.updated_at <= cutoff,
        )
        .all()
    )
    settled: list[tuple[str, str]] = []
    for operation in parked:
        try:
            transition_operation(
                session,
                operation_id=operation.operation_id,
                current_state=operation.state,
                current_version=operation.state_version,
                target_state="needs_manual_review",
                now=now,
                failure_reason="device report timed out; the write may exist",
            )
        except StaleOperationVersionError:
            # The device reported (or another worker swept) between the read
            # and the CAS. The winner's state is the truth; nothing to do.
            continue
        settled.append((operation.operation_id, "needs_manual_review"))
    return settled
