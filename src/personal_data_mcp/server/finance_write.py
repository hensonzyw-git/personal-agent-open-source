"""The three Finance write tools, as MCP handlers.

This is the seam `DEV-027` was missing: the resolvers, the FX layer, the
duplicate gate and the governed write were all built, but nothing bound them to
a tool the Agent could actually call. Everything here is composition -- no new
accounting rule is introduced, and none may be.

Four properties are structural rather than conventional:

- **Nothing is loaded here.** Credentials, the protected ledger config and the
  bound source arrive as injected dependencies. A handler that could open
  `.env.finance.local` or pick a config path would make the "test Base only
  until G5" guarantee a matter of what the process happened to read.
- **The schema is revalidated inside the call.** A payload is addressed by field
  *name* while the protected config identifies fields by *id*; only a fresh
  validation proves that those ids still carry those names. A stale validation
  is not accepted, so drift refuses the write instead of writing to whatever the
  ledger now happens to have.
- **The `duplicate_check_id` never travels on this channel.** A blocked write
  fails with a bare `POSSIBLE_DUPLICATE`; the id lives on the internal control
  plane, keyed by the idempotency key. An MCP result is what the model sees, and
  design 5.2 requires the model never to see or forge that id. The override
  travels the other way for the same reason: it is read from the *verified Host
  Context*, never from the arguments.
- **FX runs here, in the read-only part of the call.** The tool schema carries
  `input_currency`, so currency resolution is a Finance-side concern; the rate is
  resolved before anything is persisted, and an unavailable rate refuses the
  write rather than storing a guess.

The one contract gap worth stating plainly: the frozen model schema has a single
`trip_tag` field, while `resolve_expense` distinguishes a tag the user wrote
from a destination the model merely extracted. The Finance design draft settles
the division of labour -- the model hands over the extracted destination and
never queries the ledger or invents a number -- so the argument is passed as a
*destination*. An existing trip resolves to itself, an unknown one becomes the
plain root, and several same-root trips ask. The single divergence from a
literal reading of "an explicit tag is used as written" is that writing `#东京`
when only `东京01` and `东京02` exist asks instead of creating a third trip,
which is the conservative direction.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.fault_breakpoint import FaultBreakpoint
from personal_agent_core.timeutil import parse_ledger_date, to_rfc3339, utc_now
from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.finance.duplicate_check import (
    DuplicateFinding,
    OverrideRefused,
)
from personal_data_mcp.finance.expense_policy import (
    Clarification,
    ResolvedExpense,
    resolve_expense,
)
from personal_data_mcp.finance.family_fund import FundOutcome, update_family_fund
from personal_data_mcp.finance.fx_connector import FxConnector
from personal_data_mcp.finance.income_write import (
    IncomeClarification,
    write_income,
)
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.ledger_reader import read_year_expenses
from personal_data_mcp.finance.money_resolution import (
    MoneyResolution,
    resolve_money,
    with_currency_suffix,
)
from personal_data_mcp.finance.schema_validator import (
    SchemaValidation,
    observed_field_from_feishu,
    validate_schema,
)
from personal_data_mcp.finance.write_path import WriteOutcome, submit_expense
from personal_data_mcp.server.handlers import ToolHandler, ToolInvocation
from personal_data_mcp.storage.models import FxEvidence


SOURCE_SYSTEM = "feishu_bitable"


@dataclass(frozen=True)
class FinanceWriteDependencies:
    """Everything a write tool needs, all of it supplied by composition."""

    adapter: FeishuAdapter
    source: BaseSource
    config: LedgerConfig
    sessions: sessionmaker[Session]
    keyring: KeyRing
    fx: FxConnector
    now: Callable[[], datetime] = utc_now
    # §13.2 drill hook, injected by composition. Unset in tests and in the
    # credential-free default surface: a write tool without it never pauses.
    fault_breakpoint: FaultBreakpoint | None = None


async def fresh_validation(
    dependencies: FinanceWriteDependencies,
) -> SchemaValidation:
    """Validate the live schema in *this* call, or fail the write closed."""
    observed: dict[str, list] = {}
    for kind, table_id in dependencies.source.tables.items():
        fields = await dependencies.adapter.list_fields(
            dependencies.source.base_token, table_id
        )
        observed[kind] = [observed_field_from_feishu(field) for field in fields]
    return validate_schema(dependencies.config, observed)


def _refused_override() -> AppError:
    """A duplicate override that did not authorise this write.

    Reported as `POSSIBLE_DUPLICATE` rather than an internal error, because that
    is what is actually true: the write is still blocked by a duplicate decision
    and the id presented did not release it -- forged, expired, already spent,
    or raised for a different entry. Which of those it was is not said, since the
    caller here is the model-facing channel. The Agent then asks the control
    plane for a pending check and finds none, and reports a safe failure.
    """
    return AppError(
        ErrorCode.POSSIBLE_DUPLICATE,
        internal_detail="the duplicate override did not authorise this write",
    )


def _duplicate_refusal() -> AppError:
    # Deliberately bare. The candidate cards and the check id are not on the
    # model-facing channel; the Agent reads the pending check from the control
    # plane using the idempotency key it sent.
    return AppError(
        ErrorCode.POSSIBLE_DUPLICATE,
        internal_detail="an exact same-day duplicate is awaiting a decision",
    )


async def _refusing_bad_override(awaitable):
    """Turn a refused override into a stable code instead of an internal error."""
    try:
        return await awaitable
    except OverrideRefused as refused:
        raise _refused_override() from refused


def _clarification_refusal(reason: str) -> AppError:
    return AppError(
        ErrorCode.CLARIFICATION_REQUIRED,
        internal_detail=f"the write needs an answer first: {reason}",
    )


def _record_receipt(
    outcome: WriteOutcome, *, table: str, record: dict[str, Any]
) -> dict[str, Any]:
    """Project a governed write onto the frozen record output schema."""
    return {
        "status": outcome.status,
        "record_id": outcome.record_id,
        "source_system": SOURCE_SYSTEM,
        "table": table,
        "committed_at": to_rfc3339(outcome.committed_at),
        # A replay knows the receipt, not what this call happened to resolve;
        # reporting the freshly resolved fields as "the record" would describe
        # a write that may never have had them.
        "record": {} if outcome.status == "idempotent_replay" else record,
        "evidence": {"kind": "feishu_record", "external_id": outcome.record_id},
    }


def _fx_recorder(
    resolution: MoneyResolution, *, idempotency_key: str
) -> Callable[[Session], None] | None:
    """Persist the quote that justified a converted amount, exactly once.

    The hook runs inside the `prepared` transaction, so a durable execution
    always carries the rate that produced its amount. It is re-entrant because a
    retry after a crash re-enters `prepared` with the same key.
    """
    audit = resolution.fx_audit
    if audit is None:
        return None

    def record(session: Session) -> None:
        existing = session.scalars(
            select(FxEvidence).where(
                FxEvidence.idempotency_key == idempotency_key
            )
        ).first()
        if existing is not None:
            return
        session.add(
            FxEvidence(
                evidence_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                base_currency=audit.original_currency,
                quote_currency="CNY",
                rate=str(audit.rate),
                provider=audit.rate_source,
                quote_date=audit.quote_date.isoformat(),
                fetched_at=audit.quoted_at,
            )
        )

    return record


def _amount(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# --- finance.log_expense -----------------------------------------------------


def build_expense_handler(
    dependencies: FinanceWriteDependencies,
) -> ToolHandler:
    """The only handler allowed to expose `finance.log_expense`."""

    async def handler(invocation: ToolInvocation) -> dict[str, Any]:
        arguments = invocation.arguments
        call = invocation.verified_call
        validation = await fresh_validation(dependencies)

        # 1. Read-only: the amount to store, and the rate that justified it.
        money = await resolve_money(
            input_amount=arguments["input_amount"],
            input_currency=arguments.get("input_currency", "CNY"),
            settlement_amount_cny=arguments.get("settlement_amount_cny"),
            fx=dependencies.fx,
        )

        # 2. Read-only: what the ledger already holds, for the trip resolver,
        #    the refund matcher and the duplicate gate alike.
        rows = await read_year_expenses(
            dependencies.adapter,
            source=dependencies.source,
            config=dependencies.config,
        )
        resolved = resolve_expense(
            name=arguments["name"],
            # `parse_amount` accepts decimal *text* only -- it refuses a float
            # or an int rather than coercing one -- and the resolved magnitude
            # is already cent-quantised, so this round-trips exactly.
            input_amount=str(money.amount_cny),
            occurred_on=parse_ledger_date(arguments["occurred_on"]),
            is_family_expense=arguments["is_family_expense"],
            entry_kind=arguments["entry_kind"],
            category=arguments.get("category"),
            # See the module docstring: the model's `trip_tag` is the extracted
            # destination, and only the ledger decides which trip it names.
            destination=arguments.get("trip_tag"),
            ledger_rows=rows,
        )
        if isinstance(resolved, Clarification):
            raise _clarification_refusal(resolved.reason.value)
        assert isinstance(resolved, ResolvedExpense)

        # The suffix lands after the trip tag, and before the duplicate gate:
        # the gate matches on the values that will actually be stored.
        entry = replace(
            resolved.entry,
            name=with_currency_suffix(resolved.entry.name, money),
        )

        outcome = await _refusing_bad_override(submit_expense(
            entry,
            ledger_rows=rows,
            keyring=dependencies.keyring,
            duplicate_override=call.duplicate_override,
            sessions=dependencies.sessions,
            adapter=dependencies.adapter,
            config=dependencies.config,
            validation=validation,
            source=dependencies.source,
            idempotency_key=call.idempotency_key,
            request_fingerprint=call.request_fingerprint,
            trace_id=call.trace_id,
            on_prepared=_fx_recorder(
                money, idempotency_key=call.idempotency_key
            ),
            now=dependencies.now,
            fault_breakpoint=dependencies.fault_breakpoint,
        ))
        if isinstance(outcome, DuplicateFinding):
            raise _duplicate_refusal()

        return _record_receipt(
            outcome,
            table="expense",
            record={
                "name": entry.name,
                "amount_cny": str(entry.amount_cny),
                "occurred_on": entry.occurred_on.isoformat(),
                "is_family_expense": entry.is_family_expense,
                "category": entry.category,
            },
        )

    return handler


# --- finance.log_income ------------------------------------------------------


def build_income_handler(
    dependencies: FinanceWriteDependencies,
) -> ToolHandler:
    """The only handler allowed to expose `finance.log_income`."""

    async def handler(invocation: ToolInvocation) -> dict[str, Any]:
        arguments = invocation.arguments
        call = invocation.verified_call
        validation = await fresh_validation(dependencies)

        money = await resolve_money(
            input_amount=arguments["input_amount"],
            input_currency=arguments.get("input_currency", "CNY"),
            settlement_amount_cny=arguments.get("settlement_amount_cny"),
            fx=dependencies.fx,
        )
        outcome = await _refusing_bad_override(write_income(
            description=arguments["income_description"],
            # Income carries no entry kind: the amount is a positive magnitude
            # and a refund is an expense reduction, never an income row.
            amount_cny=money.amount_cny,
            occurred_on=parse_ledger_date(arguments["occurred_on"]),
            sessions=dependencies.sessions,
            adapter=dependencies.adapter,
            config=dependencies.config,
            validation=validation,
            source=dependencies.source,
            idempotency_key=call.idempotency_key,
            request_fingerprint=call.request_fingerprint,
            trace_id=call.trace_id,
            keyring=dependencies.keyring,
            duplicate_override=call.duplicate_override,
            on_prepared=_fx_recorder(
                money, idempotency_key=call.idempotency_key
            ),
            now=dependencies.now,
            fault_breakpoint=dependencies.fault_breakpoint,
        ))
        if isinstance(outcome, IncomeClarification):
            raise _clarification_refusal(outcome.value)
        if isinstance(outcome, DuplicateFinding):
            raise _duplicate_refusal()

        return _record_receipt(
            outcome,
            table="income",
            record={
                "amount_cny": str(money.amount_cny),
                "occurred_on": arguments["occurred_on"],
            },
        )

    return handler


# --- finance.update_family_fund ---------------------------------------------


def build_family_fund_handler(
    dependencies: FinanceWriteDependencies,
) -> ToolHandler:
    """The only handler allowed to expose `finance.update_family_fund`."""

    async def handler(invocation: ToolInvocation) -> dict[str, Any]:
        arguments = invocation.arguments
        call = invocation.verified_call
        validation = await fresh_validation(dependencies)

        outcome: FundOutcome = await update_family_fund(
            mode=arguments["mode"],
            recharge_amount_cny=_amount(arguments.get("recharge_amount_cny")),
            target_balance_cny=_amount(arguments.get("target_balance_cny")),
            note=arguments.get("note"),
            sessions=dependencies.sessions,
            adapter=dependencies.adapter,
            config=dependencies.config,
            validation=validation,
            source=dependencies.source,
            idempotency_key=call.idempotency_key,
            request_fingerprint=call.request_fingerprint,
            trace_id=call.trace_id,
            keyring=dependencies.keyring,
            now=dependencies.now,
            fault_breakpoint=dependencies.fault_breakpoint,
        )
        return {
            "status": outcome.status,
            "record_id": outcome.record_id,
            "mode": outcome.mode,
            "recharge_amount_cny": str(outcome.recharge_amount_cny),
            "balance_before_cny": (
                None if outcome.balance_before_cny is None
                else str(outcome.balance_before_cny)
            ),
            "balance_after_cny": str(outcome.balance_after_cny),
            "note": outcome.note,
            "evidence": {
                "kind": "feishu_record",
                "external_id": outcome.record_id,
            },
        }

    return handler
