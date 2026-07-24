"""Turn a model's `input_amount`/`input_currency` into a CNY amount to store.

DEV-023. This is the small policy that sits *above* the FX connector and decides
whether a rate is even needed. Finance spec 5.4 fixes three branches, in this
order of priority:

1. **The user's actual CNY settlement wins.** When `settlement_amount_cny` is
   given, it is stored exactly and no rate is ever queried -- it is a real
   settled amount, not a reference estimate, so it carries no currency suffix and
   no FX audit. Henson types this once he knows the true figure, and the Agent
   must never later overwrite it with a reference-rate guess (spec 5.4).
2. **CNY passes straight through.** A CNY amount with no settlement override is
   used as given, with no network call.
3. **A foreign amount is converted at the current reference rate.** Only here is
   the connector consulted; the exact rate produces a quantised CNY amount, the
   original amount and currency become a human-removable name suffix
   (`（10,000 JPY）`), and the rate, quote date and source are captured as audit.

The amount returned is always a **positive magnitude**: the accounting sign still
comes from `entry_kind`, applied downstream by `money.apply_entry_sign`, exactly
as it did before FX existed. This module never invents a rate, and an unavailable
rate propagates as `FX_RATE_UNAVAILABLE` so the write is refused rather than
completed with a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.money import (
    CNY,
    convert_to_cny,
    parse_amount,
    parse_currency,
)
from personal_data_mcp.finance.fx_connector import FxConnector


#: Full-width brackets, matching the ledger convention in Finance spec 5.4.
_SUFFIX_OPEN: Final[str] = "（"
_SUFFIX_CLOSE: Final[str] = "）"


@dataclass(frozen=True)
class FxAudit:
    """The provenance of a converted amount, for operations data only.

    None of this is ever shown to the model or used to reconstruct a rate; it
    exists so a receipt and the audit log can say exactly how a reference-estimate
    amount was derived. `is_reference_estimate` is always true here, because a
    converted amount is by definition an estimate the user may later correct.
    """

    original_amount: Decimal
    original_currency: str
    rate: Decimal
    rate_source: str
    quote_date: date
    quoted_at: datetime
    is_reference_estimate: bool = True


@dataclass(frozen=True)
class MoneyResolution:
    """The CNY magnitude to store, plus what a foreign entry adds to it.

    `amount_cny` is a positive, cent-quantised magnitude; the caller applies the
    accounting sign. `currency_suffix` is appended to the *display* name (after
    any trip tag) so it reads `机票 #东京（10,000 JPY）`, and is None for a plain
    CNY entry. `fx_audit` is present only when a reference rate was applied.
    """

    amount_cny: Decimal
    currency_suffix: str | None
    fx_audit: FxAudit | None


async def resolve_money(
    *,
    input_amount: str | Decimal,
    input_currency: str = CNY,
    settlement_amount_cny: str | Decimal | None = None,
    fx: FxConnector | None = None,
) -> MoneyResolution:
    """Resolve the CNY amount for one entry, querying a rate only if needed."""
    currency = parse_currency(input_currency)

    # 1. The user's real settled figure always wins, with no rate query.
    if settlement_amount_cny is not None:
        return MoneyResolution(
            amount_cny=parse_amount(settlement_amount_cny),
            currency_suffix=None,
            fx_audit=None,
        )

    # 2. CNY needs no conversion.
    if currency == CNY:
        return MoneyResolution(
            amount_cny=parse_amount(input_amount),
            currency_suffix=None,
            fx_audit=None,
        )

    # 3. A foreign amount is converted at the current reference rate.
    if fx is None:  # pragma: no cover - a caller wiring error, not a user path
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="a foreign-currency amount needs an FX connector",
        )
    original = parse_amount(input_amount)
    quote = await fx.rate(currency)
    amount_cny = convert_to_cny(original, quote.rate)
    return MoneyResolution(
        amount_cny=amount_cny,
        currency_suffix=_currency_suffix(original, currency),
        fx_audit=FxAudit(
            original_amount=original,
            original_currency=currency,
            rate=quote.rate,
            rate_source=quote.source,
            quote_date=quote.quote_date,
            quoted_at=quote.fetched_at,
        ),
    )


def _currency_suffix(amount: Decimal, currency: str) -> str:
    """Render the removable original-amount suffix, e.g. `（10,000 JPY）`."""
    return f"{_SUFFIX_OPEN}{amount:,} {currency}{_SUFFIX_CLOSE}"


def with_currency_suffix(display_name: str, resolution: MoneyResolution) -> str:
    """Append the currency suffix to an already-resolved display name.

    The trip tag is applied first (`display_name`), so the suffix lands last:
    `机票 #东京（10,000 JPY）`. A CNY entry has no suffix and the name is returned
    unchanged.
    """
    if resolution.currency_suffix is None:
        return display_name
    return display_name + resolution.currency_suffix
