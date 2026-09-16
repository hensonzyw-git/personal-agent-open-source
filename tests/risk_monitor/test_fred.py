"""Offline tests for the FRED adapter — mock transport, no network."""

from __future__ import annotations

import httpx
import pytest

from risk_monitor.ingestion.fred import FRED_SERIES, FredClient, FredError


def _mock(handler):
    return httpx.MockTransport(handler)


def test_observations_parses_missing_marker():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fred/series/observations"
        assert request.url.params["series_id"] == "BAMLH0A0HYM2"
        assert request.url.params["api_key"] == "k"
        return httpx.Response(200, json={"observations": [
            {"date": "2026-08-20", "value": "2.75"},
            {"date": "2026-08-19", "value": "."},
        ]})

    with FredClient(api_key="k", transport=_mock(handler)) as c:
        rows = c.observations("BAMLH0A0HYM2", limit=2)
    assert rows == [("2026-08-20", 2.75), ("2026-08-19", None)]


def test_latest_returns_most_recent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"observations": [
            {"date": "2026-08-20", "value": "2.75"},
        ]})

    with FredClient(api_key="k", transport=_mock(handler)) as c:
        assert c.latest("X") == ("2026-08-20", 2.75)


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with FredClient(api_key="k", transport=_mock(handler)) as c:
        with pytest.raises(FredError):
            c.observations("X")


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(FredError):
        FredClient(api_key=None)


def test_series_registry_has_required_metrics():
    for mid in ("credit.hy_oas_pct", "credit.bbb_oas_pct", "market.vix", "market.spx_close"):
        assert mid in FRED_SERIES
