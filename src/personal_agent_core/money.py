"""Decimal-only money helpers.

Binary floats never touch an amount. Every entry point rejects `float` instead
of silently rounding it, because a wrong cent in a ledger is not recoverable by
retrying.

Reference: PRD 13 (amounts use decimal), Finance spec 4.2 and 4.2.1.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final, Literal


CNY: Final[str] = "CNY"
CENT: Final[Decimal] = Decimal("0.01")

EntryKind = Literal["expense", "refund", "aa_reimbursement"]
NEGATIVE_ENTRY_KINDS: Final[frozenset[str]] = frozenset(
    {"refund", "aa_reimbursement"}
)

#: Non-negative decimal string with at most two fraction digits. Shared with the
#: generated tool schemas so the model and the connector cannot disagree.
AMOUNT_PATTERN: Final[str] = r"^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$"

#: Same shape, but signed. Only query filters accept a negative bound, because
#: refunds and AA receipts reduce the personal spend formula.
SIGNED_AMOUNT_PATTERN: Final[str] = r"^-?(0|[1-9][0-9]*)(\.[0-9]{1,2})?$"

CURRENCY_PATTERN: Final[str] = r"^[A-Z]{3}$"

_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(AMOUNT_PATTERN)
_SIGNED_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(SIGNED_AMOUNT_PATTERN)
_CURRENCY_RE: Final[re.Pattern[str]] = re.compile(CURRENCY_PATTERN)


class AmountError(ValueError):
    """An amount could not be accepted as an exact decimal value."""


class CurrencyError(ValueError):
    """A currency code is not a plain ISO 4217 alphabetic code."""


def _require_decimal_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    kind = type(raw).__name__
    raise AmountError(
        f"amounts must be decimal strings, got {kind}; "
        "float and int inputs are rejected rather than coerced"
    )


def parse_signed_amount(raw: object) -> Decimal:
    """Parse a signed decimal string with at most two fraction digits."""
    text = _require_decimal_text(raw)
    if not _SIGNED_AMOUNT_RE.fullmatch(text):
        raise AmountError(f"not an exact two-decimal amount: {text!r}")
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - pattern already guards
        raise AmountError(f"not a decimal amount: {text!r}") from exc


def parse_amount(raw: object) -> Decimal:
    """Parse a strictly positive amount as used by every write tool.

    Zero is always rejected: Finance spec 4.2.1 has no zero-value entry, and a
    user typed minus sign never carries refund or AA semantics on its own.
    """
    text = _require_decimal_text(raw)
    if not _AMOUNT_RE.fullmatch(text):
        raise AmountError(
            f"not a non-negative two-decimal amount: {text!r}; "
            "the sign comes from entry_kind, never from the user's text"
        )
    value = Decimal(text)
    if value == 0:
        raise AmountError("amount must not be zero")
    return value


def parse_currency(raw: object) -> str:
    """Validate an ISO 4217 alphabetic code without guessing ambiguous symbols."""
    if not isinstance(raw, str) or not _CURRENCY_RE.fullmatch(raw):
        raise CurrencyError(f"not an ISO 4217 alphabetic currency code: {raw!r}")
    return raw


def quantize_cny(value: Decimal) -> Decimal:
    """Round to whole cents with ROUND_HALF_UP, as fixed by the technical design."""
    if not isinstance(value, Decimal):
        raise AmountError(
            f"quantize_cny requires Decimal, got {type(value).__name__}"
        )
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def convert_to_cny(amount: Decimal, rate: Decimal) -> Decimal:
    """Convert a foreign amount using an exact reference rate.

    The rate is supplied by the FX connector, never estimated or remembered by
    the model.
    """
    for name, value in (("amount", amount), ("rate", rate)):
        if not isinstance(value, Decimal):
            raise AmountError(
                f"{name} must be Decimal, got {type(value).__name__}"
            )
    if rate <= 0:
        raise AmountError("reference rate must be positive")
    return quantize_cny(amount * rate)


def apply_entry_sign(amount: Decimal, entry_kind: str) -> Decimal:
    """Give a positive amount its ledger sign.

    Refunds and AA receipts are stored negative so the existing Feishu formulas
    reduce the personal spend total. The magnitude is taken as an absolute value
    so an already negative input is never double negated.
    """
    if not isinstance(amount, Decimal):
        raise AmountError(
            f"apply_entry_sign requires Decimal, got {type(amount).__name__}"
        )
    if entry_kind not in {"expense", *NEGATIVE_ENTRY_KINDS}:
        raise AmountError(f"unknown entry_kind: {entry_kind!r}")
    magnitude = abs(amount)
    if magnitude == 0:
        raise AmountError("amount must not be zero")
    return -magnitude if entry_kind in NEGATIVE_ENTRY_KINDS else magnitude


def format_cny(value: Decimal) -> str:
    """Render an exact two-decimal string for receipts and comparisons."""
    return f"{quantize_cny(value):.2f}"
