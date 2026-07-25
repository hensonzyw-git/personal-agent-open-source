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

import json
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
from personal_data_mcp.finance.source_guard import require_validated_source
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

#: AAD binding for the sealed create payload. The row id is the idempotency key,
#: so a sealed payload cannot be lifted onto a different execution.
_PAYLOAD_TABLE: str = "tool_executions"
_PAYLOAD_COLUMN: str = "encrypted_payload"


def seal_create_payload(
    fields: dict[str, Any],
    *,
    keyring: KeyRing,
    idempotency_key: str,
    tool: str | None = None,
    table_kind: str | None = None,
    config_checksum: str | None = None,
    recovery_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seal the exact `fields` a create will send, for cold-restart recovery.

    A reconciler that wakes after a crash has only the database, so the payload
    it must replay under the same client token has to be persisted here -- and
    it names the amount and the item text, so it is sealed, not stored in the
    clear. The client token itself is a separate plaintext column, because it is
    a unique key the database must be able to enforce.
    """
    return keyring.encrypt(
        json.dumps(
            {
                "fields": fields,
                "tool": tool,
                "table_kind": table_kind,
                "config_checksum": config_checksum,
                "recovery_context": recovery_context,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        table=_PAYLOAD_TABLE,
        column=_PAYLOAD_COLUMN,
        row_id=idempotency_key,
    )


def open_create_payload(
    envelope: dict[str, Any], *, keyring: KeyRing, idempotency_key: str
) -> dict[str, Any]:
    """Recover the sealed `fields`. Raises if the AAD binding does not hold."""
    raw = keyring.decrypt(
        envelope,
        table=_PAYLOAD_TABLE,
        column=_PAYLOAD_COLUMN,
        row_id=idempotency_key,
    )
    return json.loads(raw.decode("utf-8"))["fields"]


def open_create_envelope(
    envelope: dict[str, Any], *, keyring: KeyRing, idempotency_key: str
) -> dict[str, Any]:
    """Open the complete recovery envelope, including its binding metadata."""
    raw = keyring.decrypt(
        envelope,
        table=_PAYLOAD_TABLE,
        column=_PAYLOAD_COLUMN,
        row_id=idempotency_key,
    )
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict) or not isinstance(decoded.get("fields"), dict):
        raise ValueError("sealed create envelope is malformed")
    return decoded


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


async def execute_governed_write(
    *,
    tool: str,
    table_kind: str,
    payload: dict[str, Any],
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    base_token: str,
    table_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    verify: Callable[[dict[str, Any]], list[str]],
    config_checksum: str,
    recovery_context: dict[str, Any] | None = None,
    result_envelope_factory: (
        Callable[[dict[str, Any]], dict[str, Any]] | None
    ) = None,
    on_prepared: Callable[[Session], None] | None = None,
    keyring: KeyRing | None = None,
    now: Callable[[], datetime] = utc_now,
    new_client_token: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> WriteOutcome:
    """The crash-safe create-and-verify skeleton, shared by every write tool.

    The ordering is the safety property and it lives here, in one place, so the
    expense, income and family-fund tools cannot drift from it: `prepared` (with
    the client token) commits before any network; `submitting` commits before
    the request leaves; the receipt and `committed_unverified` commit before the
    read-back. `verify` returns the logical names of any fields whose read-back
    disagrees with what was sent -- an empty list is the only thing that reaches
    `succeeded`. Nothing here retries a create or writes a correction.
    """
    sealed_payload = (
        seal_create_payload(
            payload,
            keyring=keyring,
            idempotency_key=idempotency_key,
            tool=tool,
            table_kind=table_kind,
            config_checksum=config_checksum,
            recovery_context=recovery_context,
        )
        if keyring is not None
        else None
    )

    # --- step 1: prepared, committed before any network ----------------------
    with sessions() as session:
        execution = prepare_execution(
            session,
            idempotency_key=idempotency_key,
            tool=tool,
            request_fingerprint=request_fingerprint,
            client_token=new_client_token(),
            encrypted_payload=sealed_payload,
            now=now(),
        )
        replay = _replay_of_succeeded(session, execution)
        if replay is not None:
            session.commit()
            return replay
        if execution.state != "prepared":
            raise AppError(
                ErrorCode.SOURCE_COMMIT_UNKNOWN,
                internal_detail=(
                    f"{idempotency_key} is at {execution.state}; "
                    "recovery belongs to the reconciler"
                ),
            )
        client_token = execution.client_token
        state_version = execution.state_version
        if on_prepared is not None:
            # Evidence that justifies the amount (today: the FX quote) is
            # written in the same transaction as `prepared`, before any network
            # call. Recording it afterwards would mean a completed write whose
            # justification was lost, or -- worse -- a successful write reported
            # as a failure because a purely evidential insert raised.
            on_prepared(session)
        _audit(
            session,
            trace_id=trace_id,
            event_type="write_prepared",
            summary=f"prepared {tool} for table {table_kind}",
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

    # --- step 3: the one create ----------------------------------------------
    try:
        record = await adapter.create_record(
            base_token, table_id, fields=payload, client_token=client_token
        )
    except AppError as error:
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
                event_type="write_commit_unknown",
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

    # --- step 4: receipt first, then committed_unverified --------------------
    with sessions() as session:
        record_receipt(
            session,
            receipt_id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            table_kind=table_kind,
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
            event_type="write_committed_unverified",
            summary="record id received and persisted",
            now=now(),
        )
        session.commit()

    # --- step 5: read back ---------------------------------------------------
    stored: dict[str, Any] | None = None
    last_error: AppError | None = None
    for _ in range(READ_BACK_ATTEMPTS):
        try:
            read = await adapter.get_record(base_token, table_id, record_id)
        except AppError as error:
            last_error = error
            continue
        fields = read.get("fields")
        stored = fields if isinstance(fields, dict) else {}
        break

    if stored is None:
        raise AppError(
            ErrorCode.SOURCE_COMMIT_UNKNOWN,
            internal_detail=(
                "record created but read-back unavailable after "
                f"{READ_BACK_ATTEMPTS} attempts"
                + (f" ({last_error.code.value})" if last_error else "")
            ),
        )

    mismatches = verify(stored)
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
                event_type="write_mismatch",
                summary="read-back mismatch on " + ",".join(mismatches),
                now=now(),
            )
            session.commit()
        raise AppError(
            ErrorCode.SOURCE_COMMITTED_MISMATCH,
            internal_detail="read-back mismatch on " + ",".join(mismatches),
        )

    # --- step 6: verified, and only now succeeded ----------------------------
    committed_at = now()
    encrypted_result = (
        result_envelope_factory(stored)
        if result_envelope_factory is not None
        else None
    )
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
            encrypted_result=encrypted_result,
        )
        _audit(
            session,
            trace_id=trace_id,
            event_type="write_succeeded",
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
        if duplicate_override is not None:
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
        elif candidates:
            with sessions() as session:
                finding = raise_check(
                    session,
                    entry=entry,
                    candidates=candidates,
                    keyring=keyring,
                    now=now(),
                    idempotency_key=idempotency_key,
                )
                session.commit()
            # Zero writes: no execution row, nothing to reconcile, and a
            # question for Henson instead of a guess.
            return finding

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
        keyring=keyring,
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
    keyring: KeyRing | None = None,
    on_prepared: Callable[[Session], None] | None = None,
    now: Callable[[], datetime] = utc_now,
    new_client_token: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> WriteOutcome:
    """Write one expense and return external evidence, or raise.

    When a `keyring` is given the create payload is sealed into the execution
    row, so a reconciler can replay it after a cold restart (design 7.6.2).
    Without one the write still works, but only in-process recovery is possible.
    """
    require_validated_source(
        config=config,
        validation=validation,
        source=source,
        operation="expense write",
    )
    # Built before anything is persisted: a refusal for a drifted schema, an
    # unknown category or a blank name must cost nothing and leave no row.
    payload = build_expense_payload(entry, config=config, validation=validation)

    def verify(stored: dict) -> list[str]:
        return [
            m.logical_name
            for m in verify_expense_record(entry, stored, config=config)
        ]

    return await execute_governed_write(
        tool=TOOL,
        table_kind=TABLE_KIND,
        payload=payload,
        sessions=sessions,
        adapter=adapter,
        base_token=source.base_token,
        table_id=source.tables[TABLE_KIND],
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        trace_id=trace_id,
        verify=verify,
        config_checksum=config.checksum(),
        on_prepared=on_prepared,
        keyring=keyring,
        now=now,
        new_client_token=new_client_token,
    )
