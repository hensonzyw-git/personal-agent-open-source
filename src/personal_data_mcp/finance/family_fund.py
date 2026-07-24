"""The family-fund ledger write: top up, or reconcile to a target balance.

This tool only ever writes one field, `充值金额`. The ledger's own formula turns
that into `实际入账 = 充值金额 × 2` and rolls it into `家庭基金余额`; the tool
never writes `实际入账`, `家庭基金余额`, or the `初始余额` baseline. Two facts
about the real table drive the shape here, both confirmed against it:

- the create response does *not* carry the formula values, so the balance is
  always read back with a separate read, never trusted from the write echo;
- `家庭基金余额` is a table-wide running total, identical on every row, so the
  current balance is whatever the latest read returns.

Two modes (design 4.6):

- `top_up` writes a positive recharge directly;
- `interest_reconcile` reads the current balance, and writes `(target - current)
  / 2` -- halved because the formula doubles it, so the balance lands exactly on
  the target. The division is exact Decimal, not rounded to cents. A target at or
  below the current balance writes nothing.

The whole read-compute-write-verify cycle is serialised per ledger year by a
durable lock, because two interleaved reconciles would each compute against a
stale balance. If an external change in Feishu means the post-write balance is
not the target, the tool reports it and keeps the record -- it never writes a
second correcting row, and it never writes a negative recharge.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import ledger_date, ledger_day_epoch_millis, utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.endpoints import SEARCH_RECORDS
from personal_data_mcp.finance.expense_record import verify_stored_against_sent
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.schema_validator import SchemaValidation
from personal_data_mcp.finance.source_guard import require_validated_source
from personal_data_mcp.finance.write_path import execute_governed_write
from personal_data_mcp.storage.execution_store import (
    acquire_resource_lock,
    release_resource_lock,
    renew_resource_lock,
)
from personal_data_mcp.storage.models import (
    TERMINAL_EXECUTION_STATES,
    ExternalReceipt,
    ToolExecution,
)


TOOL: str = "finance.update_family_fund"
TABLE_KIND: str = "family_fund"
INTEREST_NOTE: str = "利息补齐"

_RESULT_TABLE: str = "tool_executions"
_RESULT_COLUMN: str = "encrypted_result"

# Adapter requests are bounded (8 s read, 10 s create, three 8 s read-backs).
# This covers the complete worst-case cycle with ample margin; the lease is
# renewed after the initial balance read and immediately before the write.
FAMILY_FUND_LOCK_SECONDS: int = 120


@dataclass(frozen=True)
class FundOutcome:
    status: str  # "created" | "idempotent_replay"
    record_id: str
    mode: str
    recharge_amount_cny: Decimal
    balance_before_cny: Decimal | None
    balance_after_cny: Decimal
    note: str | None


def _fields(config: LedgerConfig):
    table = config.tables.get("family_fund")
    if table is None:  # pragma: no cover
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail="the ledger config has no family_fund table",
        )
    return table.fields


async def read_current_balance(
    adapter: FeishuAdapter, *, source: BaseSource, config: LedgerConfig
) -> Decimal | None:
    """The current family-fund balance, or None if the table has no rows yet.

    `家庭基金余额` is the same running total on every row, so any row answers the
    question; the newest is read to be least surprising. The value is taken from
    the formula's underlying number, never the two-decimal display text.
    """
    fields = _fields(config)
    balance_name = fields["balance"].expected_name
    data = await adapter.request(
        SEARCH_RECORDS,
        params={
            "app_token": source.base_token,
            "table_id": source.tables[TABLE_KIND],
        },
        json={"field_names": [balance_name], "automatic_fields": False},
        query={"page_size": "500"},
    )
    items = data.get("items")
    if items is None:
        items = []
    if not isinstance(items, list) or not all(
        isinstance(item, dict) for item in items
    ):
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail="family-fund balance read returned malformed items",
        )
    if not items:
        return None
    # Every row carries the same total; the last one is as good as any.
    return _balance_of(items[-1].get("fields", {}), balance_name)


def _balance_of(cells: dict[str, Any], balance_name: str) -> Decimal | None:
    """Read a formula balance cell's underlying Decimal value."""
    raw = cells.get(balance_name)
    if isinstance(raw, dict):
        value = raw.get("value")
        if isinstance(value, list) and value:
            return _exact_decimal(value[0])
        return _exact_decimal(value)
    return _exact_decimal(raw)


def _compute_recharge(
    *,
    mode: str,
    recharge_amount_cny: Decimal | None,
    target_balance_cny: Decimal | None,
    current_balance: Decimal | None,
) -> Decimal:
    """The recharge to write, or a refusal. Never returns a non-positive value."""
    if mode == "top_up":
        if recharge_amount_cny is None or recharge_amount_cny <= 0:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="top_up recharge must be positive",
            )
        return recharge_amount_cny

    if mode == "interest_reconcile":
        if target_balance_cny is None:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="interest_reconcile needs a target balance",
            )
        if current_balance is None:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="cannot reconcile without a current balance",
            )
        delta = target_balance_cny - current_balance
        if delta == 0:
            raise AppError(
                ErrorCode.NO_CHANGE_REQUIRED,
                internal_detail="target already equals the current balance",
            )
        if delta < 0:
            raise AppError(
                ErrorCode.TARGET_BELOW_CURRENT_BALANCE,
                internal_detail="target is below the current balance",
            )
        # Exact halving: the formula doubles it, so the balance lands on target.
        # Deliberately not quantised to cents (design 4.6).
        return delta / 2

    raise AppError(
        ErrorCode.INVALID_ARGUMENT, internal_detail=f"unknown mode {mode!r}"
    )


