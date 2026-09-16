"""Yahoo Finance chart adapter for S&P 500 constituent daily closes.

Free, no key, and reachable from the home machine (validated 2026-08-21), but
*unofficial* — subject to throttling or API shape changes. It is used only for
breadth (per-name adjusted closes); the index-level series stay on FRED.

Fails closed: a ticker that 404s, returns no result, or has no closes is
skipped; ``collect_closes`` reports the coverage fraction so the daily job can
surface a coverage drop instead of silently computing breadth from a subset.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import httpx

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


class YahooError(RuntimeError):
    pass


class YahooClient:
    def __init__(
        self,
        base_url: str = YAHOO_CHART_URL,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            transport=self._transport,
            headers=_HEADERS,
            timeout=self._timeout,
        )

    def closes(self, ticker: str, range_: str = "2y") -> list[tuple[str, Optional[float]]]:
        """Ascending ``[(date_iso, adjusted_close_or_None), ...]`` for one ticker.

        Adjusted close is preferred (avoids dividend-gap artefacts in the 200dma);
        falls back to raw close. Returns ``[]`` when the ticker has no data."""
        with self._client() as client:
            resp = client.get(f"/{ticker}", params={"range": range_, "interval": "1d"})
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise YahooError(f"Yahoo {ticker}: HTTP {resp.status_code}: {resp.text[:200]}")
        return self._parse(resp.json())

    def _parse(self, payload: dict) -> list[tuple[str, Optional[float]]]:
        chart = payload.get("chart") or {}
        result = (chart.get("result") or [None])[0]
        if result is None:
            return []
        ts = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        adj = (indicators.get("adjclose") or [{}])
        adj_values = adj[0].get("adjclose") if adj else None
        quote = (indicators.get("quote") or [{}])
        close_values = quote[0].get("close") if quote else None

        out: list[tuple[str, Optional[float]]] = []
        for i, t in enumerate(ts):
            v: Optional[float] = None
            if adj_values and i < len(adj_values) and adj_values[i] is not None:
                v = float(adj_values[i])
            elif close_values and i < len(close_values) and close_values[i] is not None:
                v = float(close_values[i])
            if v is None:
                continue
            d = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
            out.append((d, v))
        return out

    def collect_closes(
        self,
        tickers: list[str],
        max_workers: int = 8,
    ) -> tuple[dict[str, list[tuple[str, Optional[float]]]], dict[str, str]]:
        """Fetch closes for many tickers concurrently. Returns ``(closes, errors)``
        where ``closes`` maps ticker -> series and ``errors`` maps failed ticker ->
        error message. A ticker in ``errors`` is absent from ``closes``."""
        closes: dict[str, list[tuple[str, Optional[float]]]] = {}
        errors: dict[str, str] = {}

        def work(ticker: str) -> None:
            try:
                closes[ticker] = self.closes(ticker)
            except Exception as exc:  # noqa: BLE001 — fail closed per ticker
                errors[ticker] = str(exc)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(work, tickers))
        return closes, errors
