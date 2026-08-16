"""The strict, whitelisted projection of a governed write's ledger record.

This is `G1`. Until now a write receipt carried a `record_id` and nothing else,
so the iOS receipt card had no business fields to draw and fell back to its
lightest form. The connector already returned the written fields on its MCP
result; the Agent dropped them at `McpFinanceDispatcher.commit`, which kept only
the record id. This module is the display contract that lets them through.

It is deliberately built like `finance_query_projection`, and for the same
reason: the MCP output schema is the *connector's* contract, while what a client
may render is a narrower one that has to fail closed on anything it has not seen
before. Everything the card could mis-render is refused here instead:

- an unknown key, a wrong type, or a payload that is not a JSON object is
  refused, so raw MCP content can never reach the card;
- amounts stay **strings**, never floats. They are decimal money that came out
  of the ledger as text, and a float round-trip is how ¥0.10 becomes ¥0.099999.
  A numeric amount is therefore a decode failure, not something to coerce;
- `to_dict()` round-trips through the same decoder, so the durable carrier
  persisted on the operation and the live projection cannot disagree about what
  a receipt says.

Two absences are contract, not omission:

- **A replay has no record.** `_record_receipt` returns `record: {}` for
  `idempotent_replay`, because a replay knows its receipt and not what the
  original call resolved. That decodes to `None` here and the card honestly
  falls back to the status row rather than showing fields from the wrong call.
- **`personal_spend_cny` may be absent.** 个人支出 is a Feishu *formula* field
  (`ledger_config.EXPECTED_TABLE_FIELDS["expense"]["personal_spend"]`), so it
  exists only if the read-back saw it computed. It is optional for exactly that
  reason, and never reconstructed locally: recomputing family sharing on this
  side is precisely what the read-only formula field exists to prevent.

`category` is nullable because the write contract allows it: a refund or AA
reimbursement may carry no category (`tool_ir` line 240). A card showing an
empty category is correct there; inventing one would not be.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from personal_agent_core.crypto import KeyRing
from personal_agent_core.manifest import canonical_json


#: The stable safe-failure reason an unprojectable record receives. A closed,
#: reviewed string like the other safe-failure reasons, never derived from the
#: result itself.
RECORD_UNREADABLE = "record_projection_unreadable"


class FinanceRecordProjectionError(ValueError):
    """A write result's `record` is not a safe, projectable shape.

    Callers fail closed on the *record only*: the write itself is still proven
    by its `record_id`, so an unreadable record costs the card its fields and
    never costs the user their receipt.
    """


#: Every key a projectable expense record may carry. Anything else is refused
#: rather than dropped: a field this build has never seen is a field it cannot
#: promise a client will ignore safely.
_EXPENSE_FIELDS = frozenset(
    {
        "name",
        "amount_cny",
        "occurred_on",
        "is_family_expense",
        "category",
        "personal_spend_cny",
        "category_updated_at",
    }
)


@dataclass(frozen=True)
class FinanceExpenseRecord:
    """One expense row as the receipt card may display it.

    `category_updated_at` is what makes the card honest after `分类` is edited.
    Henson's decision (2026-08-15) is that the card follows the ledger's current
    value rather than freezing at what was first written, which means the card
    is no longer literally the write receipt. This timestamp is the difference
    being stated on the card instead of hidden: set once a category edit has
    been verified against the ledger, absent while the row still reads as
    written. The original value is not carried here — it lives in the audit
    trail, which is the place a superseded fact belongs.
    """

    name: str
    amount_cny: str
    occurred_on: str
    is_family_expense: bool
    category: str | None = None
    personal_spend_cny: str | None = None
    category_updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "name": self.name,
            "amount_cny": self.amount_cny,
            "occurred_on": self.occurred_on,
            "is_family_expense": self.is_family_expense,
            "category": self.category,
        }
        if self.personal_spend_cny is not None:
            record["personal_spend_cny"] = self.personal_spend_cny
        if self.category_updated_at is not None:
            record["category_updated_at"] = self.category_updated_at
        return record

    def with_category(
        self, category: str, *, updated_at: str
    ) -> "FinanceExpenseRecord":
        """The same row after a verified category edit.

        `personal_spend_cny` is deliberately **dropped**, not carried over. It is
        a Base formula output, and this side does not know whether the formula
        depends on 分类. Keeping a stale value would put a number on the card
        that the ledger may no longer agree with, and recomputing it here would
        reconstruct the very formula the config marks read-only. Absent is the
        only honest third option; the next read-back supplies the real one.
        """
        return FinanceExpenseRecord(
            name=self.name,
            amount_cny=self.amount_cny,
            occurred_on=self.occurred_on,
            is_family_expense=self.is_family_expense,
            category=category,
            personal_spend_cny=None,
            category_updated_at=updated_at,
        )


def decode_finance_expense_record(
    raw: dict[str, Any] | str | None,
) -> FinanceExpenseRecord | None:
    """Strictly decode a write result's `record` into the display projection.

    Accepts the MCP result's `record` dict or its canonical JSON string (the
    durable carrier on the operation), so one decoder reads both live receipts
    and history and they cannot drift apart.

    Returns `None` for the two shapes that legitimately carry no record — a
    missing key and the empty dict an `idempotent_replay` returns — and raises
    for everything else. That distinction matters: "this call had no record to
    report" is a contract outcome, while "the record did not decode" is a defect
    or tampering and must not be silently rendered as the former.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FinanceRecordProjectionError(
                "record is not valid JSON"
            ) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise FinanceRecordProjectionError("record is not an object")
    if not data:
        # `idempotent_replay`: the connector states it does not know this call's
        # fields. Not a failure, and not something to fill in.
        return None

    unknown = set(data) - _EXPENSE_FIELDS
    if unknown:
        raise FinanceRecordProjectionError(
            f"record carries unknown fields: {', '.join(sorted(unknown))}"
        )

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise FinanceRecordProjectionError("record name is not a non-empty string")

    occurred_on = data.get("occurred_on")
    if not isinstance(occurred_on, str) or not occurred_on:
        raise FinanceRecordProjectionError(
            "record occurred_on is not a non-empty string"
        )

    family = data.get("is_family_expense")
    # `isinstance(True, int)` is True in Python, so the bool check has to come
    # first everywhere in this module; here it is the only accepted type anyway.
    if not isinstance(family, bool):
        raise FinanceRecordProjectionError(
            "record is_family_expense is not a boolean"
        )

    category = data.get("category")
    if category is not None and (not isinstance(category, str) or not category):
        raise FinanceRecordProjectionError(
            "record category is not a non-empty string or null"
        )

    updated_at = data.get("category_updated_at")
    if updated_at is not None and (
        not isinstance(updated_at, str) or not updated_at
    ):
        raise FinanceRecordProjectionError(
            "record category_updated_at is not a non-empty string or null"
        )

    return FinanceExpenseRecord(
        name=name,
        amount_cny=_require_amount(data.get("amount_cny"), field="amount_cny"),
        occurred_on=occurred_on,
        is_family_expense=family,
        category=category,
        personal_spend_cny=_optional_amount(
            data.get("personal_spend_cny"), field="personal_spend_cny"
        ),
        category_updated_at=updated_at,
    )


