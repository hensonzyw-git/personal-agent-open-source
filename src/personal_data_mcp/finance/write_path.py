"""The single-entry write: prepare, submit, receipt, read back, succeed.

This is where design 7.6 and 9.4 become one code path. The ordering is the whole
point, so it is worth stating plainly:

1. persist `prepared` with the client token **and commit**, before any network;
2. commit `submitting` **before** the HTTP request leaves, so a crash can never
   be read as "never sent";
3. on a record id, persist the receipt and `committed_unverified` **before**
   reading back, so a crash after the create still leaves an id to verify
   instead of an unknown commit to reconcile;
4. only a field-by-field read-back match produces `succeeded`.

Each step is its own short transaction. A SQLite write transaction is never held
across an HTTP round trip -- that would block every other writer for the
duration of a call to someone else's server.

What this module deliberately does not do: it never retries a create, never
mints a second client token, and never writes a correction. A lost response is
an *unknown* commit, and resolving one is the reconciler's job (DEV-019) working
from the same token. A read-back mismatch is a terminal `needs_manual_review`
with the record id preserved -- writing again to "fix" it is how a ledger gets
two rows for one expense.
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
from personal_data_mcp.finance.duplicate_check import (
    DuplicateFinding,
    authorise_override,
    find_exact_duplicates,
    raise_check,
)
from personal_data_mcp.finance.ledger_reader import LedgerExpense
from personal_data_mcp.finance.expense_record import (
    ExpenseEntry,
    build_expense_payload,
    verify_expense_record,
)
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.schema_validator import SchemaValidation
from personal_data_mcp.storage.execution_store import (
    append_audit_event,
    mark_receipt_verified,
    prepare_execution,
    record_receipt,
    transition,
)
from personal_data_mcp.storage.models import ExternalReceipt, ToolExecution


TOOL: str = "finance.log_expense"
TABLE_KIND: str = "expense"

#: A read is safe to repeat, unlike a create. Bounded so a persistently
#: unavailable source resolves to "unverified", not to an unbounded wait.
READ_BACK_ATTEMPTS: int = 3


@dataclass(frozen=True)
class WriteOutcome:
    """Proof, not a claim: every field here came from the fact source."""

    status: str  # "created" | "idempotent_replay"
    record_id: str
    committed_at: datetime
    stored_fields: dict[str, Any]


def _audit(
    session: Session,
    *,
    trace_id: str,
    event_type: str,
    summary: str,
    now: datetime,
) -> None:
    append_audit_event(
        session,
        event_id=str(uuid.uuid4()),
        trace_id=trace_id,
        event_type=event_type,
        redacted_summary=summary,
        now=now,
    )


def _replay_of_succeeded(
    session: Session, execution: ToolExecution
) -> WriteOutcome | None:
    """The outcome of an already-succeeded execution, from its receipt."""
    if execution.state != "succeeded":
        return None
    receipt = session.query(ExternalReceipt).filter(
        ExternalReceipt.idempotency_key == execution.idempotency_key
    ).one()
    return WriteOutcome(
        status="idempotent_replay",
        record_id=receipt.record_id,
        committed_at=receipt.verified_at or receipt.created_at,
        stored_fields={},
    )


async def submit_expense(
    entry: ExpenseEntry,
    *,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    config: LedgerConfig,
    validation: SchemaValidation,
    source: BaseSource,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    ledger_rows: list[LedgerExpense],
    keyring: KeyRing,
    duplicate_override: str | None = None,
    now: Callable[[], datetime] = utc_now,
    **write_kwargs,
) -> WriteOutcome | DuplicateFinding:
    """The duplicate gate, then the write.

    The gate is outside `write_expense` for a reason that is easy to get wrong:
    it must not run when an execution for this idempotency key already exists.
    A replay would otherwise find the row it wrote itself a moment ago, call it
    a duplicate, and refuse -- turning idempotency into a failure. Design 7.6
    rule 8 states this directly: the check happens only before the first
    `prepared`, and recovery is never handled by "looks like a duplicate".
    """
    with sessions() as session:
        already_started = session.get(ToolExecution, idempotency_key) is not None

    if not already_started:
        candidates = find_exact_duplicates(entry, ledger_rows)
        if candidates:
            if duplicate_override is None:
                with sessions() as session:
                    finding = raise_check(
                        session,
                        entry=entry,
                        candidates=candidates,
                        keyring=keyring,
                        now=now(),
                    )
                    session.commit()
                # Zero writes: no execution row, nothing to reconcile, and a
                # question for Henson instead of a guess.
                return finding
            with sessions() as session:
                authorise_override(
                    session,
                    check_id=duplicate_override,
                    entry=entry,
                    current_candidates=candidates,
                    keyring=keyring,
                    now=now(),
                )
                session.commit()
        # No candidates: there is nothing to release, so an override that names
        # a now-empty check is simply unnecessary rather than an error.

    return await write_expense(
        entry,
        sessions=sessions,
        adapter=adapter,
        config=config,
        validation=validation,
        source=source,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        trace_id=trace_id,
        now=now,
        **write_kwargs,
    )


async def write_expense(
    entry: ExpenseEntry,
    *,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    config: LedgerConfig,
    validation: SchemaValidation,
    source: BaseSource,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    now: Callable[[], datetime] = utc_now,
    new_client_token: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> WriteOutcome:
    """Write one expense and return external evidence, or raise."""
    table_id = source.tables[TABLE_KIND]

    # Built before anything is persisted: a refusal for a drifted schema, an
    # unknown category or a blank name must cost nothing and leave no row.
    payload = build_expense_payload(entry, config=config, validation=validation)

    # --- step 1: prepared, committed before any network ----------------------
    with sessions() as session:
        execution = prepare_execution(
            session,
            idempotency_key=idempotency_key,
            tool=TOOL,
            request_fingerprint=request_fingerprint,
            client_token=new_client_token(),
            encrypted_payload=None,
            now=now(),
        )
        replay = _replay_of_succeeded(session, execution)
        if replay is not None:
            session.commit()
            return replay
        if execution.state != "prepared":
            # Anything past `prepared` may already have reached Feishu. Only the
            # reconciler may touch it, and only under the same client token.
            raise AppError(
                ErrorCode.SOURCE_COMMIT_UNKNOWN,
                internal_detail=(
                    f"{idempotency_key} is at {execution.state}; "
                    "recovery belongs to the reconciler"
                ),
            )
        client_token = execution.client_token
        state_version = execution.state_version
        _audit(
            session,
            trace_id=trace_id,
            event_type="expense_write_prepared",
            summary=f"prepared {TOOL} for table {TABLE_KIND}",
            now=now(),
        )
        session.commit()

    # --- step 2: submitting, committed before the request leaves -------------
    with sessions() as session:
        state_version = transition(
            session,
            idempotency_key=idempotency_key,
            current_state="prepared",
            current_version=state_version,
            target_state="submitting",
            now=now(),
        )
        session.commit()

    # --- step 3: the one create -----------------------------------------------
    try:
        record = await adapter.create_record(
            source.base_token,
            table_id,
            fields=payload,
            client_token=client_token,
        )
    except AppError as error:
        # Conservative by design: the adapter cannot yet prove which Feishu
        # failures leave no record, so every failed create is an *unknown*
        # commit rather than a safe failure. Narrowing this to `failed_safe`
        # needs evidence from the test Base, not an assumption -- and being
        # wrong in that direction invents a duplicate later.
        with sessions() as session:
            transition(
                session,
                idempotency_key=idempotency_key,
                current_state="submitting",
                current_version=state_version,
                target_state="commit_unknown",
                now=now(),
                failure_code=error.code.value,
            )
            _audit(
                session,
                trace_id=trace_id,
                event_type="expense_write_commit_unknown",
                summary=f"create failed with {error.code.value}",
                now=now(),
            )
            session.commit()
        raise AppError(
            ErrorCode.SOURCE_COMMIT_UNKNOWN,
            internal_detail=f"create failed as {error.code.value}",
        ) from error

    record_id = record.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        with sessions() as session:
            transition(
                session,
                idempotency_key=idempotency_key,
                current_state="submitting",
                current_version=state_version,
                target_state="commit_unknown",
                now=now(),
                failure_code=ErrorCode.SOURCE_COMMIT_UNKNOWN.value,
            )
            session.commit()
        raise AppError(
            ErrorCode.SOURCE_COMMIT_UNKNOWN,
            internal_detail="create returned no usable record id",
        )

    # --- step 4: receipt first, then committed_unverified ---------------------
    with sessions() as session:
        record_receipt(
            session,
            receipt_id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            table_kind=TABLE_KIND,
            record_id=record_id,
            now=now(),
        )
        state_version = transition(
            session,
            idempotency_key=idempotency_key,
            current_state="submitting",
            current_version=state_version,
            target_state="committed_unverified",
            now=now(),
        )
        _audit(
            session,
            trace_id=trace_id,
            event_type="expense_write_committed_unverified",
            summary="record id received and persisted",
            now=now(),
        )
        session.commit()

    # --- step 5: read back ----------------------------------------------------
    stored: dict[str, Any] | None = None
    last_error: AppError | None = None
    for _ in range(READ_BACK_ATTEMPTS):
        try:
            read = await adapter.get_record(source.base_token, table_id, record_id)
        except AppError as error:
            last_error = error
            continue
        fields = read.get("fields")
        stored = fields if isinstance(fields, dict) else {}
        break

    if stored is None:
        # The record exists and its id is durable; only the verification is
        # missing. It stays `committed_unverified` for the reconciler, and the
        # caller is told exactly that rather than a success or a failure.
        raise AppError(
            ErrorCode.SOURCE_COMMIT_UNKNOWN,
            internal_detail=(
                "record created but read-back unavailable after "
                f"{READ_BACK_ATTEMPTS} attempts"
                + (f" ({last_error.code.value})" if last_error else "")
            ),
        )

    mismatches = verify_expense_record(entry, stored, config=config)
    if mismatches:
        with sessions() as session:
            transition(
                session,
                idempotency_key=idempotency_key,
                current_state="committed_unverified",
                current_version=state_version,
                target_state="needs_manual_review",
                now=now(),
                failure_code=ErrorCode.SOURCE_COMMITTED_MISMATCH.value,
            )
            _audit(
                session,
                trace_id=trace_id,
                event_type="expense_write_mismatch",
                summary=(
                    "read-back mismatch on "
                    + ",".join(m.logical_name for m in mismatches)
                ),
                now=now(),
            )
            session.commit()
        raise AppError(
            ErrorCode.SOURCE_COMMITTED_MISMATCH,
            internal_detail=(
                "read-back mismatch on "
                + ",".join(m.logical_name for m in mismatches)
            ),
        )

    # --- step 6: verified, and only now succeeded -----------------------------
    committed_at = now()
    with sessions() as session:
        mark_receipt_verified(
            session, idempotency_key=idempotency_key, now=committed_at
        )
        transition(
            session,
            idempotency_key=idempotency_key,
            current_state="committed_unverified",
            current_version=state_version,
            target_state="succeeded",
            now=committed_at,
        )
        _audit(
            session,
            trace_id=trace_id,
            event_type="expense_write_succeeded",
            summary="read-back verified every written field",
            now=committed_at,
        )
        session.commit()

    return WriteOutcome(
        status="created",
        record_id=record_id,
        committed_at=committed_at,
        stored_fields=stored,
    )
