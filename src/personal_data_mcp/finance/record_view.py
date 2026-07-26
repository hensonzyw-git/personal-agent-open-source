"""Project one ledger record's *current* values for the daily review card.

`DEV-028`, technical design 7.7 step 5: when the card is opened, the fields are
read from Feishu again, so a correction Henson made on the computer shows up
immediately. That is why `daily_review_items` stores only a pointer -- a cached
amount would show the value at write time, which is exactly the value a manual
correction replaces.

Two properties matter more than convenience here:

- **nothing is invented.** Each cell is read through the same normaliser the
  write path verifies with, chosen by the *configured* field type. A cell that
  does not parse is reported as unreadable, never as `null`, `0` or `""`; a card
  that quietly showed a zero would be worse than one that says it could not read
  the field.
- **only configured fields leave.** The projection is driven by the protected
  config, so a column someone adds to the Base later cannot start flowing to the
  client through this path.

Values are rendered JSON-safe (`Decimal` as a plain string, a ledger date as
`YYYY-MM-DD`) so the control plane never has to re-guess a type.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from personal_data_mcp.finance.expense_record import (
    as_decimal,
    as_ledger_date,
    as_text,
)
from personal_data_mcp.finance.ledger_config import (
    FieldType,
    LedgerConfig,
)


@dataclass(frozen=True)
class RecordView:
    """One record's current values, plus what could not be read."""

    table_kind: str
    record_id: str
    #: Logical field name -> JSON-safe current value.
    values: dict[str, Any]
    #: Logical fields present in the config whose cell did not parse.
    unreadable_fields: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "table_kind": self.table_kind,
            "record_id": self.record_id,
            "values": dict(self.values),
            "unreadable_fields": list(self.unreadable_fields),
        }


def project_record(
    stored_fields: dict[str, Any],
    *,
    config: LedgerConfig,
    table_kind: str,
    record_id: str,
) -> RecordView:
    """Read every configured field of one record, by meaning."""

    table = config.tables.get(table_kind)
    if table is None:
        raise KeyError(f"the ledger config has no {table_kind} table")

    values: dict[str, Any] = {}
    unreadable: list[str] = []
    for logical, spec in table.fields.items():
        raw = stored_fields.get(spec.expected_name)
        value = _normalise(raw, spec.type)
        if value is _UNREADABLE:
            unreadable.append(logical)
            continue
        values[logical] = value

    return RecordView(
        table_kind=table_kind,
        record_id=record_id,
        values=values,
        unreadable_fields=tuple(sorted(unreadable)),
    )


class _Unreadable:
    """Distinct from `None`, which is a legitimate empty cell."""

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "<unreadable>"


_UNREADABLE = _Unreadable()


def _normalise(raw: Any, field_type: FieldType) -> Any:
    if raw is None:
        # An empty optional cell (an unset checkbox, a category left blank) is
        # genuinely empty. Only a checkbox has a defined meaning for absence.
        return False if field_type is FieldType.CHECKBOX else None

    if field_type is FieldType.NUMBER:
        return _decimal_or_unreadable(raw)
    if field_type is FieldType.DATETIME:
        day = as_ledger_date(raw)
        return _UNREADABLE if day is None else day.isoformat()
    if field_type is FieldType.CHECKBOX:
        return raw if isinstance(raw, bool) else _UNREADABLE
    if field_type is FieldType.FORMULA:
        return _formula_or_unreadable(raw)
    # TEXT and SINGLE_SELECT are exact text.
    text = as_text(raw)
    return _UNREADABLE if text is None else text


def _decimal_or_unreadable(raw: Any) -> Any:
    amount = as_decimal(raw)
    return _UNREADABLE if amount is None else _as_money_string(amount)


def _formula_or_unreadable(raw: Any) -> Any:
    """Accept only the formula envelope that has live provider evidence.

    `DEV-022` observed `{"type": 2, "value": [20]}` from `search_records`.
    `get_record` has not yet been observed for a formula cell, so a plausible
    bare number must stay unreadable until that boundary is verified live.
    """

    if isinstance(raw, dict):
        if raw.get("type") != 2:
            return _UNREADABLE
        inner = raw.get("value")
        if not isinstance(inner, list) or len(inner) != 1:
            return _UNREADABLE
        return _decimal_or_unreadable(inner[0])
    return _UNREADABLE


def _as_money_string(amount: Decimal) -> str:
    """A money value crosses the wire as text, never as a JSON float."""
    return format(amount, "f")
