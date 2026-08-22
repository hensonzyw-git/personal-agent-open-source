"""Tencent kline adapter tests (MockTransport; no network)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from risk_monitor.ingestion.tencent import TencentClient, TencentError


def _kline(rows, code="usAAPL.OQ"):
    # Tencent day row: [date, open, close, high, low, volume]; close = index 2.
    return {"data": {code: {"day": rows}}}


def _client(handler, codes=None):
    return TencentClient(
        codes or {"AAPL": "AAPL.OQ"},
        transport=httpx.MockTransport(handler),
    )


def test_closes_parses_raw_close_at_index_2():
    """The adapter reads the *raw* close (index 2), not any adjusted field."""

    def handler(req):
        assert req.url.params["param"].startswith("usAAPL.OQ,day")
        return httpx.Response(200, json=_kline([
            ["2026-08-19", "225.0", "226.0", "227.0", "224.0", "1000"],
            ["2026-08-20", "226.0", "227.0", "228.0", "225.0", "2000"],
        ]))

    assert _client(handler).closes("AAPL") == [
        ("2026-08-19", 226.0),
        ("2026-08-20", 227.0),
    ]


def test_closes_composes_us_prefix_and_dotted_code():
    """A hyphenated ticker (``BRK-B``) maps to Tencent's dotted canonical code
    (``BRK.B.N``) and is prefixed with ``us`` in the kline param."""

    def handler(req):
        assert req.url.params["param"].startswith("usBRK.B.N,day")
        return httpx.Response(200, json=_kline([
            ["2026-08-20", "495.0", "498.0", "500.0", "494.0", "1000"],
        ], code="usBRK.B.N"))

    client = TencentClient(
        {"BRK-B": "BRK.B.N"},
        transport=httpx.MockTransport(handler),
    )
    assert client.closes("BRK-B") == [("2026-08-20", 498.0)]


def test_closes_missing_canonical_code_raises():
    """A ticker absent from the frozen snapshot fails closed, not silently."""
    client = TencentClient(
        {"AAPL": "AAPL.OQ"},
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
    )
    with pytest.raises(TencentError):
        client.closes("UNKNOWN")


def test_closes_non_200_raises():
    def handler(req):
        return httpx.Response(500, text="boom")

    with pytest.raises(TencentError):
        _client(handler).closes("AAPL")


def test_closes_non_json_200_body_raises_tencent_error():
    """A 200 with a non-JSON body (HTML interstitial, truncated stream) is the
    same failure class as an HTTP error — a ``TencentError``, not a raw
    ``JSONDecodeError`` leaking out of ``closes``."""

    def handler(req):
        return httpx.Response(200, text="<html>gateway timeout</html>")

    with pytest.raises(TencentError):
        _client(handler).closes("AAPL")


def test_closes_skips_short_rows_and_nulls_unparseable_close():
    """A row shorter than 3 fields is dropped; an unparseable close becomes None
    rather than crashing the whole series."""

    def handler(req):
        return httpx.Response(200, json=_kline([
            ["2026-08-19"],  # too short -> skipped
            ["2026-08-20", "226.0", "bad", "228.0", "225.0", "2000"],  # close -> None
            ["2026-08-21", "227.0", "227.5", "228.0", "226.0", "3000"],
        ]))

    assert _client(handler).closes("AAPL") == [
        ("2026-08-20", None),
        ("2026-08-21", 227.5),
    ]


def test_collect_closes_partitions_errors():
    """A failing ticker lands in ``errors`` and is absent from ``closes`` —
    fail closed per name, not a zero series."""

    def handler(req):
        if "BAD" in req.url.params["param"]:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_kline([
            ["2026-08-20", "226.0", "227.0", "228.0", "225.0", "2000"],
        ]))

    client = _client(handler, {"AAPL": "AAPL.OQ", "BAD": "BAD.N"})
    closes, errors = client.collect_closes(["AAPL", "BAD"], max_workers=1)
    assert set(closes) == {"AAPL"}
    assert "BAD" in errors


def test_closes_parses_real_tencent_fixture():
    """The adapter parses a REAL Tencent kline body — including the dividend row
    that carries a 7th dict element — reading close from index 2 and date from
    index 0, not from a fake written from the code's own assumptions (§5.1)."""
    fixture_path = Path(__file__).parent / "fixtures" / "tencent_usAAPL_OQ_day.json"
    fixture = json.loads(fixture_path.read_text())
    rows = fixture["data"]["usAAPL.OQ"]["day"]
    expected = [(r[0], float(r[2])) for r in rows]

    def handler(req):
        assert req.url.params["param"].startswith("usAAPL.OQ,day")
        return httpx.Response(200, json=fixture)

    client = TencentClient({"AAPL": "AAPL.OQ"}, transport=httpx.MockTransport(handler))
    assert client.closes("AAPL") == expected
    assert len(expected) == 10
    # The fixture is only a real counterexample if at least one row carries the
    # extra dividend-metadata element the fake never had.
    assert any(len(r) > 6 for r in rows)
