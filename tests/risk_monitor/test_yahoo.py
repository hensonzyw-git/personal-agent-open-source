"""Yahoo chart adapter tests (MockTransport, no network)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from risk_monitor.ingestion.yahoo import YahooClient, YahooError


def _ts(day: int) -> int:
    return int(datetime(2026, 8, day, 13, 30, tzinfo=timezone.utc).timestamp())


def _chart(rows, adjclose=True, close=True):
    ts = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    indicators = {}
    if adjclose:
        indicators["adjclose"] = [{"adjclose": vals}]
    if close:
        indicators["quote"] = [{"close": vals}]
    return {"chart": {"result": [{"timestamp": ts, "indicators": indicators}], "error": None}}


def _client(handler):
    return YahooClient(transport=httpx.MockTransport(handler))


def test_closes_parses_adjclose():
    def handler(req):
        return httpx.Response(200, json=_chart([(_ts(19), 100.0), (_ts(20), 101.0)]))

    c = _client(handler)
    assert c.closes("AAPL") == [("2026-08-19", 100.0), ("2026-08-20", 101.0)]


def test_closes_404_returns_empty():
    def handler(req):
        return httpx.Response(404, json={"chart": {"error": {"code": "Not Found"}}})

    assert _client(handler).closes("DELISTED") == []


def test_closes_non_200_raises():
    def handler(req):
        return httpx.Response(500, text="boom")

    with pytest.raises(YahooError):
        _client(handler).closes("AAPL")


def test_closes_falls_back_to_close():
    def handler(req):
        return httpx.Response(200, json=_chart([(_ts(20), 101.0)], adjclose=False))

    assert _client(handler).closes("AAPL") == [("2026-08-20", 101.0)]


def test_closes_skips_null_values():
    def handler(req):
        return httpx.Response(200, json=_chart([(_ts(19), None), (_ts(20), 101.0)]))

    assert _client(handler).closes("AAPL") == [("2026-08-20", 101.0)]


def test_collect_closes_partitions_errors():
    def handler(req):
        if "BAD" in req.url.path:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_chart([(_ts(20), 101.0)]))

    c = _client(handler)
    closes, errors = c.collect_closes(["AAPL", "BAD"], max_workers=1)
    assert set(closes) == {"AAPL"}
    assert "BAD" in errors
