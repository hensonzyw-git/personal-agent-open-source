"""Daily-pipeline wiring tests (no network; derive/collect helpers only)."""

from __future__ import annotations

from risk_monitor.daily import collect_breadth, derive_market_values


def _series(values, start_day=1):
    return [(f"2026-01-{i+1:02d}", v) for i, v in enumerate(values)]


def test_derive_market_values():
    raw = {
        "spx_history": _series([100.0] * 200 + [110.0]),
        "hy_oas_history": _series([1.0] * 30 + [1.5]),
        "bbb_oas_latest": ("2026-08-20", 1.0),
        "dgs10_latest": ("2026-08-20", 4.65),
        "dgs30_latest": ("2026-08-20", 5.19),
        "vix_latest": ("2026-08-20", 16.0),
    }
    mbs, css, as_of = derive_market_values(raw)
    assert mbs["market.spx_vs_200dma_pct"] == 9.945
    assert mbs["market.vix"] == 16.0
    assert css["credit.hy_oas_pct"] == 1.5
    assert css["credit.hy_oas_20d_change"] == 50.0  # (1.5 - 1.0) * 100 bp
    assert css["credit.bbb_oas_pct"] == 1.0
    assert as_of == "2026-01-201"


class _StubYahoo:
    def __init__(self, closes):
        self._closes = closes

    def collect_closes(self, tickers):
        return self._closes, {}


def test_collect_breadth_metric_keys():
    up = _series([100.0] * 200 + [110.0])
    down = _series([100.0] * 201)
    stub = _StubYahoo({"UP": up, "DOWN": down})
    metrics, meta, bs = collect_breadth(stub, ["UP", "DOWN"])  # type: ignore[arg-type]
    assert metrics["market.spx_pct_above_200dma"] == 50.0
    assert "market.breadth_20d_change" not in metrics  # only one signal day -> None
    assert meta["coverage"] == 1.0
    assert meta["tickers_ok"] == 2


def test_collect_breadth_empty_when_no_signal():
    stub = _StubYahoo({"SHORT": _series([100.0] * 100)})
    metrics, meta, bs = collect_breadth(stub, ["SHORT"])  # type: ignore[arg-type]
    assert metrics == {}
    assert meta["coverage"] == 0.0
    assert bs == []
