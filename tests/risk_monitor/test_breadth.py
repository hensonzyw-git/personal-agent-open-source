"""Breadth-computation tests (pure, no network)."""

from __future__ import annotations

import pytest

from risk_monitor import breadth as b


def _series(values):
    # Dates must not collide across ticks that share a length; length is the
    # only thing that matters here since the date axis is the union.
    return [(f"2026-01-{i+1:02d}", v) for i, v in enumerate(values)]


def _flat(n, value):
    return _series([value] * n)


def test_rolling_above_empty_when_short():
    assert b._rolling_above(_flat(199, 100.0)) == []


def test_rolling_above_last_day_above():
    flags = b._rolling_above(_series([100.0] * 200 + [110.0]))
    assert flags[-1][1] is True  # 110 > (199*100+110)/200 = 100.05


def test_rolling_above_flat_is_not_above():
    flags = b._rolling_above(_flat(201, 100.0))
    assert flags[-1][1] is False  # 100 > 100 is False


def test_breadth_series_two_tickers_fifty_pct():
    up = _series([100.0] * 200 + [110.0])
    down = _flat(201, 100.0)
    bs = b.breadth_series({"UP": up, "DOWN": down})
    # Last common signal day: UP above, DOWN not -> 50%.
    assert bs[-1][1] == 50.0


def test_breadth_series_skips_short_ticker():
    up = _series([100.0] * 200 + [110.0])
    down = _flat(201, 100.0)
    short = _flat(100, 50.0)  # <200 closes -> no signal, excluded from denominator
    bs = b.breadth_series({"UP": up, "DOWN": down, "SHORT": short})
    assert bs[-1][1] == 50.0


def test_breadth_today():
    up = _series([100.0] * 200 + [110.0])
    down = _flat(201, 100.0)
    d, pct = b.breadth_today({"UP": up, "DOWN": down})
    assert pct == 50.0
    assert d == "2026-01-201"


def test_breadth_change_flat_is_zero():
    # 200 x 100 then 25 x 110: above for all 25 signal days on the up ticker,
    # the down ticker never above -> constant 50% breadth -> 0 change.
    up = _series([100.0] * 200 + [110.0] * 25)
    down = _flat(225, 100.0)
    assert b.breadth_change({"UP": up, "DOWN": down}, days=20) == 0.0


def test_breadth_change_none_when_short():
    up = _series([100.0] * 200 + [110.0])
    down = _flat(201, 100.0)
    # Only one signal day -> fewer than days+1 -> None.
    assert b.breadth_change({"UP": up, "DOWN": down}, days=20) is None


def test_coverage_fraction():
    up = _series([100.0] * 200 + [110.0])
    short = _flat(100, 50.0)
    assert b.coverage({"UP": up, "SHORT": short}) == pytest.approx(0.5)
    assert b.coverage({}) == 0.0
