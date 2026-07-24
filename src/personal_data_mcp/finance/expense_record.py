"""What one expense entry looks like in Feishu, and what read-back must prove.

Two directions, deliberately written as one module so they cannot drift apart:
building the create payload, and deciding whether the record that came back is
the record we meant to write. Design 9.4 makes the second half the definition of
success -- a `record_id` alone is not success, and neither is the model saying
"done".

**Why the payload is keyed by field name.** The Bitable v1 record APIs key
`fields` by field *name*, while the protected config locates every field by
*id*. That is not a contradiction, it is the reason drift validation is a
precondition here: the config binds id to expected name, `validate_schema`
proves the live Base still agrees, and only then is writing by name equivalent
to writing by id. So `build_expense_payload` refuses to build anything unless it
is handed a valid validation for the same config -- a renamed column becomes a
refused write, never a write into the wrong column.

Only the five configured expense fields are ever sent. The formula columns
(`个人支出`, `家庭基金变动`) and the auto-number `ID` are not in the config at
all, so there is no code path that addresses them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.money import quantize_cny
from personal_agent_core.timeutil import LEDGER_TIMEZONE, ledger_day_epoch_millis
from personal_data_mcp.finance.ledger_config import FieldType, LedgerConfig
from personal_data_mcp.finance.schema_validator import SchemaValidation


@dataclass(frozen=True)
class ExpenseEntry:
    """One fully resolved expense, as it will be stored.

    Every ambiguity is already gone by the time an entry exists: `amount_cny`
    carries its accounting sign, `occurred_on` is an absolute ledger date, and
    `is_family_expense` was stated by the user rather than inferred. Resolving
    those is the policy layer's job (DEV-021); this module only stores what it
    is given, and preserves the user's `name` byte for byte.
    """

    name: str
    amount_cny: Decimal
    occurred_on: date
    is_family_expense: bool
    category: str


@dataclass(frozen=True)
class FieldMismatch:
    """One field whose stored value is not what was written."""

    logical_name: str
    #: Both sides are rendered as short, comparable text. Amounts and names are
    #: ledger content, so a mismatch is reported by field, not by value.
    detail: str


def _writable_expense_fields(config: LedgerConfig) -> dict[str, Any]:
    table = config.tables.get("expense")
    if table is None:  # pragma: no cover - the config validator forbids this
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail="the ledger config has no expense table",
        )
    return table.fields


def _require_valid_schema(validation: SchemaValidation, config: LedgerConfig) -> None:
    """Write only against a schema proven to match the config, just now.

    Design 9.3: a drifted schema breaks every write immediately. Because the
    payload is keyed by name, this is not merely a policy check -- it is what
    makes the name safe to use as an address at all.
    """
    if not validation.is_valid:
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail=(
                f"expense write refused: schema is {validation.status}"
            ),
        )
    if validation.config_version != config.config_version:
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail=(
                "the schema validation belongs to a different config version"
            ),
        )


def build_expense_payload(
    entry: ExpenseEntry,
    *,
    config: LedgerConfig,
    validation: SchemaValidation,
) -> dict[str, Any]:
    """The exact `fields` object to send, or a refusal."""
    _require_valid_schema(validation, config)
    fields = _writable_expense_fields(config)

    category_spec = fields["category"]
    if entry.category not in (category_spec.options or ()):
        # The connector never creates a select option (design 9.2), so an
        # unknown category is a refusal rather than something to reconcile.
        raise AppError(
            ErrorCode.CATEGORY_NOT_ALLOWED,
            internal_detail=f"category {entry.category!r} is not a ledger option",
        )
    if not entry.name.strip():
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="expense name must not be blank",
        )
    if entry.amount_cny == 0:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="a zero-amount expense is never written",
        )

    amount = quantize_cny(entry.amount_cny)
    return {
        fields["amount"].expected_name: float(amount),
        fields["name"].expected_name: entry.name,
        fields["occurred_on"].expected_name: ledger_day_epoch_millis(
            entry.occurred_on
        ),
        fields["is_family_expense"].expected_name: entry.is_family_expense,
        fields["category"].expected_name: entry.category,
    }


def as_text(value: Any) -> str | None:
    """Normalise a Bitable text value.

    A text cell comes back either as a plain string or as a list of rich-text
    segments (`[{"text": ..., "type": "text"}]`) depending on the field and API
    version. Both are the same stored content, so both normalise to the same
    string rather than one of them being reported as a mismatch.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [
            segment.get("text", "")
            for segment in value
            if isinstance(segment, dict)
        ]
        if len(parts) == len(value):
            return "".join(parts)
    return None


def as_decimal(value: Any) -> Decimal | None:
    """Read a number cell as an exact 2dp Decimal.

    Bitable stores a number as a double, so the value arrives as a JSON float.
    Going through `str` recovers the shortest representation that round-trips,
    which for a two-decimal money value is the value itself; quantising then
    makes `20` and `20.0` compare equal to `20.00`.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return quantize_cny(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return None
    return None


def as_ledger_date(value: Any) -> date | None:
    """Read a datetime cell back as the ledger day it represents.

    Bitable returns epoch milliseconds. The ledger's dates are Asia/Shanghai
    days, so the instant is converted in that zone -- reading it as UTC would
    shift any entry made before 08:00 to the previous day.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    moment = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    return moment.astimezone(LEDGER_TIMEZONE).date()


def verify_expense_record(
    entry: ExpenseEntry,
    stored_fields: dict[str, Any],
    *,
    config: LedgerConfig,
) -> list[FieldMismatch]:
    """Compare every written field against what the ledger now holds.

    Comparison is by meaning, not by JSON: Decimal for the amount, an absolute
    Asia/Shanghai date for the day, a real boolean for the family flag, and
    exact text for the name and category. An unreadable value is a mismatch,
    never a pass -- "I could not parse it" must not resolve to success.
    """
    fields = _writable_expense_fields(config)
    mismatches: list[FieldMismatch] = []

    def stored(logical: str) -> Any:
        return stored_fields.get(fields[logical].expected_name)

    if as_decimal(stored("amount")) != quantize_cny(entry.amount_cny):
        mismatches.append(
            FieldMismatch("amount", "stored amount differs from the written amount")
        )
    if as_text(stored("name")) != entry.name:
        mismatches.append(
            FieldMismatch("name", "stored name differs from the user's text")
        )
    if as_ledger_date(stored("occurred_on")) != entry.occurred_on:
        mismatches.append(
            FieldMismatch("occurred_on", "stored ledger date differs")
        )
    family = stored("is_family_expense")
    # A checkbox that was never set can legitimately come back absent; only
    # `false` and absence are the same, and any non-boolean is a mismatch.
    if family is None:
        family = False
    if not isinstance(family, bool) or family is not entry.is_family_expense:
        mismatches.append(
            FieldMismatch("is_family_expense", "stored family scope differs")
        )
    if as_text(stored("category")) != entry.category:
        mismatches.append(FieldMismatch("category", "stored category differs"))

    return mismatches


def expense_field_types(config: LedgerConfig) -> dict[str, FieldType]:
    """The configured logical field types, for diagnostics and tests."""
    return {
        logical: spec.type
        for logical, spec in _writable_expense_fields(config).items()
    }
