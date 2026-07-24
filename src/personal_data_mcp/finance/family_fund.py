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
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import ledger_date, ledger_day_epoch_millis, utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.endpoints import SEARCH_RECORDS
from personal_data_mcp.finance.expense_record import as_decimal
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.write_path import execute_governed_write
from personal_data_mcp.storage.execution_store import (
    acquire_resource_lock,
    release_resource_lock,
)
from personal_data_mcp.storage.models import ExternalReceipt, ToolExecution


TOOL: str = "finance.update_family_fund"
TABLE_KIND: str = "family_fund"
INTEREST_NOTE: str = "利息补齐"

_RESULT_TABLE: str = "tool_executions"
_RESULT_COLUMN: str = "encrypted_result"


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
    items = data.get("items") or []
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
            return as_decimal(value[0])
        return as_decimal(value)
    return as_decimal(raw)


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
    if (
        execution is None
        or execution.state != "succeeded"
        or execution.encrypted_result is None
    ):
        return None
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


async def update_family_fund(
    *,
    mode: str,
    recharge_amount_cny: Decimal | None = None,
    target_balance_cny: Decimal | None = None,
    note: str | None = None,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    config: LedgerConfig,
    source: BaseSource,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    keyring: KeyRing,
    owner: str = "family-fund",
    now: Callable[[], datetime] = utc_now,
    new_client_token: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> FundOutcome:
    """Serialise, read the balance, write one recharge, verify against target."""
    fields = _fields(config)
    lock_key = f"family_fund:{config.ledger_year}"

    # A quick replay check before taking the lock keeps a settled key cheap.
    with sessions() as session:
        replayed = _replay(session, idempotency_key, keyring=keyring)
        if replayed is not None:
            return replayed

    with sessions() as session:
        if not acquire_resource_lock(
            session, lock_key=lock_key, owner=owner, now=now()
        ):
            session.commit()
            raise AppError(
                ErrorCode.SOURCE_UNAVAILABLE,
                internal_detail="family fund is being updated concurrently",
            )
        session.commit()

    try:
        current = await read_current_balance(adapter, source=source, config=config)
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

        def verify(stored: dict[str, Any]) -> list[str]:
            after = _balance_of(stored, balance_name)
            if after is None:
                return ["balance"]
            if mode == "interest_reconcile" and after != target_balance_cny:
                # An external change moved the balance; do not auto-correct.
                return ["balance"]
            return []

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
            keyring=keyring,
            now=now,
            new_client_token=new_client_token,
        )
    finally:
        with sessions() as session:
            release_resource_lock(session, lock_key=lock_key, owner=owner)
            session.commit()

    after = _balance_of(outcome.stored_fields, balance_name)
    _persist_result(
        sessions,
        key=idempotency_key,
        keyring=keyring,
        mode=mode,
        recharge=recharge,
        balance_before=current,
        balance_after=after,
        note=stored_note,
    )
    return FundOutcome(
        status=outcome.status,
        record_id=outcome.record_id,
        mode=mode,
        recharge_amount_cny=recharge,
        balance_before_cny=current,
        balance_after_cny=after if after is not None else Decimal("0"),
        note=stored_note,
    )


def _persist_result(
    sessions: sessionmaker[Session],
    *,
    key: str,
    keyring: KeyRing,
    mode: str,
    recharge: Decimal,
    balance_before: Decimal | None,
    balance_after: Decimal | None,
    note: str | None,
) -> None:
    """Seal enough on the execution to replay the receipt without recomputing."""
    result = {
        "mode": mode,
        "recharge": str(recharge),
        "balance_before": None if balance_before is None else str(balance_before),
        "balance_after": None if balance_after is None else str(balance_after),
        "note": note,
    }
    sealed = keyring.encrypt(
        json.dumps(result, ensure_ascii=False).encode("utf-8"),
        table=_RESULT_TABLE,
        column=_RESULT_COLUMN,
        row_id=key,
    )
    with sessions() as session:
        execution = session.get(ToolExecution, key)
        if execution is None:
            return
        execution.encrypted_result = sealed
        session.flush()
        session.commit()
