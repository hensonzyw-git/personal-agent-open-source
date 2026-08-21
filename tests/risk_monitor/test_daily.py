"""Daily-pipeline wiring tests (no network; derive/collect helpers only)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from risk_monitor.daily import (
    collect_ai_basket,
    collect_breadth,
    collect_fred,
    derive_market_values,
    recompute_state,
)
from risk_monitor.domain.models import ScoreSnapshot
from risk_monitor.domain.storage import create_database_engine, init_schema
from risk_monitor.ingestion.fred import FRED_SERIES
from risk_monitor.scoring.policy import load_policy


def _series(values, start_day=1):
    return [(f"2026-01-{i+1:02d}", v) for i, v in enumerate(values)]


def test_derive_market_values():
    raw = {
        "spx_history": _series([100.0] * 200 + [110.0]),
        "hy_oas_history": _series([1.0] * 30 + [1.5]),
        "bbb_oas_latest": ("2026-08-20", 1.0),
        "dgs10_latest": ("2026-08-20", 4.65),
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


def test_collect_ai_basket_maps_ticker_to_entity():
    """The six AI names are collected and keyed by entity_id, not ticker."""
    up = _series([100.0] * 200 + [110.0])
    closes = {name: up for name in ("NVDA", "ORCL", "MSFT", "META", "AMZN", "GOOGL")}
    stub = _StubYahoo(closes)
    result = collect_ai_basket(stub)  # type: ignore[arg-type]
    assert set(result) == {"NVDA", "ORCL", "MSFT", "META", "AMZN", "GOOGL"}
    assert result["NVDA"] is up


def test_collect_ai_basket_omits_failed_names():
    """A name Yahoo fails on is dropped (fail-closed per name), not zero."""
    up = _series([100.0] * 200 + [110.0])
    stub = _StubYahoo({"NVDA": up, "ORCL": up})  # 4 names absent
    result = collect_ai_basket(stub)  # type: ignore[arg-type]
    assert set(result) == {"NVDA", "ORCL"}


def test_collect_breadth_drops_metrics_below_pull_coverage_threshold():
    """A partial Yahoo pull (only 2 of 100 names) must not produce breadth from
    the subset — the two MBS indicators become unavailable instead."""
    up = _series([100.0] * 200 + [110.0])
    down = _series([100.0] * 201)
    stub = _StubYahoo({"UP": up, "DOWN": down})
    tickers = ["UP", "DOWN"] + [f"T{i}" for i in range(98)]
    metrics, meta, bs = collect_breadth(stub, tickers)  # type: ignore[arg-type]
    assert metrics == {}
    assert meta["below_threshold"] is True
    assert meta["pull_coverage"] < 0.8


class _FlakyFred:
    def history(self, series_id):
        if series_id == FRED_SERIES["credit.hy_oas_pct"]:
            raise RuntimeError("hy oas down")
        return _series([100.0] * 200 + [110.0])

    def latest(self, series_id):
        if series_id == FRED_SERIES["market.vix"]:
            raise RuntimeError("vix down")
        return ("2026-08-20", 1.0)


def test_collect_fred_isolates_series_failures():
    """A failing series degrades to ``None`` + a failure entry, never crashes
    the run (ADR-0001 fail-closed)."""
    raw, failures = collect_fred(_FlakyFred())  # type: ignore[arg-type]
    assert raw["spx_history"] is not None
    assert raw["hy_oas_history"] is None
    assert raw["vix_latest"] is None
    assert raw["bbb_oas_latest"] == ("2026-08-20", 1.0)
    assert failures["credit.hy_oas_pct"] == "RuntimeError"
    assert failures["market.vix"] == "RuntimeError"


def test_derive_market_values_with_failed_series():
    """A failed series yields no value (metric omitted), never a crash or a
    fabricated number."""
    raw = {
        "spx_history": None,
        "hy_oas_history": _series([1.0] * 30 + [1.5]),
        "bbb_oas_latest": None,
        "dgs10_latest": ("2026-08-20", 4.65),
        "vix_latest": ("2026-08-20", 16.0),
    }
    mbs, css, as_of = derive_market_values(raw)
    assert "market.spx_vs_200dma_pct" not in mbs
    assert mbs["market.vix"] == 16.0
    assert css["credit.hy_oas_pct"] == 1.5
    assert "credit.bbb_oas_pct" not in css
    assert as_of == date.today().isoformat()


def _snapshot(as_of, state):
    return ScoreSnapshot(
        as_of_date=as_of,
        mbs=30.0,
        css=40.0,
        afrs=60.0,
        component_json=json.dumps({"indication": {"state": state, "reasons": ["afrs>=55"]}}),
        policy_version="2026-08-21.1",
        quality_status="ok",
        created_at=datetime.now(timezone.utc),
    )


def test_same_day_reruns_do_not_advance_state(tmp_path):
    """N snapshots written on the SAME day must count as one day of
    confirmation, not N — otherwise a same-day re-run bypasses the 5-day
    upgrade gate."""
    policy = load_policy()
    engine = create_database_engine(tmp_path / "risk.db")
    init_schema(engine)
    with Session(engine) as s:
        for _ in range(5):
            s.add(_snapshot(date(2026, 8, 20), "RISK_ACCUMULATION"))
        s.commit()

    state, _reasons, _conf = recompute_state(engine, policy)
    assert state == "NORMAL"  # one distinct day, not five


def test_five_distinct_days_advance_state(tmp_path):
    """Positive control: five distinct days at RISK_ACCUMULATION do upgrade."""
    policy = load_policy()
    engine = create_database_engine(tmp_path / "risk.db")
    init_schema(engine)
    with Session(engine) as s:
        for i in range(5):
            s.add(_snapshot(date(2026, 8, 20 - i), "RISK_ACCUMULATION"))
        s.commit()

    state, _reasons, _conf = recompute_state(engine, policy)
    assert state == "RISK_ACCUMULATION"