_TABLE = "operations"
_COLUMN = "encrypted_result_record"


def seal_expense_record(
    keyring: KeyRing, *, operation_id: str, record: FinanceExpenseRecord
) -> dict[str, Any]:
    """Seal a receipt's ledger row for storage on its own operation row.

    Bound to `operation_id` as additional data, like every other sealed column
    here, so a ciphertext lifted onto a different operation fails to open rather
    than putting one write's amount on another write's card.
    """
    plaintext = canonical_json(record.to_dict()).encode("utf-8")
    return keyring.encrypt(
        plaintext, table=_TABLE, column=_COLUMN, row_id=operation_id
    )


def open_expense_record(
    keyring: KeyRing, *, operation_id: str, envelope: dict[str, Any]
) -> FinanceExpenseRecord | None:
    """Open a sealed receipt record, or report that it cannot be shown.

    Returns `None` for anything that does not open and decode cleanly. That is
    the fail-closed direction and it is safe here for the same reason the
    projection is optional everywhere else: the receipt's proof is its
    `record_id`, which lives in a separate, unsealed column. A key rotation, a
    truncated envelope or a payload from an older projection therefore costs the
    card its rows and never costs the user their receipt -- and never raises on
    a read path that is otherwise a plain projection.
    """
    try:
        plaintext = keyring.decrypt(
            envelope, table=_TABLE, column=_COLUMN, row_id=operation_id
        )
    except Exception:  # noqa: BLE001 - any failure to open means "cannot show"
        return None
    try:
        return decode_finance_expense_record(plaintext.decode("utf-8"))
    except (FinanceRecordProjectionError, UnicodeDecodeError):
        return None


def _require_amount(raw: Any, *, field: str) -> str:
    """A money value, as the string the ledger stated it in.

    A number is refused rather than stringified. `Decimal(str(0.1 + 0.2))` is
    `'0.30000000000000004'`, and a receipt that renders that has silently
    invented a fact about the user's ledger.
    """
    if isinstance(raw, bool) or isinstance(raw, (int, float)):
        raise FinanceRecordProjectionError(
            f"record {field} is a number; money must stay a decimal string"
        )
    if not isinstance(raw, str) or not raw:
        raise FinanceRecordProjectionError(
            f"record {field} is not a non-empty string"
        )
    return raw


def _optional_amount(raw: Any, *, field: str) -> str | None:
    if raw is None:
        return None
    return _require_amount(raw, field=field)
