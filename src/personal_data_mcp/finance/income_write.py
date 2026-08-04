"""Writing one income row: policy, payload, and the shared crash-safe write.

Income reuses the governed write skeleton (`execute_governed_write`) but nothing
of the expense *semantics*. The amount is positive by contract, the name and
category come from the closed Income Policy rather than the model, and the table
carries no family, trip, or entry-kind field at all.

Like every write here, success is a read-back that matches what was sent, and
the source is rebound to the validated config so a write cannot land on an
unvalidated Base.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.fault_breakpoint import FaultBreakpoint
from personal_agent_core.money import quantize_cny
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import ledger_day_epoch_millis, utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.finance.expense_record import (
    FieldMismatch,
    as_decimal,
    as_ledger_date,
    as_text,
)
from personal_data_mcp.finance.duplicate_check import (
    DuplicateFinding,
    authorise_override,
    find_duplicates,
    raise_check,
)
from personal_data_mcp.finance.income_policy import (
    IncomeClarification,
    ResolvedIncome,
    resolve_income,
)
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.ledger_reader import read_year_incomes
from personal_data_mcp.finance.schema_validator import SchemaValidation
from personal_data_mcp.finance.source_guard import require_validated_source
from personal_data_mcp.finance.write_path import WriteOutcome, execute_governed_write
from personal_data_mcp.storage.models import ToolExecution


TOOL: str = "finance.log_income"
TABLE_KIND: str = "income"


@dataclass(frozen=True)
class IncomeDuplicateIntent:
    name: str
    amount_cny: Decimal
    occurred_on: date
    category: str


def _income_fields(config: LedgerConfig):
    table = config.tables.get("income")
    if table is None:  # pragma: no cover - the config validator forbids this
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail="the ledger config has no income table",
        )
    return table.fields


def build_income_payload(
    *,
    name: str,
    amount_cny: Decimal,
    occurred_on: date,
    category: str,
    config: LedgerConfig,
    validation: SchemaValidation,
) -> dict[str, Any]:
    """The exact income `fields` to send, or a refusal.

    The amount is stored positive: income never carries an accounting sign, and
    a non-positive amount is refused here rather than written.
    """
    if not validation.is_valid or validation.config_checksum != config.checksum():
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail="income write refused: schema is not valid for this config",
        )
    fields = _income_fields(config)
    if category not in (fields["category"].options or ()):
        raise AppError(
            ErrorCode.CATEGORY_NOT_ALLOWED,
            internal_detail=f"income category {category!r} is not a ledger option",
        )
    amount = quantize_cny(amount_cny)
    if amount <= 0:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="income amount must be positive",
        )
    if not name.strip():
        raise AppError(
            ErrorCode.INVALID_ARGUMENT, internal_detail="income name must not be blank"
        )
    return {
        fields["amount"].expected_name: float(amount),
        fields["name"].expected_name: name,
        fields["occurred_on"].expected_name: ledger_day_epoch_millis(occurred_on),
        fields["category"].expected_name: category,
    }


def verify_income_record(
    *,
    name: str,
    amount_cny: Decimal,
    occurred_on: date,
    category: str,
    stored_fields: dict[str, Any],
    config: LedgerConfig,
) -> list[FieldMismatch]:
    """Compare the four income fields against the read-back, by meaning."""
    fields = _income_fields(config)
    mismatches: list[FieldMismatch] = []

    def stored(logical: str) -> Any:
        return stored_fields.get(fields[logical].expected_name)

    if as_decimal(stored("amount")) != quantize_cny(amount_cny):
        mismatches.append(FieldMismatch("amount", "stored income amount differs"))
    if as_text(stored("name")) != name:
        mismatches.append(FieldMismatch("name", "stored income name differs"))
    if as_ledger_date(stored("occurred_on")) != occurred_on:
        mismatches.append(FieldMismatch("occurred_on", "stored income date differs"))
    if as_text(stored("category")) != category:
        mismatches.append(FieldMismatch("category", "stored income category differs"))
    return mismatches


async def write_income(
    *,
    description: str,
    amount_cny: Decimal,
    occurred_on: date,
    sessions: sessionmaker[Session],
    adapter: FeishuAdapter,
    config: LedgerConfig,
    validation: SchemaValidation,
    source: BaseSource,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    keyring: KeyRing,
    duplicate_override: str | None = None,
    on_prepared: Callable[[Session], None] | None = None,
    now: Callable[[], datetime] = utc_now,
    new_client_token: Callable[[], str] = lambda: str(uuid.uuid4()),
    fault_breakpoint: FaultBreakpoint | None = None,
) -> WriteOutcome | IncomeClarification | DuplicateFinding:
    """Resolve the income, then write it. A clarification writes nothing."""
    resolved = resolve_income(description)
    if isinstance(resolved, IncomeClarification):
        return resolved
    assert isinstance(resolved, ResolvedIncome)

    require_validated_source(
        config=config,
        validation=validation,
        source=source,
        operation="income write",
    )

    payload = build_income_payload(
        name=resolved.name,
        amount_cny=amount_cny,
        occurred_on=occurred_on,
        category=resolved.category,
        config=config,
        validation=validation,
    )

    duplicate_intent = IncomeDuplicateIntent(
        name=resolved.name,
        amount_cny=quantize_cny(amount_cny),
        occurred_on=occurred_on,
        category=resolved.category,
    )
    with sessions() as session:
        already_started = session.get(ToolExecution, idempotency_key) is not None

    if not already_started:
        rows = await read_year_incomes(adapter, source=source, config=config)
        candidates = find_duplicates(duplicate_intent, rows)
        if duplicate_override is not None:
            with sessions() as session:
                # See `write_path`: read-then-CAS, so a lost snapshot retries
                # into the same refusal rather than a lock error.
                run_write_transaction(
                    session,
                    lambda: authorise_override(
                        session,
                        check_id=duplicate_override,
                        entry=duplicate_intent,
                        current_candidates=candidates,
                        keyring=keyring,
                        now=now(),
                    ),
                )
        elif candidates:
            with sessions() as session:
                finding = raise_check(
                    session,
                    entry=duplicate_intent,
                    candidates=candidates,
                    keyring=keyring,
                    now=now(),
                    idempotency_key=idempotency_key,
                )
                session.commit()
            return finding

    def verify(stored: dict[str, Any]) -> list[str]:
        return [
            m.logical_name
            for m in verify_income_record(
                name=resolved.name,
                amount_cny=amount_cny,
                occurred_on=occurred_on,
                category=resolved.category,
                stored_fields=stored,
                config=config,
            )
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
        fault_breakpoint=fault_breakpoint,
    )
