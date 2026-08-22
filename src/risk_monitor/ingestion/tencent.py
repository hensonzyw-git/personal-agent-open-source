"""Tencent Finance kline adapter for S&P 500 constituent daily closes.

China-reachable replacement for Yahoo, which geo-blocks the production ECS
(Aliyun mainland IP) with a hard 403. Free, no key. Returns *unadjusted* (raw)
daily closes — the dividend-adjusted convention is deliberately abandoned for a
China-reachable source, and the breadth band thresholds are recalibrated to
match (see ADR-0001 and the 2026-08-22 raw-vs-adjusted measurement).

Fails closed: a ticker with no ``day`` array, an HTTP error, or an unparseable
row is skipped; ``collect_closes`` reports the coverage fraction so the daily
job can surface a coverage drop instead of silently computing breadth from a
subset.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import httpx

TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
# Trading days to pull: >= 200 (200dma) + 20 (breadth 20d change) + margin.
_DEFAULT_COUNT = 500


class TencentError(RuntimeError):
    pass


class TencentClient:
    def __init__(
        self,
        tencent_codes: Optional[dict[str, str]] = None,
        base_url: str = TENCENT_KLINE_URL,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 30.0,
        count: int = _DEFAULT_COUNT,
    ) -> None:
        self.tencent_codes = tencent_codes or {}
        self.base_url = base_url
        self._transport = transport
        self._timeout = timeout
        self.count = count

    def _param_code(self, ticker: str) -> str:
        """The canonical Tencent code (``AAPL.OQ``, ``JPM.N``, ``BRK.B.N``) plus
        the ``us`` market prefix. Missing from the snapshot -> fail closed."""
        canonical = self.tencent_codes.get(ticker)
        if canonical is None:
            raise TencentError(f"Tencent {ticker}: no canonical code in snapshot")
        return f"us{canonical}"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            headers=_HEADERS,
            timeout=self._timeout,
        )

    def closes(self, ticker: str) -> list[tuple[str, Optional[float]]]:
        """Ascending ``[(date_iso, raw_close_or_None), ...]`` for one ticker."""
        code = self._param_code(ticker)
        params = {"param": f"{code},day,,,{self.count},qfq"}
        with self._client() as client:
            resp = client.get(self.base_url, params=params)
        if resp.status_code != 200:
            raise TencentError(f"Tencent {ticker}: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            # A 200 with a non-JSON body (HTML interstitial, truncated stream)
            # is the same failure class as an HTTP error, not a parse surprise.
            raise TencentError(f"Tencent {ticker}: non-JSON 200 body: {exc}") from exc
        return self._parse(payload, code)

    def _parse(self, payload: dict, code: str) -> list[tuple[str, Optional[float]]]:
        data = (payload.get("data") or {}).get(code) or {}
        rows = data.get("day") or []
        out: list[tuple[str, Optional[float]]] = []
        for row in rows:
            # Tencent day row: [date, open, close, high, low, volume]; close = index 2.
            if not row or len(row) < 3:
                continue
            d = row[0]
            try:
                v = float(row[2])
            except (TypeError, ValueError):
                v = None
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
