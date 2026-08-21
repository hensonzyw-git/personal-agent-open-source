"""FRED (St. Louis Fed) adapter for market / credit / treasury series.

Personal non-commercial use (ADR-0001). Requires ``FRED_API_KEY`` in the
environment or passed explicitly. Returns raw observations; FRED's ``"."``
missing marker is mapped to ``None`` (never coerced to 0).
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

FRED_BASE_URL = "https://api.stlouisfed.org/fred"

# metric_id -> FRED series_id for the daily raw series we ingest. The derived
# metrics (20d changes, vs-200dma, breadth) are computed downstream, not pulled.
FRED_SERIES: dict[str, str] = {
    "credit.hy_oas_pct": "BAMLH0A0HYM2",
    "credit.bbb_oas_pct": "BAMLC0A4CBBB",
    "treasury.10y_yield": "DGS10",
    "treasury.30y_yield": "DGS30",
    "market.spx_close": "SP500",
    "market.vix": "VIXCLS",
}


class FredError(RuntimeError):
    pass


class FredClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = FRED_BASE_URL,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("FRED_API_KEY")
        if not self.api_key:
            raise FredError("FRED_API_KEY is not set")
        self.base_url = base_url
        self._client = httpx.Client(base_url=base_url, transport=transport, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FredClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def observations(
        self,
        series_id: str,
        *,
        limit: Optional[int] = None,
        sort: str = "desc",
    ) -> list[tuple[str, Optional[float]]]:
        """Return ``[(date, value), ...]``. ``value`` is ``None`` when FRED reports
        ``"."`` (missing). Raises ``FredError`` on a non-2xx response."""
        params: dict[str, str] = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": sort,
        }
        if limit is not None:
            params["limit"] = str(limit)

        resp = self._client.get("/series/observations", params=params)
        if resp.status_code != 200:
            raise FredError(f"FRED {series_id}: HTTP {resp.status_code}: {resp.text[:200]}")
        rows = resp.json().get("observations", [])
        return [
            (o["date"], None if o.get("value") == "." else float(o["value"]))
            for o in rows
        ]

    def latest(self, series_id: str) -> Optional[tuple[str, Optional[float]]]:
        rows = self.observations(series_id, limit=1, sort="desc")
        return rows[0] if rows else None

    def history(self, series_id: str) -> list[tuple[str, Optional[float]]]:
        """Full ascending history, for local archiving (ADR-0001: the ICE BofA
        OAS window is shrinking, so we archive from day one)."""
        return self.observations(series_id, sort="asc")
