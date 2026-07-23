"""Durable execution bookkeeping: prepare, transition, lease, audit.

Every state change is a conditional UPDATE, never a read-then-write. The reason
is crash recovery: two workers can be alive at once after a restart, and a
read-modify-write would let the slower one overwrite the faster one's result
with stale state. Each function here reports how many rows it actually changed,
and refuses to continue when that is not one.

Leases are durable rows rather than in-process locks, because the process that
holds an in-process lock is exactly the thing that just died.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json
from personal_data_mcp.storage.models import (
    TERMINAL_EXECUTION_STATES,
    AuditEvent,
    ExternalReceipt,
    ResourceLock,
    ToolExecution,
)
from personal_data_mcp.storage.state_machine import (
    StaleStateVersionError,
    assert_transition,
    recovery_target,
)


DEFAULT_LEASE_SECONDS: Final[int] = 30

TOOL_TABLE_KINDS: Final[dict[str, str]] = {
    "finance.log_expense": "expense",
    "finance.log_income": "income",
    "finance.update_family_fund": "family_fund",
}


class UnverifiedReceiptError(RuntimeError):
    """An execution tried to report success without verified external proof."""


def prepare_execution(
    session: Session,
    *,
    idempotency_key: str,
    tool: str,
    request_fingerprint: str,
    client_token: str,
    encrypted_payload: dict[str, Any] | None,
    now: datetime,
) -> ToolExecution:
    """Create, or return, the single execution for this idempotency key.

    Called before any network activity. Replaying the same request returns the
    existing row rather than creating a second one; replaying the *key* with a
    different request is a conflict, because a caller must not be able to
    smuggle new arguments into an execution that may already have committed.
    """
    existing = session.get(ToolExecution, idempotency_key)
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                internal_detail=(
                    f"key {idempotency_key} already bound to a different request"
                ),
            )
        return existing

    execution = ToolExecution(
        idempotency_key=idempotency_key,
        tool=tool,
        request_fingerprint=request_fingerprint,
        state="prepared",
        state_version=1,
        client_token=client_token,
        encrypted_payload=encrypted_payload,
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    session.flush()
    return execution


def transition(
    session: Session,
    *,
    idempotency_key: str,
    current_state: str,
    current_version: int,
    target_state: str,
    now: datetime,
    failure_code: str | None = None,
    encrypted_result: dict[str, Any] | None = None,
) -> int:
    """Move one execution forward, returning its new `state_version`.

    The transition is validated against the safety table first, then applied
    with a compare-and-swap on `(state, state_version)`.
    """
    assert_transition(current_state, target_state)

    if target_state == "succeeded":
        execution = session.get(ToolExecution, idempotency_key)
        expected_table_kind = (
            TOOL_TABLE_KINDS.get(execution.tool) if execution is not None else None
        )
        receipts = list(
            session.scalars(
                select(ExternalReceipt).where(
                    ExternalReceipt.idempotency_key == idempotency_key
                )
            )
        )
        if (
            len(receipts) != 1
            or receipts[0].verified_at is None
            or not receipts[0].record_id.strip()
            or expected_table_kind is None
            or receipts[0].table_kind != expected_table_kind
        ):
            raise UnverifiedReceiptError(
                f"{idempotency_key} cannot succeed without exactly one "
                "matching verified external receipt"
            )

    values: dict[str, Any] = {
        "state": target_state,
        "state_version": current_version + 1,
        "updated_at": now,
    }
    if failure_code is not None:
        values["failure_code"] = failure_code
    if encrypted_result is not None:
        values["encrypted_result"] = encrypted_result
    if target_state == "submitting":
        # Recorded as the request leaves, which is what lets recovery treat an
        # interrupted submit as possibly committed.
        values["submitted_at"] = now
    if target_state in TERMINAL_EXECUTION_STATES:
        values["completed_at"] = now
        values["recovery_lease_owner"] = None
        values["recovery_lease_until"] = None

    result = session.execute(
        update(ToolExecution)
        .where(
            ToolExecution.idempotency_key == idempotency_key,
            ToolExecution.state == current_state,
            ToolExecution.state_version == current_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise StaleStateVersionError(
            f"{idempotency_key} is no longer at "
            f"{current_state}/v{current_version}; another worker moved it first"
        )
    return current_version + 1


def acquire_recovery_lease(
    session: Session,
    *,
    idempotency_key: str,
    owner: str,
    now: datetime,
    seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Claim exclusive recovery of one execution.

    Succeeds when the execution is unfinished and either unleased or holding an
    expired lease. A single conditional UPDATE keeps two workers from both
    believing they own it.
    """
    result = session.execute(
        update(ToolExecution)
        .where(
            ToolExecution.idempotency_key == idempotency_key,
            ToolExecution.state.not_in(sorted(TERMINAL_EXECUTION_STATES)),
            (ToolExecution.recovery_lease_owner.is_(None))
            | (ToolExecution.recovery_lease_until <= now),
        )
        .values(
            recovery_lease_owner=owner,
            recovery_lease_until=now + timedelta(seconds=seconds),
            updated_at=now,
        )
    )
    return result.rowcount == 1


