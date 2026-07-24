"""Driving an unknown-commit execution to a terminal state, without duplicating.

When a create times out or the process dies mid-submit, the execution is at
`commit_unknown`: the request may or may not have reached Feishu. The one safe
move is to replay the create under the *same* client token and let Feishu
deduplicate. This is verified, not assumed -- an empirical check on the test Base
confirmed that a second create with the same `client_token` returns the original
`record_id` and adds no row (design 7.6 rule 7). Minting a new token here is what
would produce a duplicate, so nothing in this module ever does.

The fault matrix of design 7.6.4 collapses to three entry states this driver
handles:

- `commit_unknown` / `reconciling_same_client_token`: replay the sealed payload
  under the stored token, then verify;
- `committed_unverified`: the record id is already known, so skip the replay and
  only read it back.

It is bounded. Past a wall-clock deadline, or when the source cannot be made to
answer, the execution goes to `needs_manual_review` with its record id (if any)
preserved and an alert raised -- never to a fabricated success.

Recovery is single-writer: a durable lease is taken first, so two workers racing
after a restart cannot both drive one execution. Every transition is the same
compare-and-swap the execution store uses, so a slow worker cannot overwrite a
fast one's result.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.finance.expense_record import verify_stored_against_sent
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.write_path import (
    READ_BACK_ATTEMPTS,
    TABLE_KIND,
    open_create_payload,
)
from personal_data_mcp.storage.execution_store import (
    acquire_recovery_lease,
    append_audit_event,
    mark_receipt_verified,
    record_receipt,
    release_recovery_lease,
    transition,
)
from personal_data_mcp.storage.models import (
    TERMINAL_EXECUTION_STATES,
    ExternalReceipt,
    ToolExecution,
)


#: A reconciliation that cannot finish in this many seconds is escalated rather
#: than retried forever (design 7.6.2: "reconciler over 30 s -> manual review").
DEADLINE_SECONDS: float = 30.0


class ReconcileError(RuntimeError):
    """Reconciliation could not reach a terminal state on its own."""


@dataclass(frozen=True)
class ReconcileResult:
    idempotency_key: str
    final_state: str
    record_id: str | None


def _audit(
    session: Session, *, trace_id: str, event_type: str, summary: str, now: datetime
) -> None:
    append_audit_event(
        session,
        event_id=str(uuid.uuid4()),
        trace_id=trace_id,
        event_type=event_type,
        redacted_summary=summary,
        now=now,
    )


def _receipt_of(session: Session, key: str) -> ExternalReceipt | None:
    return (
        session.query(ExternalReceipt)
        .filter(ExternalReceipt.idempotency_key == key)
        .one_or_none()
    )


def _to_manual_review(
    sessions: sessionmaker[Session],
    *,
    key: str,
    current_state: str,
    current_version: int,
    reason: str,
    trace_id: str,
    now: Callable[[], datetime],
) -> ReconcileResult:
    """Escalate, keeping any record id, and signal that an alert is due."""
    with sessions() as session:
        transition(
            session,
            idempotency_key=key,
            current_state=current_state,
            current_version=current_version,
            target_state="needs_manual_review",
            now=now(),
            failure_code=ErrorCode.SOURCE_COMMIT_UNKNOWN.value,
        )
        _audit(
            session,
            trace_id=trace_id,
            event_type="expense_reconcile_manual_review",
            summary=f"escalated: {reason}",
            now=now(),
        )
        receipt = _receipt_of(session, key)
        record_id = receipt.record_id if receipt else None
        session.commit()
    return ReconcileResult(key, "needs_manual_review", record_id)


async def reconcile_expense(
    idempotency_key: str,
    *,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    source: BaseSource,
    config: LedgerConfig,
    keyring: KeyRing,
    owner: str,
    trace_id: str | None = None,
    now: Callable[[], datetime] = utc_now,
    deadline_seconds: float = DEADLINE_SECONDS,
) -> ReconcileResult:
    """Take one unknown-commit execution to a terminal state. Zero duplicates."""
    trace_id = trace_id or f"reconcile-{idempotency_key}"
    started = now()

    # A terminal execution needs no lease -- and could not get one, since the
    # lease is only grantable on unfinished work. Return its settled result.
    with sessions() as session:
        execution = session.get(ToolExecution, idempotency_key)
        if execution is None:
            raise ReconcileError(
                f"{idempotency_key} has no execution to reconcile"
            )
        if execution.state in TERMINAL_EXECUTION_STATES:
            receipt = _receipt_of(session, idempotency_key)
            return ReconcileResult(
                idempotency_key,
                execution.state,
                receipt.record_id if receipt else None,
            )

    with sessions() as session:
        if not acquire_recovery_lease(
            session, idempotency_key=idempotency_key, owner=owner, now=now()
        ):
            session.commit()
            raise ReconcileError(
                f"{idempotency_key} is being recovered by another worker"
            )
        session.commit()

    try:
        return await _drive(
            idempotency_key,
            sessions=sessions,
            adapter=adapter,
            source=source,
            config=config,
            keyring=keyring,
            trace_id=trace_id,
            now=now,
            started=started,
            deadline_seconds=deadline_seconds,
        )
    finally:
        with sessions() as session:
            release_recovery_lease(
                session, idempotency_key=idempotency_key, owner=owner
            )
            session.commit()


async def _drive(
    key: str,
    *,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    source: BaseSource,
    config: LedgerConfig,
    keyring: KeyRing,
    trace_id: str,
    now: Callable[[], datetime],
    started: datetime,
    deadline_seconds: float,
) -> ReconcileResult:
    table_id = source.tables[TABLE_KIND]

    with sessions() as session:
        execution = session.get(ToolExecution, key)
        if execution is None:
            raise ReconcileError(f"{key} has no execution to reconcile")
        state = execution.state
        version = execution.state_version
        client_token = execution.client_token
        sealed = execution.encrypted_payload

    if state in ("succeeded", "failed_safe", "cancelled_pre_submit"):
        receipt_id = None
        with sessions() as session:
            receipt = _receipt_of(session, key)
            receipt_id = receipt.record_id if receipt else None
        return ReconcileResult(key, state, receipt_id)
    if state == "needs_manual_review":
        with sessions() as session:
            receipt = _receipt_of(session, key)
            return ReconcileResult(
                key, state, receipt.record_id if receipt else None
            )

    # The record id may already be known (`committed_unverified`), in which case
    # the replay is skipped entirely and only verification remains.
    record_id: str | None = None
    if state in ("commit_unknown", "reconciling_same_client_token"):
        if sealed is None:
            return _to_manual_review(
                sessions,
                key=key,
                current_state=state,
                current_version=version,
                reason="no sealed payload to replay",
                trace_id=trace_id,
                now=now,
            )
        if state == "commit_unknown":
            with sessions() as session:
                version = transition(
                    session,
                    idempotency_key=key,
                    current_state="commit_unknown",
                    current_version=version,
                    target_state="reconciling_same_client_token",
                    now=now(),
                )
                session.commit()

        fields = open_create_payload(sealed, keyring=keyring, idempotency_key=key)
        try:
            # Same client token: Feishu returns the original record if the first
            # attempt did land, and creates it exactly once if it did not.
            record = await adapter.create_record(
                source.base_token, table_id, fields=fields, client_token=client_token
            )
        except AppError:
            if (now() - started).total_seconds() >= deadline_seconds:
                return _to_manual_review(
                    sessions,
                    key=key,
                    current_state="reconciling_same_client_token",
                    current_version=version,
                    reason="replay still failing at deadline",
                    trace_id=trace_id,
                    now=now,
                )
            raise ReconcileError(f"{key} replay failed; retry before the deadline")

        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            return _to_manual_review(
                sessions,
                key=key,
                current_state="reconciling_same_client_token",
                current_version=version,
                reason="replay returned no record id",
                trace_id=trace_id,
                now=now,
            )

        with sessions() as session:
            if _receipt_of(session, key) is None:
                record_receipt(
                    session,
                    receipt_id=str(uuid.uuid4()),
                    idempotency_key=key,
                    table_kind=TABLE_KIND,
                    record_id=record_id,
                    now=now(),
                )
            version = transition(
                session,
                idempotency_key=key,
                current_state="reconciling_same_client_token",
                current_version=version,
                target_state="committed_unverified",
                now=now(),
            )
            _audit(
                session,
                trace_id=trace_id,
                event_type="expense_reconcile_committed_unverified",
                summary="same-token replay resolved the record id",
                now=now(),
            )
            session.commit()

    # From here the state is `committed_unverified`: verify by read-back.
    with sessions() as session:
        execution = session.get(ToolExecution, key)
        version = execution.state_version
        sealed = execution.encrypted_payload
        receipt = _receipt_of(session, key)
        record_id = receipt.record_id if receipt else record_id

    if record_id is None or sealed is None:
        return _to_manual_review(
            sessions,
            key=key,
            current_state="committed_unverified",
            current_version=version,
            reason="nothing to verify against",
            trace_id=trace_id,
            now=now,
        )

    fields = open_create_payload(sealed, keyring=keyring, idempotency_key=key)
    stored: dict[str, Any] | None = None
    for _ in range(READ_BACK_ATTEMPTS):
        try:
            read = await adapter.get_record(source.base_token, table_id, record_id)
        except AppError:
            continue
        cells = read.get("fields")
        stored = cells if isinstance(cells, dict) else {}
        break

    if stored is None:
        if (now() - started).total_seconds() >= deadline_seconds:
            return _to_manual_review(
                sessions,
                key=key,
                current_state="committed_unverified",
                current_version=version,
                reason="read-back unavailable at deadline",
                trace_id=trace_id,
                now=now,
            )
        raise ReconcileError(f"{key} read-back unavailable; retry before deadline")

    mismatches = verify_stored_against_sent(fields, stored, config=config)
    if mismatches:
        return _to_manual_review(
            sessions,
            key=key,
            current_state="committed_unverified",
            current_version=version,
            reason="read-back mismatch on " + ",".join(m.logical_name for m in mismatches),
            trace_id=trace_id,
            now=now,
        )

    committed_at = now()
    with sessions() as session:
        mark_receipt_verified(session, idempotency_key=key, now=committed_at)
        transition(
            session,
            idempotency_key=key,
            current_state="committed_unverified",
            current_version=version,
            target_state="succeeded",
            now=committed_at,
        )
        _audit(
            session,
            trace_id=trace_id,
            event_type="expense_reconcile_succeeded",
            summary="reconciled and verified every field",
            now=committed_at,
        )
        session.commit()
    return ReconcileResult(key, "succeeded", record_id)