def _replay(session: Session, key: str, *, keyring: KeyRing) -> FundOutcome | None:
    """Recover a settled outcome from the sealed result. Never recomputes.

    A replay must return the same record id *and* the same post-write balance
    (design 4.6), so it reads the balance that was true at write time out of the
    sealed result rather than re-reading a balance that may have since moved.
    """
    execution = session.get(ToolExecution, key)
    if execution is None or execution.state != "succeeded":
        return None
    if execution.encrypted_result is None:
        raise AppError(
            ErrorCode.SOURCE_COMMITTED_MISMATCH,
            internal_detail=(
                "family-fund execution succeeded without its sealed result; "
                "manual review is required"
            ),
        )
    receipt = (
        session.query(ExternalReceipt)
        .filter(ExternalReceipt.idempotency_key == key)
        .one()
    )
    result = json.loads(
        keyring.decrypt(
            execution.encrypted_result,
            table=_RESULT_TABLE,
            column=_RESULT_COLUMN,
            row_id=key,
        ).decode("utf-8")
    )
    return FundOutcome(
        status="idempotent_replay",
        record_id=receipt.record_id,
        mode=result["mode"],
        recharge_amount_cny=Decimal(result["recharge"]),
        balance_before_cny=(
            Decimal(result["balance_before"])
            if result["balance_before"] is not None
            else None
        ),
        balance_after_cny=Decimal(result["balance_after"]),
        note=result["note"],
    )


def _other_unfinished_execution(
    session: Session, *, idempotency_key: str
) -> ToolExecution | None:
    return (
        session.query(ToolExecution)
        .filter(
            ToolExecution.tool == TOOL,
            ToolExecution.idempotency_key != idempotency_key,
            ToolExecution.state.not_in(sorted(TERMINAL_EXECUTION_STATES)),
        )
        .order_by(ToolExecution.created_at)
        .first()
    )


def _refuse_unfinished_family_fund(
    session: Session, *, idempotency_key: str
) -> None:
    """Keep a prior unknown write ahead of every new read-compute-write cycle."""
    if _other_unfinished_execution(
        session, idempotency_key=idempotency_key
    ) is not None:
        raise AppError(
            ErrorCode.SOURCE_COMMIT_UNKNOWN,
            internal_detail=(
                "a previous family-fund write is unfinished; "
                "recover it before starting another"
            ),
        )


