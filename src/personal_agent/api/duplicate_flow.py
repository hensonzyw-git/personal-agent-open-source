"""The duplicate decision flow, per technical design 5.2 and 5.3.

A suspected duplicate is not a failure. When Finance MCP returns
`POSSIBLE_DUPLICATE`, the operation is parked in `waiting_for_duplicate_decision`
holding the `duplicate_check_id`, and the existing record is shown to the app.
Nothing was written, so the parked operation is still safely pre-submit.

Only the user resolves it, through `/v1/duplicate-checks/{id}/decision`:

- **dismiss** -- the parked operation ends as `cancelled_pre_submit`; no write
  ever happens.
- **write anyway** -- a *new* operation is created (a new client request and
  UUID, design 5.2.1) carrying the sealed intent and the `duplicate_check_id` as
  its override authorisation. The parked operation ends as `cancelled_pre_submit`,
  and the write happens under the new operation with the Host-bound override.

The override is never a model argument. It exists only as a value bound to an
operation the user explicitly authorised, which is what keeps the model from
writing past its own duplicate check.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from personal_agent.api.intent import (
    WriteIntent,
    open_intent,
    seal_intent,
)
from personal_agent.api.operation_store import (
    open_operation,
    transition_operation,
)
from personal_agent.storage.models import ApiRequest, Operation
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode


DISMISS = "dismiss"
WRITE_ANYWAY = "write_anyway"
_DECISIONS = frozenset({DISMISS, WRITE_ANYWAY})


@dataclass(frozen=True)
class DuplicateDecisionOutcome:
    decision: str
    #: The new, override-authorised operation, present only for `write_anyway`.
    new_operation: Operation | None


def record_possible_duplicate(
    session,
    keyring: KeyRing,
    *,
    operation: Operation,
    write_intent: WriteIntent,
    duplicate_check_id: str,
    now: datetime,
) -> None:
    """Park a dispatching operation on a duplicate, sealing its intent for later.

    The intent is sealed onto the operation's own api_request so `write anyway`
    can resume the exact write without the model. Nothing has been submitted, so
    this stays in the cancellable, pre-submit part of the machine.
    """
    operation.api_request.encrypted_request_payload = seal_intent(
        keyring, request_id=operation.request_id, intent=write_intent
    )
    session.flush()
    transition_operation(
        session,
        operation_id=operation.operation_id,
        current_state=operation.state,
        current_version=operation.state_version,
        target_state="waiting_for_duplicate_decision",
        now=now,
        duplicate_check_id=duplicate_check_id,
    )


def decide_duplicate(
    session,
    keyring: KeyRing,
    *,
    duplicate_check_id: str,
    decision: str,
    device_id: str,
    new_client_request_id: str,
    now: datetime,
) -> DuplicateDecisionOutcome:
    """Resolve a parked duplicate as the user chose. No model self-decision."""
    if decision not in _DECISIONS:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"unknown duplicate decision {decision!r}",
        )

    parked = _find_parked(session, duplicate_check_id)
    if parked is None:
        return _replay_or_reject(
            session,
            duplicate_check_id,
            decision,
            device_id,
            new_client_request_id,
        )

    _authorize(parked, device_id)

    new_operation: Operation | None = None
    if decision == WRITE_ANYWAY:
        new_operation = _spawn_override_operation(
            session,
            keyring,
            parked=parked,
            device_id=device_id,
            new_client_request_id=new_client_request_id,
            duplicate_check_id=duplicate_check_id,
            now=now,
        )
    else:
        _record_dismiss_request(
            session,
            duplicate_check_id=duplicate_check_id,
            device_id=device_id,
            new_client_request_id=new_client_request_id,
            now=now,
        )

    # Whatever the choice, the parked operation itself never wrote, so it ends as
    # a clean pre-submit cancellation.
    transition_operation(
        session,
        operation_id=parked.operation_id,
        current_state=parked.state,
        current_version=parked.state_version,
        target_state="cancelled_pre_submit",
        now=now,
    )
    return DuplicateDecisionOutcome(
        decision=decision, new_operation=new_operation
    )


def _find_parked(session, duplicate_check_id: str) -> Operation | None:
    return (
        session.query(Operation)
        .filter(
            Operation.duplicate_check_id == duplicate_check_id,
            Operation.state == "waiting_for_duplicate_decision",
        )
        .one_or_none()
    )


def _authorize(parked: Operation, device_id: str) -> None:
    if parked.api_request.device_id != device_id:
        raise AppError(
            ErrorCode.SCOPE_DENIED,
            internal_detail="a duplicate decision must come from its own device",
        )


def _spawn_override_operation(
    session,
    keyring: KeyRing,
    *,
    parked: Operation,
    device_id: str,
    new_client_request_id: str,
    duplicate_check_id: str,
    now: datetime,
) -> Operation:
    """Create the new operation that carries the override for this decision."""
    if parked.api_request.encrypted_request_payload is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="the parked operation has no sealed intent to resume",
        )
    intent = open_intent(
        keyring,
        request_id=parked.request_id,
        envelope=parked.api_request.encrypted_request_payload,
    )
    fingerprint = _decision_fingerprint(duplicate_check_id, WRITE_ANYWAY)
    opened = open_operation(
        session,
        device_id=device_id,
        client_request_id=new_client_request_id,
        request_fingerprint=fingerprint,
        now=now,
    )
    new_operation = opened.operation
    if opened.created:
        new_operation.api_request.encrypted_request_payload = seal_intent(
            keyring, request_id=new_operation.request_id, intent=intent
        )
        # The shared duplicate_check_id is the override authorisation: an active
        # operation carrying it is permitted to write past exactly this check.
        new_operation.duplicate_check_id = duplicate_check_id
        session.flush()
    return new_operation


def _replay_or_reject(
    session,
    duplicate_check_id: str,
    decision: str,
    device_id: str,
    new_client_request_id: str,
) -> DuplicateDecisionOutcome:
    """Handle a decision whose parked operation is already resolved.

    A replayed `write anyway` finds its already-created new operation; a replayed
    `dismiss` proves the same fingerprint and returns the cancelled parked
    operation through the API. A different check or choice under the same client
    key is an idempotency conflict.
    """
    request = _decision_request(session, device_id, new_client_request_id)
    if request is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="no pending duplicate decision for this check",
        )
    expected = _decision_fingerprint(duplicate_check_id, decision)
    if request.request_fingerprint != expected:
        raise AppError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            internal_detail=(
                f"client_request_id {new_client_request_id} is already bound "
                "to a different duplicate decision"
            ),
        )

    if decision == WRITE_ANYWAY:
        existing = (
            session.query(Operation)
            .filter(Operation.request_id == request.request_id)
            .one_or_none()
        )
        if (
            existing is None
            or existing.duplicate_check_id != duplicate_check_id
        ):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="duplicate override operation is missing or mismatched",
            )
        return DuplicateDecisionOutcome(
            decision=decision, new_operation=existing
        )

    resolved = (
        session.query(Operation)
        .filter(
            Operation.duplicate_check_id == duplicate_check_id,
            Operation.state == "cancelled_pre_submit",
            Operation.api_request.has(device_id=device_id),
        )
        .first()
    )
    if resolved is not None:
        return DuplicateDecisionOutcome(decision=decision, new_operation=None)
    raise AppError(
        ErrorCode.INVALID_ARGUMENT,
        internal_detail="no pending duplicate decision for this check",
    )


def _record_dismiss_request(
    session,
    *,
    duplicate_check_id: str,
    device_id: str,
    new_client_request_id: str,
    now: datetime,
) -> None:
    """Anchor a side-effect-free dismiss so its HTTP retry is idempotent."""
    fingerprint = _decision_fingerprint(duplicate_check_id, DISMISS)
    existing = _decision_request(session, device_id, new_client_request_id)
    if existing is not None:
        _assert_decision_fingerprint(
            existing, fingerprint, new_client_request_id
        )
        return

    try:
        with session.begin_nested():
            session.add(
                ApiRequest(
                    request_id=f"req_{uuid.uuid4().hex}",
                    device_id=device_id,
                    client_request_id=new_client_request_id,
                    request_fingerprint=fingerprint,
                    encrypted_request_payload=None,
                    received_at=now,
                )
            )
            session.flush()
    except IntegrityError:
        contender = _decision_request(
            session, device_id, new_client_request_id
        )
        if contender is None:  # pragma: no cover - the constraint just fired
            raise
        _assert_decision_fingerprint(
            contender, fingerprint, new_client_request_id
        )


def _decision_request(
    session, device_id: str, client_request_id: str
) -> ApiRequest | None:
    return (
        session.query(ApiRequest)
        .filter(
            ApiRequest.device_id == device_id,
            ApiRequest.client_request_id == client_request_id,
        )
        .one_or_none()
    )


def _assert_decision_fingerprint(
    request: ApiRequest, expected: str, client_request_id: str
) -> None:
    if request.request_fingerprint != expected:
        raise AppError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            internal_detail=(
                f"client_request_id {client_request_id} is already bound "
                "to a different duplicate decision"
            ),
        )


def _decision_fingerprint(duplicate_check_id: str, decision: str) -> str:
    payload = f"duplicate_decision:{duplicate_check_id}:{decision}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