def renew_recovery_lease(
    session: Session,
    *,
    idempotency_key: str,
    owner: str,
    now: datetime,
    seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend a lease this worker still holds. Never steals another's."""
    result = session.execute(
        update(ToolExecution)
        .where(
            ToolExecution.idempotency_key == idempotency_key,
            ToolExecution.recovery_lease_owner == owner,
            ToolExecution.recovery_lease_until > now,
        )
        .values(recovery_lease_until=now + timedelta(seconds=seconds))
    )
    return result.rowcount == 1


def release_recovery_lease(
    session: Session, *, idempotency_key: str, owner: str
) -> bool:
    result = session.execute(
        update(ToolExecution)
        .where(
            ToolExecution.idempotency_key == idempotency_key,
            ToolExecution.recovery_lease_owner == owner,
        )
        .values(recovery_lease_owner=None, recovery_lease_until=None)
    )
    return result.rowcount == 1


def scan_unfinished(session: Session) -> list[ToolExecution]:
    """Every execution that has not reached a terminal state."""
    return list(
        session.scalars(
            select(ToolExecution)
            .where(ToolExecution.state.not_in(sorted(TERMINAL_EXECUTION_STATES)))
            .order_by(ToolExecution.created_at)
        )
    )


def resume_after_restart(
    session: Session, *, owner: str, now: datetime
) -> list[tuple[str, str]]:
    """Take over unfinished executions, per technical design 7.6.2.

    Returns `(idempotency_key, state)` for each execution this worker claimed.
    An interrupted `submitting` is promoted to `commit_unknown`: the process
    that died cannot prove the request never left, so it is treated as possibly
    committed. Everything else keeps its state and continues the bounded
    recovery it was already in.
    """
    claimed: list[tuple[str, str]] = []
    for execution in scan_unfinished(session):
        if not acquire_recovery_lease(
            session, idempotency_key=execution.idempotency_key, owner=owner, now=now
        ):
            continue
        target = recovery_target(execution.state)
        if target is not None and target != execution.state:
            transition(
                session,
                idempotency_key=execution.idempotency_key,
                current_state=execution.state,
                current_version=execution.state_version,
                target_state=target,
                now=now,
            )
            claimed.append((execution.idempotency_key, target))
        else:
            claimed.append((execution.idempotency_key, execution.state))
    return claimed


def record_receipt(
    session: Session,
    *,
    receipt_id: str,
    idempotency_key: str,
    table_kind: str,
    record_id: str,
    now: datetime,
    verified: bool = False,
) -> ExternalReceipt:
    """Persist external proof before verifying it.

    Order matters: the receipt is written as soon as a record id comes back, so
    a crash before the read-back still leaves the record id to verify against
    rather than an unknown commit to reconcile.
    """
    execution = session.get(ToolExecution, idempotency_key)
    if execution is None:
        raise UnverifiedReceiptError(
            f"{idempotency_key} has no execution to receive external proof"
        )
    expected_table_kind = TOOL_TABLE_KINDS.get(execution.tool)
    if expected_table_kind is None or table_kind != expected_table_kind:
        raise UnverifiedReceiptError(
            f"{execution.tool} cannot record a {table_kind!r} receipt"
        )
    if not record_id.strip():
        raise UnverifiedReceiptError("external receipt record_id must not be empty")
    receipt = ExternalReceipt(
        receipt_id=receipt_id,
        idempotency_key=idempotency_key,
        source_system="feishu_bitable",
        table_kind=table_kind,
        record_id=record_id,
        created_at=now,
        verified_at=now if verified else None,
    )
    session.add(receipt)
    session.flush()
    return receipt


def acquire_resource_lock(
    session: Session,
    *,
    lock_key: str,
    owner: str,
    now: datetime,
    seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Serialise updates to one resource, such as a ledger year's family fund.

    The lock row is taken and committed in a short transaction; the caller then
    does its network work. Holding a SQLite write transaction across an HTTP
    round trip would block every other writer for its duration.
    """
    lease_until = now + timedelta(seconds=seconds)
    inserted = session.execute(
        insert(ResourceLock)
        .values(
            lock_key=lock_key,
            owner=owner,
            lease_until=lease_until,
            acquired_at=now,
        )
        .on_conflict_do_nothing(index_elements=[ResourceLock.lock_key])
    )
    if inserted.rowcount == 1:
        return True

    result = session.execute(
        update(ResourceLock)
        .where(ResourceLock.lock_key == lock_key, ResourceLock.lease_until <= now)
        .values(owner=owner, lease_until=lease_until, acquired_at=now)
    )
    return result.rowcount == 1


def renew_resource_lock(
    session: Session,
    *,
    lock_key: str,
    owner: str,
    now: datetime,
    seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend a live resource lease without allowing an owner change."""
    result = session.execute(
        update(ResourceLock)
        .where(
            ResourceLock.lock_key == lock_key,
            ResourceLock.owner == owner,
            ResourceLock.lease_until > now,
        )
        .values(lease_until=now + timedelta(seconds=seconds))
    )
    return result.rowcount == 1


def release_resource_lock(
    session: Session, *, lock_key: str, owner: str
) -> bool:
    """Release a lock this worker holds. Another owner's lock is left alone."""
    result = session.execute(
        delete(ResourceLock).where(
            ResourceLock.lock_key == lock_key, ResourceLock.owner == owner
        )
    )
    return result.rowcount == 1


def compute_event_hash(
    *,
    prev_hash: str | None,
    trace_id: str,
    event_type: str,
    redacted_summary: str,
    created_at: datetime,
) -> str:
    from personal_agent_core.timeutil import to_rfc3339

    return hashlib.sha256(
        canonical_json(
            {
                "prev_hash": prev_hash,
                "trace_id": trace_id,
                "event_type": event_type,
                "redacted_summary": redacted_summary,
                "created_at": to_rfc3339(created_at),
            }
        ).encode("utf-8")
    ).hexdigest()


def append_audit_event(
    session: Session,
    *,
    event_id: str,
    trace_id: str,
    event_type: str,
    redacted_summary: str,
    now: datetime,
) -> AuditEvent:
    """Append to the hash chain.

    The chain detects accidental tampering and gaps. It is not a defence against
    an attacker who already holds root and the signing keys, and the design says
    so explicitly, so it is not treated as one.
    """
    previous = session.scalars(
        select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)
    ).first()
    prev_hash = previous.event_hash if previous else None
    event = AuditEvent(
        event_id=event_id,
        trace_id=trace_id,
        event_type=event_type,
        redacted_summary=redacted_summary,
        prev_hash=prev_hash,
        event_hash=compute_event_hash(
            prev_hash=prev_hash,
            trace_id=trace_id,
            event_type=event_type,
            redacted_summary=redacted_summary,
            created_at=now,
        ),
        created_at=now,
    )
    session.add(event)
    session.flush()
    return event


def verify_audit_chain(session: Session) -> list[str]:
    """Return the ids of events whose recorded hash no longer holds."""
    broken: list[str] = []
    prev_hash: str | None = None
    events = session.scalars(
        select(AuditEvent).order_by(AuditEvent.sequence)
    ).all()
    for event in events:
        expected = compute_event_hash(
            prev_hash=prev_hash,
            trace_id=event.trace_id,
            event_type=event.event_type,
            redacted_summary=event.redacted_summary,
            created_at=event.created_at,
        )
        if event.prev_hash != prev_hash or event.event_hash != expected:
            broken.append(event.event_id)
        prev_hash = event.event_hash
    return broken
