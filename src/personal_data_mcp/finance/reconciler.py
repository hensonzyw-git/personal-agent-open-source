"""Recover any governed Finance write under its original Feishu client token.

Recovery is driven by durable facts: the execution's tool, its persisted first
submission time, the sealed exact create payload, and binding metadata for the
table and protected ledger config. It never accepts those facts from the model
or from a caller-selected table.
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
from personal_data_mcp.finance.family_fund import (
    FAMILY_FUND_LOCK_SECONDS,
    seal_family_fund_result,
    verify_family_fund_recovery,
)
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.schema_validator import SchemaValidation
from personal_data_mcp.finance.source_guard import require_validated_source
from personal_data_mcp.finance.write_path import (
    READ_BACK_ATTEMPTS,
    open_create_envelope,
)
from personal_data_mcp.storage.execution_store import (
    TOOL_TABLE_KINDS,
    acquire_recovery_lease,
    acquire_resource_lock,
    append_audit_event,
    mark_receipt_verified,
    record_receipt,
    release_recovery_lease,
    release_resource_lock,
    transition,
)
from personal_data_mcp.storage.models import (
    TERMINAL_EXECUTION_STATES,
    ExternalReceipt,
    ToolExecution,
)


DEADLINE_SECONDS: float = 30.0


class ReconcileError(RuntimeError):
    """Reconciliation could not reach a terminal state on this invocation."""


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
            event_type="write_reconcile_manual_review",
            summary=f"escalated: {reason}",
            now=now(),
        )
        receipt = _receipt_of(session, key)
        record_id = receipt.record_id if receipt else None
        session.commit()
    return ReconcileResult(key, "needs_manual_review", record_id)


def _deadline_reached(
    submitted_at: datetime | None,
    *,
    now: Callable[[], datetime],
    deadline_seconds: float,
) -> bool:
    """Use the persisted first submit time; retries cannot reset the deadline."""
    if submitted_at is None:
        return True
    return (now() - submitted_at).total_seconds() >= deadline_seconds


def _open_bound_envelope(
    sealed: dict[str, Any],
    *,
    keyring: KeyRing,
    key: str,
    tool: str,
    table_kind: str,
    config: LedgerConfig,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    envelope = open_create_envelope(
        sealed, keyring=keyring, idempotency_key=key
    )
    if (
        envelope.get("tool") != tool
        or envelope.get("table_kind") != table_kind
        or envelope.get("config_checksum") != config.checksum()
    ):
        raise ValueError("sealed recovery binding does not match the execution")
    context = envelope.get("recovery_context")
    if context is not None and not isinstance(context, dict):
        raise ValueError("sealed recovery context is malformed")
    return envelope["fields"], context


async def reconcile_write(
    idempotency_key: str,
    *,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    source: BaseSource,
    config: LedgerConfig,
    validation: SchemaValidation,
    keyring: KeyRing,
    owner: str,
    trace_id: str | None = None,
    now: Callable[[], datetime] = utc_now,
    deadline_seconds: float = DEADLINE_SECONDS,
) -> ReconcileResult:
    """Take one unknown Finance write to a terminal state, with zero duplicates."""
    trace_id = trace_id or f"reconcile-{idempotency_key}"

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
        table_kind = TOOL_TABLE_KINDS.get(execution.tool)
        if table_kind is None:
            raise ReconcileError(
                f"{idempotency_key} belongs to an unsupported tool"
            )

    try:
        require_validated_source(
            config=config,
            validation=validation,
            source=source,
            operation="write recovery",
        )
    except AppError as error:
        # Recovery is the one path nobody watches. A drift here does not go
        # unnoticed -- executions stop reaching a terminal state and DEV-034's
        # `execution_stuck` warning fires within the hour -- but "something is
        # stuck" sends the reader to the reconciler, while this event sends them
        # to the Feishu Base, which is where the change actually happened. So
        # this is a *diagnosis* improvement on an already-detected condition,
        # not new detection, and it must not be mistaken for the primary guard:
        # `require_validated_source` still refuses, unchanged, on all six of its
        # call sites.
        if error.code is ErrorCode.SOURCE_SCHEMA_CHANGED:
            with sessions() as session:
                _audit(
                    session,
                    trace_id=trace_id,
                    event_type="schema_drift_blocked_recovery",
                    summary=(
                        "write recovery refused: ledger schema or source no "
                        "longer matches the validated config"
                    ),
                    now=now(),
                )
                session.commit()
        raise

    resource_lock_key: str | None = None
    resource_lock_owner: str | None = None
    if table_kind == "family_fund":
        resource_lock_key = f"family_fund:{config.ledger_year}"
        resource_lock_owner = f"{owner}:recovery:{uuid.uuid4()}"
        with sessions() as session:
            acquired = acquire_resource_lock(
                session,
                lock_key=resource_lock_key,
                owner=resource_lock_owner,
                now=now(),
                seconds=FAMILY_FUND_LOCK_SECONDS,
            )
            session.commit()
        if not acquired:
            raise ReconcileError(
                f"{idempotency_key} family fund is being updated by another worker"
            )
    try:
        with sessions() as session:
            if not acquire_recovery_lease(
                session,
                idempotency_key=idempotency_key,
                owner=owner,
                now=now(),
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
                deadline_seconds=deadline_seconds,
            )
        finally:
            with sessions() as session:
                release_recovery_lease(
                    session, idempotency_key=idempotency_key, owner=owner
                )
                session.commit()
    finally:
        if resource_lock_key is not None and resource_lock_owner is not None:
            with sessions() as session:
                release_resource_lock(
                    session,
                    lock_key=resource_lock_key,
                    owner=resource_lock_owner,
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
    deadline_seconds: float,
) -> ReconcileResult:
    with sessions() as session:
        execution = session.get(ToolExecution, key)
        if execution is None:
            raise ReconcileError(f"{key} has no execution to reconcile")
        state = execution.state
        version = execution.state_version
        client_token = execution.client_token
        sealed = execution.encrypted_payload
        tool = execution.tool
        submitted_at = execution.submitted_at

    table_kind = TOOL_TABLE_KINDS.get(tool)
    if table_kind is None:
        return _to_manual_review(
            sessions,
            key=key,
            current_state=state,
            current_version=version,
            reason="execution tool has no Finance table binding",
            trace_id=trace_id,
            now=now,
        )
    table_id = source.tables[table_kind]

    if state in TERMINAL_EXECUTION_STATES:
        with sessions() as session:
            receipt = _receipt_of(session, key)
            return ReconcileResult(
                key, state, receipt.record_id if receipt else None
            )

    if sealed is None:
        return _to_manual_review(
            sessions,
            key=key,
            current_state=state,
            current_version=version,
            reason="no sealed payload to recover",
            trace_id=trace_id,
            now=now,
        )
    try:
        fields, recovery_context = _open_bound_envelope(
            sealed,
            keyring=keyring,
            key=key,
            tool=tool,
            table_kind=table_kind,
            config=config,
        )
    except (KeyError, TypeError, ValueError):
        return _to_manual_review(
            sessions,
            key=key,
            current_state=state,
            current_version=version,
            reason="sealed payload or binding is invalid",
            trace_id=trace_id,
            now=now,
        )

    record_id: str | None = None
    if state in ("commit_unknown", "reconciling_same_client_token"):
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

        try:
            record = await adapter.create_record(
                source.base_token,
                table_id,
                fields=fields,
                client_token=client_token,
            )
        except AppError:
            if _deadline_reached(
                submitted_at, now=now, deadline_seconds=deadline_seconds
            ):
                return _to_manual_review(
                    sessions,
                    key=key,
                    current_state="reconciling_same_client_token",
                    current_version=version,
                    reason="same-token replay still failing at deadline",
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
                reason="same-token replay returned no record id",
                trace_id=trace_id,
                now=now,
            )

        with sessions() as session:
            if _receipt_of(session, key) is None:
                record_receipt(
                    session,
                    receipt_id=str(uuid.uuid4()),
                    idempotency_key=key,
                    table_kind=table_kind,
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
                event_type="write_reconcile_committed_unverified",
                summary=f"same-token replay resolved a {table_kind} record id",
                now=now(),
            )
            session.commit()
    elif state != "committed_unverified":
        return _to_manual_review(
            sessions,
            key=key,
            current_state=state,
            current_version=version,
            reason=f"unsupported recovery state {state}",
            trace_id=trace_id,
            now=now,
        )

    with sessions() as session:
        execution = session.get(ToolExecution, key)
        version = execution.state_version
        receipt = _receipt_of(session, key)
        record_id = receipt.record_id if receipt else record_id
        submitted_at = execution.submitted_at

    if record_id is None:
        return _to_manual_review(
            sessions,
            key=key,
            current_state="committed_unverified",
            current_version=version,
            reason="no external receipt to verify",
            trace_id=trace_id,
            now=now,
        )

    stored: dict[str, Any] | None = None
    for _ in range(READ_BACK_ATTEMPTS):
        try:
            read = await adapter.get_record(
                source.base_token, table_id, record_id
            )
        except AppError:
            continue
        cells = read.get("fields")
        stored = cells if isinstance(cells, dict) else {}
        break

    if stored is None:
        if _deadline_reached(
            submitted_at, now=now, deadline_seconds=deadline_seconds
        ):
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

    if table_kind == "family_fund":
        if recovery_context is None:
            mismatch_names = ["recovery_context"]
        else:
            mismatch_names = verify_family_fund_recovery(
                fields,
                stored,
                config=config,
                recovery_context=recovery_context,
            )
    else:
        mismatch_names = [
            mismatch.logical_name
            for mismatch in verify_stored_against_sent(
                fields,
                stored,
                config=config,
                table_kind=table_kind,
            )
        ]
    if mismatch_names:
        return _to_manual_review(
            sessions,
            key=key,
            current_state="committed_unverified",
            current_version=version,
            reason="read-back mismatch on " + ",".join(mismatch_names),
            trace_id=trace_id,
            now=now,
        )

    encrypted_result = None
    if table_kind == "family_fund":
        assert recovery_context is not None
        encrypted_result = seal_family_fund_result(
            stored,
            config=config,
            recovery_context=recovery_context,
            keyring=keyring,
            idempotency_key=key,
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
            encrypted_result=encrypted_result,
        )
        _audit(
            session,
            trace_id=trace_id,
            event_type="write_reconcile_succeeded",
            summary=f"reconciled and verified every {table_kind} field",
            now=committed_at,
        )
        session.commit()
    return ReconcileResult(key, "succeeded", record_id)


async def reconcile_expense(
    idempotency_key: str,
    **kwargs: Any,
) -> ReconcileResult:
    """Backward-compatible name; the implementation now dispatches by tool."""
    return await reconcile_write(idempotency_key, **kwargs)
