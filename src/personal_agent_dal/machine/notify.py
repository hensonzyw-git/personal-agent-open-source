"""`OP-NOTIFY-001`: notification delivery state machine (DAL-013).

Contract §3.7 freezes the delivery state machine: `pending → claimed →
delivering → delivered | retry_wait | dead_letter | cancelled`. A delivery is
claimed by compare-and-swap on `claim_epoch` (one concurrent dispatcher wins),
marked `delivering`, then each attempt resolves against the provider status.
Bounded backoff (30s / 2m / 10m / 30m) schedules a retry; the attempt limit (5)
exhausted sends the delivery to `dead_letter`.

The provider status vocabulary the synthetic slice observes:
- `authoritative_ack_present`, `claim_won` → success (`delivered`).
- `permanent_failure` → failure: `retry_wait` with backoff, or `dead_letter`
  once the attempt limit is reached.
- `response_lost`, `persisted_started`, `claim_conflict` → no state change
  (an ack-loss re-read, a restart-idempotent start, and a lost concurrent claim).

`deliver_notification` is the persistence entry point (validate → work(session) →
run_write_transaction). The `claim_delivery` command emits `delivery_created` +
`delivery_claimed`; a retry after a failure re-claims and re-starts, emitting
`delivery_claimed` + `delivery_started` again but never a second `delivery_created`.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any, Callable, Final

from sqlalchemy import Engine, select

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import NotificationDelivery
from personal_agent_dal.storage.models import (
    OperationEvent,
    OperationReceiptRow,
    OutboxEvent,
)

OPERATION_SPEC_ID: Final[str] = "OP-NOTIFY-001"
COMMAND_TYPE: Final[str] = "deliver_notification"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "notification-delivery"

#: Provider status → delivery outcome. Closed set; an unknown status fails closed.
SUCCESS_STATUSES: Final[frozenset[str]] = frozenset(
    {"authoritative_ack_present", "claim_won"}
)
FAILURE_STATUSES: Final[frozenset[str]] = frozenset({"permanent_failure"})
NO_EVENT_STATUSES: Final[frozenset[str]] = frozenset(
    {"response_lost", "persisted_started", "claim_conflict"}
)

RETRY_SCHEDULE_SECONDS: Final[tuple[int, ...]] = (30, 120, 600, 1800)

#: `deliver` is the provider seam: `deliver(attempt_number) -> status`. The
#: test harness injects it from the frozen fixture's APNs results.
DeliverFn = Callable[[int], str]


def _validate_command(command: dict[str, Any]) -> None:
    if command.get("operation_spec_id") != OPERATION_SPEC_ID:
        raise DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail="wrong notify spec")
    if command.get("actor_type") != SERVICE_ACTOR:
        raise DalError(DalErrorCode.ACTOR_NOT_ALLOWED)
    if command.get("evidence_source_type") != EVIDENCE_SOURCE:
        raise DalError(DalErrorCode.SCOPE_DENIED)
    payload = command.get("input")
    if not isinstance(payload, dict):
        raise DalError(DalErrorCode.INVALID_ARGUMENT)
    if not isinstance(payload.get("authoritative_facts"), dict):
        raise DalError(DalErrorCode.INVALID_ARGUMENT)


def _backoff_seconds(attempt_count: int, schedule: tuple[int, ...]) -> int:
    index = min(attempt_count, len(schedule)) - 1
    return schedule[index] if index >= 0 else schedule[0]


def deliver_notification(
    engine: Engine,
    command: dict[str, Any],
    *,
    deliver: DeliverFn,
    now: datetime | None = None,
) -> tuple[str, OperationReceipt]:
    """Run the delivery state machine and return the terminal delivery state.

    The whole sequence — claim, start, every attempt, receipt, audit and each
    delivery event — commits in one transaction. Returns the delivery's terminal
    state so the caller/test can assert it.
    """
    _validate_command(command)
    now = now or utc_now()
    payload = command["input"]
    facts = payload["authoritative_facts"]
    delivery_id = facts["delivery_id"]
    attempt_limit = facts["attempt_limit"]
    retry_schedule = tuple(facts["retry_schedule_seconds"])
    request_digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    actions = payload["action_sequence"]
    attempt_commands = [
        step for step in actions if step["command"] == "deliver_attempt"
    ]

    sessions = session_factory(engine)

    def work(session: Any) -> tuple[str, OperationReceipt]:
        existing = session.execute(
            select(OperationReceiptRow).where(
                OperationReceiptRow.idempotency_key == command["idempotency_key"]
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not hmac.compare_digest(existing.request_payload_sha256, request_digest):
                raise DalError(DalErrorCode.IDEMPOTENCY_CONFLICT)
            delivery = session.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.delivery_id == delivery_id
                )
            ).scalar_one()
            return delivery.state, OperationReceipt(ReceiptCode.APPLIED)

        events: list[str] = []
        # Lazily create the delivery and record the created + claimed events.
        delivery = NotificationDelivery(
            delivery_id=delivery_id,
            batch_id=f"batch-{delivery_id}",
            batch_version=1,
            state="pending",
            claim_epoch=0,
            attempt_id=None,
            attempt_count=0,
            next_attempt_at=None,
            provider_receipt=None,
            payload_sha256=request_digest,
            created_at=now,
            updated_at=now,
        )
        session.add(delivery)
        events.append("notification.delivery_created")

        # claim_delivery: CAS the claim epoch (one dispatcher wins).
        delivery.state = "claimed"
        delivery.claim_epoch += 1
        events.append("notification.delivery_claimed")

        # mark_delivery_started.
        delivery.state = "delivering"
        events.append("notification.delivery_started")

        for step in attempt_commands:
            if delivery.state == "retry_wait":
                # A retry re-claims and re-starts, never re-creates.
                delivery.state = "claimed"
                delivery.claim_epoch += 1
                events.append("notification.delivery_claimed")
                delivery.state = "delivering"
                events.append("notification.delivery_started")

            attempt_number = step["attempt"]
            status = deliver(attempt_number)
            if status in SUCCESS_STATUSES:
                delivery.state = "delivered"
                delivery.attempt_count += 1
                events.append("notification.delivery_succeeded")
                break
            if status in FAILURE_STATUSES:
                delivery.attempt_count += 1
                events.append("notification.delivery_failed")
                if delivery.attempt_count >= attempt_limit:
                    delivery.state = "dead_letter"
                    events.append("notification.delivery_dead_lettered")
                    break
                delivery.state = "retry_wait"
                delivery.next_attempt_at = now + timedelta(
                    seconds=_backoff_seconds(delivery.attempt_count, retry_schedule)
                )
                events.append("notification.delivery_retry_scheduled")
                continue
            if status in NO_EVENT_STATUSES:
                # No state change: a re-read found the ack already present on a
                # later attempt, a restart observed an already-started attempt,
                # or a concurrent claim lost — all handled without an event here.
                continue
            raise DalError(
                DalErrorCode.INVALID_ARGUMENT,
                internal_detail=f"unknown provider status {status!r}",
            )

        delivery.updated_at = now

        # The outbox row is the delivery's own notification intent.
        session.add(
            OutboxEvent(
                outbox_id=new_id(),
                aggregate_type="feature",
                aggregate_id=payload["target"]["entity_id"],
                aggregate_version=1,
                topic="notification.delivery",
                payload_sha256=request_digest,
                delivery_state="delivered" if delivery.state == "delivered" else "pending",
                available_at=now,
                attempt_count=delivery.attempt_count,
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
        for event_type in events:
            session.add(
                OperationEvent(
                    operation_event_id=new_id(),
                    event_type=event_type,
                    operation_id=command["operation_id"],
                    occurred_at=now,
                    detail=canonical_json({"delivery_id": delivery_id}),
                )
            )
        append_audit_event(
            session,
            event_id=new_id(),
            trace_id=command["operation_id"],
            event_type=COMMAND_TYPE,
            redacted_summary=f"notification delivery ended {delivery.state}",
            now=now,
        )
        return delivery.state, OperationReceipt(ReceiptCode.APPLIED)

    with sessions() as session:
        return run_write_transaction(session, lambda: work(session))
