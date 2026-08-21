"""SEC EDGAR adapter for company fundamentals (public domain, no key).

Pulls ``companyfacts`` (the per-CIK XBRL JSON), which is the free, no-license
source for the six AI companies' capex / OCF / revenue / receivables. SEC asks
for a descriptive User-Agent with a contact; it is pinned here and may be
overridden only by an explicit env var, never inferred from the environment
(§5.1 — a credential/host must not be provider-controlled).
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

EDGAR_BASE_URL = "https://data.sec.gov"
_DEFAULT_UA = "risk-monitor maintainer@example.invalid"
UA_ENV = "RISK_MONITOR_EDGAR_UA"


class EdgarError(RuntimeError):
    pass


class EdgarClient:
    def __init__(
        self,
        base_url: str = EDGAR_BASE_URL,
        transport: Optional[httpx.BaseTransport] = None,
        user_agent: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url
        self._transport = transport
        self._user_agent = user_agent or os.environ.get(UA_ENV) or _DEFAULT_UA
        self._timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            transport=self._transport,
            headers={"User-Agent": self._user_agent},
            timeout=self._timeout,
        )

    def company_facts(self, cik: str) -> dict:
        """The full ``companyfacts`` JSON for a 10-digit (zero-padded) CIK."""
        with self._client() as client:
            resp = client.get(f"/api/xbrl/companyfacts/CIK{cik}.json")
        if resp.status_code == 404:
            raise EdgarError(f"EDGAR: no facts for CIK {cik}")
        if resp.status_code != 200:
            raise EdgarError(f"EDGAR {cik}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def close(self) -> None:
        pass

    def __enter__(self) -> "EdgarClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
