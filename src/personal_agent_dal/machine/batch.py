"""`OP-BATCH-001`: notification batch evaluation (DAL-013).

Contract §3.5.2 freezes the notification batch semantics. Normal-priority
decisions accumulate in a fixed 2-minute window whose `flush_at` is fixed at the
first member's entry and never extended; the window flushes early on the
`maximum_items` (5th) member; an `immediate` decision closes the current normal
batch and flushes it at once, absorbing the current normal backlog. Every member
is re-read for validity at flush time — a decision that is no longer `open` is
not counted. When every member is invalid the evaluation is `NOOP` and the batch
window is `cancelled` with zero outbox writes.

`evaluate_notification_batch` is the pure decision; `apply_notification_batch`
is the persistence entry point (validate → work(session) → run_write_transaction)
that writes the four frozen write classes: `notification_batch`,
`notification_outbox`, `operation_receipt`, `audit`, plus the
`notification.batch_flushed` operation event.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Engine, select

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import parse_rfc3339, utc_now

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import NotificationBatch
from personal_agent_dal.storage.models import (
    OperationEvent,
    OperationReceiptRow,
    OutboxEvent,
)

OPERATION_SPEC_ID: Final[str] = "OP-BATCH-001"
COMMAND_TYPE: Final[str] = "evaluate_notification_batch"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "decision-store"
MAXIMUM_ITEMS: Final[int] = 5
PRIORITIES: Final[frozenset[str]] = frozenset({"immediate", "normal"})
MEMBER_FIELDS: Final[frozenset[str]] = frozenset(
    {"decision_id", "status", "notification_priority", "created_at", "expires_at"}
)


@dataclass(frozen=True)
class BatchDecision:
    """The result of one window evaluation."""

    flush: bool
    cancelled: bool
    valid_members: tuple[dict[str, Any], ...]


def _validate_member(member: dict[str, Any]) -> None:
    if frozenset(member) != MEMBER_FIELDS:
        raise ValueError("batch member field set is not closed")
    if member["notification_priority"] not in PRIORITIES:
        raise ValueError("notification_priority outside the frozen set")


def evaluate_notification_batch(
    members: list[dict[str, Any]],
    *,
    server_now: datetime,
    flush_at: datetime,
    maximum_items: int = MAXIMUM_ITEMS,
) -> BatchDecision:
    """The §3.5.2 window: validity filter, then immediate / size / deadline flush.

    Pure and side-effect free. A member that is not `open`, or whose `expires_at`
    has passed, is invalid and excluded from the flush.
    """
    for member in members:
        _validate_member(member)

    valid: list[dict[str, Any]] = []
    for member in members:
        if member["status"] != "open":
            continue
        if member["expires_at"] is not None and server_now >= parse_rfc3339(
            member["expires_at"]
        ):
            continue
        valid.append(member)

    if not valid:
        return BatchDecision(flush=False, cancelled=True, valid_members=())

    has_immediate = any(
        member["notification_priority"] == "immediate" for member in valid
    )
    flush = (
        has_immediate
        or len(valid) >= maximum_items
        or server_now >= flush_at
    )
    return BatchDecision(flush=flush, cancelled=False, valid_members=tuple(valid))


def _validate_command(command: dict[str, Any]) -> None:
    if command.get("operation_spec_id") != OPERATION_SPEC_ID:
        raise DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail="wrong batch spec")
    if command.get("actor_type") != SERVICE_ACTOR:
        raise DalError(DalErrorCode.ACTOR_NOT_ALLOWED)
    if command.get("evidence_source_type") != EVIDENCE_SOURCE:
        raise DalError(DalErrorCode.SCOPE_DENIED)
    payload = command.get("input")
    if not isinstance(payload, dict):
        raise DalError(DalErrorCode.INVALID_ARGUMENT)
    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict):
        raise DalError(DalErrorCode.INVALID_ARGUMENT)


def _open_window(action_sequence: list[dict[str, Any]], facts: dict[str, Any]) -> datetime:
    """The flush deadline: the persisted window's deadline wins, else the command's."""
    persisted = facts.get("persisted_window")
    if persisted is not None:
        return parse_rfc3339(persisted["deadline"])
    for step in action_sequence:
        if step["command"] == "open_fixed_window":
            return parse_rfc3339(step["deadline"])
    raise DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail="no fixed window")


def apply_notification_batch(
    engine: Engine,
    command: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[BatchDecision, OperationReceipt]:
    """Evaluate the window and, on flush, atomically persist the batch + outbox."""
    _validate_command(command)
    now = now or utc_now()
    payload = command["input"]
    facts = payload["authoritative_facts"]
    request_digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    members = facts["members"]
    maximum_items = facts.get("maximum_items", MAXIMUM_ITEMS)
    flush_at = _open_window(payload["action_sequence"], facts)
    server_now = parse_rfc3339(facts["server_now"])
    decision = evaluate_notification_batch(
        members, server_now=server_now, flush_at=flush_at, maximum_items=maximum_items
    )

    sessions = session_factory(engine)

    def work(session: Any) -> tuple[BatchDecision, OperationReceipt]:
        existing = session.execute(
            select(OperationReceiptRow).where(
                OperationReceiptRow.idempotency_key == command["idempotency_key"]
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not hmac.compare_digest(existing.request_payload_sha256, request_digest):
                raise DalError(DalErrorCode.IDEMPOTENCY_CONFLICT)
            # A replay returns the original receipt. Reconstruct the decision
            # from the recorded outcome: NOOP iff the receipt code is NOOP.
            noop = existing.receipt_code == ReceiptCode.NOOP.value
            return (
                BatchDecision(flush=not noop, cancelled=noop, valid_members=()),
                OperationReceipt(
                    ReceiptCode.NOOP if noop else ReceiptCode.APPLIED
                ),
            )

        if decision.cancelled:
            # §3.5.2: every member invalid → NOOP, zero writes. Nothing is
            # persisted — not even the receipt — because there is no outcome to
            # announce. The cancelled window is a computed fact, not a row.
            return decision, OperationReceipt(ReceiptCode.NOOP)

        # Flush: persist the batch, its outbox intent, receipt, audit and event.
        session.add(
            NotificationBatch(
                batch_id=new_id(),
                state="closed",
                opened_at=now,
                flush_at=flush_at,
                maximum_items=maximum_items,
                channel="apns",
                payload_sha256=request_digest,
                created_at=now,
            )
        )
        session.add(
            OutboxEvent(
                outbox_id=new_id(),
                aggregate_type="feature",
                aggregate_id=payload["target"]["entity_id"],
                aggregate_version=1,
                topic="notification.batch_flushed",
                payload_sha256=request_digest,
                delivery_state="pending",
                available_at=now,
                attempt_count=0,
                created_at=now,
            )
        )
        session.add(
            OperationReceiptRow(
                operation_id=command["operation_id"],
                idempotency_key=command["idempotency_key"],
                operation_spec_id=OPERATION_SPEC_ID,
                command_type=COMMAND_TYPE,
                actor_type=SERVICE_ACTOR,
                evidence_source_type=EVIDENCE_SOURCE,
                receipt_code=ReceiptCode.APPLIED.value,
                receipt_schema_version=OperationReceipt(ReceiptCode.APPLIED).schema_version,
                request_payload_sha256=request_digest,
                response_payload_sha256=None,
                recorded_at=now,
            )
        )
        session.add(
            OperationEvent(
                operation_event_id=new_id(),
                event_type="notification.batch_flushed",
                operation_id=command["operation_id"],
                occurred_at=now,
                detail=canonical_json(
                    {"flushed": [m["decision_id"] for m in decision.valid_members]}
                ),
            )
        )
        append_audit_event(
            session,
            event_id=new_id(),
            trace_id=command["operation_id"],
            event_type=COMMAND_TYPE,
            redacted_summary=(
                f"notification batch flushed {len(decision.valid_members)} member(s)"
            ),
            now=now,
        )
        return decision, OperationReceipt(ReceiptCode.APPLIED)

    with sessions() as session:
        return run_write_transaction(session, lambda: work(session))