async def update_family_fund(
    *,
    mode: str,
    recharge_amount_cny: Decimal | None = None,
    target_balance_cny: Decimal | None = None,
    note: str | None = None,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    config: LedgerConfig,
    validation: SchemaValidation,
    source: BaseSource,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    keyring: KeyRing,
    owner: str | None = None,
    now: Callable[[], datetime] = utc_now,
    new_client_token: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> FundOutcome:
    """Serialise, read the balance, write one recharge, verify against target."""
    require_validated_source(
        config=config,
        validation=validation,
        source=source,
        operation="family-fund write",
    )
    fields = _fields(config)
    lock_key = f"family_fund:{config.ledger_year}"
    lock_owner = f"{owner or 'family-fund'}:{uuid.uuid4()}"

    # A quick replay check before taking the lock keeps a settled key cheap.
    with sessions() as session:
        replayed = _replay(session, idempotency_key, keyring=keyring)
        if replayed is not None:
            return replayed
        existing = session.get(ToolExecution, idempotency_key)
        if existing is not None:
            raise AppError(
                ErrorCode.SOURCE_COMMIT_UNKNOWN,
                internal_detail=(
                    f"{idempotency_key} is at {existing.state}; "
                    "recovery belongs to the reconciler"
                ),
            )
        _refuse_unfinished_family_fund(
            session, idempotency_key=idempotency_key
        )

    with sessions() as session:
        if not acquire_resource_lock(
            session,
            lock_key=lock_key,
            owner=lock_owner,
            now=now(),
            seconds=FAMILY_FUND_LOCK_SECONDS,
        ):
            session.commit()
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="family fund is being updated concurrently",
            )
        session.commit()

    try:
        # Repeat after acquiring the lock: two fresh operations may both pass
        # the optimistic pre-check, but only the one that prepared first may
        # proceed if it became unknown before releasing the lock.
        with sessions() as session:
            _refuse_unfinished_family_fund(
                session, idempotency_key=idempotency_key
            )
        current = await read_current_balance(adapter, source=source, config=config)
        if current is None:
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail=(
                    "family-fund balance is unavailable; refusing an "
                    "unverifiable recharge"
                ),
            )
        _renew_family_fund_lock(
            sessions,
            lock_key=lock_key,
            owner=lock_owner,
            now=now,
        )
        recharge = _compute_recharge(
            mode=mode,
            recharge_amount_cny=recharge_amount_cny,
            target_balance_cny=target_balance_cny,
            current_balance=current,
        )

        # The family-fund contract has no date input in either mode, so the
        # record is dated the operation day; interest reconcile explicitly
        # forbids backfilling a historical date (design 4.6).
        occurred_on: date = ledger_date(now())
        stored_note = INTEREST_NOTE if mode == "interest_reconcile" else note
        payload: dict[str, Any] = {
            fields["recharge_amount"].expected_name: float(recharge),
            fields["occurred_on"].expected_name: ledger_day_epoch_millis(occurred_on),
        }
        if stored_note is not None:
            payload[fields["note"].expected_name] = stored_note

        balance_name = fields["balance"].expected_name
        expected_after = current + recharge * 2
        recovery_context: dict[str, Any] = {
            "mode": mode,
            "recharge": str(recharge),
            "balance_before": str(current),
            "balance_after": str(expected_after),
            "note": stored_note,
        }

        def verify(stored: dict[str, Any]) -> list[str]:
            return verify_family_fund_recovery(
                payload,
                stored,
                config=config,
                recovery_context=recovery_context,
            )

        def seal_result(stored: dict[str, Any]) -> dict[str, Any]:
            return seal_family_fund_result(
                stored,
                config=config,
                recovery_context=recovery_context,
                keyring=keyring,
                idempotency_key=idempotency_key,
            )

        _renew_family_fund_lock(
            sessions,
            lock_key=lock_key,
            owner=lock_owner,
            now=now,
        )
        outcome = await execute_governed_write(
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
            recovery_context=recovery_context,
            result_envelope_factory=seal_result,
            keyring=keyring,
            now=now,
            new_client_token=new_client_token,
        )
    finally:
        with sessions() as session:
            release_resource_lock(
                session, lock_key=lock_key, owner=lock_owner
            )
            session.commit()

    after = _balance_of(outcome.stored_fields, balance_name)
    if after is None:  # verify() makes this unreachable on a created outcome
        raise AppError(
            ErrorCode.SOURCE_COMMITTED_MISMATCH,
            internal_detail="family-fund write has no verified post-write balance",
        )
    return FundOutcome(
        status=outcome.status,
        record_id=outcome.record_id,
        mode=mode,
        recharge_amount_cny=recharge,
        balance_before_cny=current,
        balance_after_cny=after,
        note=stored_note,
    )


def _renew_family_fund_lock(
    sessions: sessionmaker[Session],
    *,
    lock_key: str,
    owner: str,
    now: Callable[[], datetime],
) -> None:
    with sessions() as session:
        held = renew_resource_lock(
            session,
            lock_key=lock_key,
            owner=owner,
            now=now(),
            seconds=FAMILY_FUND_LOCK_SECONDS,
        )
        session.commit()
    if not held:
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail="family-fund lock was lost before the write",
        )


def _exact_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    return None


def verify_family_fund_recovery(
    sent_fields: dict[str, Any],
    stored_fields: dict[str, Any],
    *,
    config: LedgerConfig,
    recovery_context: dict[str, Any],
) -> list[str]:
    """Verify both the written row and the formula effect needed for success."""
    mismatches = {
        mismatch.logical_name
        for mismatch in verify_stored_against_sent(
            sent_fields,
            stored_fields,
            config=config,
            table_kind=TABLE_KIND,
        )
    }
    fields = _fields(config)
    recharge_name = fields["recharge_amount"].expected_name
    if _exact_decimal(sent_fields.get(recharge_name)) != _exact_decimal(
        stored_fields.get(recharge_name)
    ):
        mismatches.add("recharge_amount")

    expected_after = _exact_decimal(recovery_context.get("balance_after"))
    actual_after = _balance_of(stored_fields, fields["balance"].expected_name)
    if expected_after is None or actual_after != expected_after:
        mismatches.add("balance")
    return sorted(mismatches)


def seal_family_fund_result(
    stored_fields: dict[str, Any],
    *,
    config: LedgerConfig,
    recovery_context: dict[str, Any],
    keyring: KeyRing,
    idempotency_key: str,
) -> dict[str, Any]:
    """Seal the verified domain result for the same transaction as success."""
    balance_after = _balance_of(
        stored_fields, _fields(config)["balance"].expected_name
    )
    if balance_after is None:
        raise AppError(
            ErrorCode.SOURCE_COMMITTED_MISMATCH,
            internal_detail="cannot seal a family-fund result without a balance",
        )
    result = {
        "mode": recovery_context["mode"],
        "recharge": recovery_context["recharge"],
        "balance_before": recovery_context["balance_before"],
        "balance_after": str(balance_after),
        "note": recovery_context.get("note"),
    }
    return keyring.encrypt(
        json.dumps(result, ensure_ascii=False).encode("utf-8"),
        table=_RESULT_TABLE,
        column=_RESULT_COLUMN,
        row_id=idempotency_key,
    )
