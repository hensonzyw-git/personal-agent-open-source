"""The foreign-exchange connector: Frankfurter's ECB reference rate, to CNY.

DEV-023. Finance spec 5.4 fixes every choice this module makes, and each one is a
safety property, not a convenience:

- **One provider, pinned.** Phase 1 uses Frankfurter's `ECB` data only. The host
  and path are constants; no base url is ever accepted from configuration or an
  argument, so this cannot become a general HTTP client or be steered at a
  scraped search page.
- **Current reference rate, never historical.** The rate is whatever Frankfurter
  reports *now*; the ledger date is irrelevant to it. When ECB has not published
  for today, Frankfurter returns the most recent available quote and its own
  `date`, which this module records rather than silently treating as today.
- **Decimal, never float.** The provider sends the rate as a JSON number, so it
  arrives as a float. It is turned into a `Decimal` through `str()` -- the same
  shortest-round-trip path the ledger read-back uses -- and never multiplied as a
  binary float. The rate keeps full precision here; the final CNY amount is
  quantised once, downstream, by `money.convert_to_cny`.
- **Fail closed.** A timeout, a transport error, a non-200, non-JSON, a missing
  or non-positive rate, a base the provider did not echo, or an unparseable quote
  date all raise `FX_RATE_UNAVAILABLE`. The caller's contract (spec 4.3) is that
  an unavailable rate writes *nothing* -- so this never guesses, remembers a
  stale rate past its TTL, or returns a partial result.
- **A short cache.** A quote is reused for 15 minutes, keyed by the source
  currency, so a burst of entries in one currency makes one call. The cache holds
  only a fetched quote; a failure is never cached.

The model never sees, estimates, or supplies a rate: it names a currency, and the
rate comes from here. Converting CNY to CNY needs no rate, so this refuses `CNY`
rather than making a pointless call -- that passthrough belongs to the resolution
layer above.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.money import CNY, parse_currency
from personal_data_mcp.feishu.redaction import redact_for_log


#: The only host and path. Frankfurter needs no API key and defaults to ECB data.
FRANKFURTER_BASE_URL: Final[str] = "https://api.frankfurter.dev"
_LATEST_PATH: Final[str] = "/v1/latest"

#: Recorded on every quote so a receipt can say where the rate came from.
RATE_SOURCE: Final[str] = "frankfurter_ecb"

#: Finance spec 5.4: a reference rate is reused for 15 minutes.
DEFAULT_CACHE_TTL_SECONDS: Final[float] = 15 * 60

#: A reference-rate lookup is a read; an unavailable rate simply blocks the write.
_TIMEOUT_SECONDS: Final[float] = 8.0


@dataclass(frozen=True)
class FxQuote:
    """One current reference rate from a source currency into CNY.

    `rate` is exact and unrounded; `quote_date` is the ECB quote day the provider
    reported, which is today only when ECB has already published for today.
    `fetched_at` is when this process retrieved it, used both for the cache TTL
    and for the audit trail.
    """

    currency: str
    rate: Decimal
    quote_date: date
    fetched_at: datetime
    source: str = RATE_SOURCE


class FxConnector:
    """Fetches and briefly caches Frankfurter ECB reference rates into CNY."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        transport: httpx.AsyncBaseTransport | None = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._now = now
        self._ttl = cache_ttl_seconds
        self._client = httpx.AsyncClient(transport=transport, trust_env=False)
        self._cache: dict[str, FxQuote] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FxConnector":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def rate(self, currency: str) -> FxQuote:
        """The current reference rate from `currency` into CNY, or a refusal.

        A fresh cached quote is returned without a call. `CNY` is refused: there
        is no CNY->CNY rate to fetch, and asking for one is a caller bug, not a
        network round trip.
        """
        code = parse_currency(currency)
        if code == CNY:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="no reference rate is needed to convert CNY to CNY",
            )

        cached = self._cache.get(code)
        if cached is not None and self._is_fresh(cached):
            return cached

        quote = await self._fetch(code)
        self._cache[code] = quote
        return quote

    def _is_fresh(self, quote: FxQuote) -> bool:
        age = (self._now() - quote.fetched_at).total_seconds()
        # A clock that appears to run backwards (age < 0) is treated as stale, so
        # a bad clock refetches rather than pinning a quote forever.
        return 0 <= age < self._ttl

    async def _fetch(self, currency: str) -> FxQuote:
        url = FRANKFURTER_BASE_URL + _LATEST_PATH
        try:
            response = await self._client.get(
                url,
                params={"base": currency, "symbols": CNY},
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.FX_RATE_UNAVAILABLE,
                internal_detail=redact_for_log(
                    f"frankfurter transport error: {type(exc).__name__}"
                ),
            ) from exc

        if response.status_code != 200:
            raise AppError(
                ErrorCode.FX_RATE_UNAVAILABLE,
                internal_detail=f"frankfurter returned HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AppError(
                ErrorCode.FX_RATE_UNAVAILABLE,
                internal_detail="frankfurter returned non-JSON",
            ) from exc
        if not isinstance(body, dict):
            raise AppError(
                ErrorCode.FX_RATE_UNAVAILABLE,
                internal_detail="frankfurter response was not a JSON object",
            )
        return self._quote_of(currency, body)

    def _quote_of(self, currency: str, body: dict[str, Any]) -> FxQuote:
        # The provider must echo the base we asked for; a mismatch means the
        # response is not the one we requested, so we refuse rather than convert
        # with the wrong pair.
        if body.get("base") != currency:
            raise AppError(
                ErrorCode.FX_RATE_UNAVAILABLE,
                internal_detail="frankfurter echoed a different base currency",
            )
        rates = body.get("rates")
        if not isinstance(rates, dict) or CNY not in rates:
            raise AppError(
                ErrorCode.FX_RATE_UNAVAILABLE,
                internal_detail="frankfurter response carried no CNY rate",
            )
        rate = _decimal_rate(rates[CNY])
        if rate is None or rate <= 0:
            raise AppError(
                ErrorCode.FX_RATE_UNAVAILABLE,
                internal_detail="frankfurter CNY rate was not a positive number",
            )
        quote_date = _quote_date(body.get("date"))
        if quote_date is None:
            raise AppError(
                ErrorCode.FX_RATE_UNAVAILABLE,
                internal_detail="frankfurter response carried no valid quote date",
            )
        return FxQuote(
            currency=currency,
            rate=rate,
            quote_date=quote_date,
            fetched_at=self._now(),
        )


def _decimal_rate(value: Any) -> Decimal | None:
    """Read a JSON rate as an exact Decimal, rejecting bool and junk.

    Frankfurter sends the rate as a JSON number, so it arrives as a float; going
    through `str` recovers its shortest round-tripping representation. A bool is
    rejected explicitly (it is an `int` subclass) so `true` never reads as `1`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _quote_date(value: Any) -> date | None:
    """Parse Frankfurter's ISO `date` (the ECB quote day) or return None."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
