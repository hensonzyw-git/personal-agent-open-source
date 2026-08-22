"""Daily-pipeline wiring tests (no network; derive/collect helpers only)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from risk_monitor import daily as daily_mod
from risk_monitor.daily import (
    collect_ai_basket,
    collect_breadth,
    collect_fred,
    derive_market_values,
    recompute_state,
    run,
)
from risk_monitor.domain.models import Observation, ScoreSnapshot
from risk_monitor.domain.storage import create_database_engine, init_schema
from risk_monitor.ingestion.fred import FRED_SERIES
from risk_monitor.replay import replay_score
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
    assert as_of == "2026-08-20"  # latest date across ALL series, not just SPX


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
    result, errors = collect_ai_basket(stub)  # type: ignore[arg-type]
    assert set(result) == {"NVDA", "ORCL", "MSFT", "META", "AMZN", "GOOGL"}
    assert result["NVDA"] is up
    assert errors == {}


def test_collect_ai_basket_omits_failed_names():
    """A name Yahoo fails on is dropped (fail-closed per name), not zero."""
    up = _series([100.0] * 200 + [110.0])
    stub = _StubYahoo({"NVDA": up, "ORCL": up})  # 4 names absent
    result, errors = collect_ai_basket(stub)  # type: ignore[arg-type]
    assert set(result) == {"NVDA", "ORCL"}
    assert errors == {}


def test_collect_ai_basket_surfaces_failed_names():
    """A Yahoo failure on one name is surfaced as provenance, not swallowed —
    so audit can distinguish 'failed pull' from 'no 200dma signal yet'."""
    up = _series([100.0] * 200 + [110.0])

    class _ErroringYahoo(_StubYahoo):
        def collect_closes(self, tickers):
            closes, _ = super().collect_closes(tickers)
            closes.pop("META", None)  # a failed name is absent from closes
            return closes, {"META": "Yahoo META: HTTP 429"}

    stub = _ErroringYahoo({name: up for name in ("NVDA", "ORCL", "MSFT", "META", "AMZN", "GOOGL")})
    result, errors = collect_ai_basket(stub)  # type: ignore[arg-type]
    assert "META" in errors
    assert set(result) == {"NVDA", "ORCL", "MSFT", "AMZN", "GOOGL"}


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
    # SPX is down but HY/Vix/10Y still have a real date; as_of must be that
    # date, never a fabricated date.today() fallback.
    assert as_of == "2026-08-20"


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


# ---------------------------------------------------------------------------
# Full-chain composition test: drive daily.run() with stubbed clients and verify
# the production wiring (FRED + breadth + ai_basket + term_financing + EDGAR)
# really composes, persists the qualitative labels, and replays back to the
# identical scores (§5.1 "production composition 必须真实存在").
# ---------------------------------------------------------------------------

def _series_end(end: date, n: int, value: float) -> list[tuple[str, float]]:
    start = end - timedelta(days=n - 1)
    return [( (start + timedelta(days=i)).isoformat(), value) for i in range(n)]


def _ramp_end(end: date, n: int, first: float, last: float) -> list[tuple[str, float]]:
    start = end - timedelta(days=n - 1)
    step = (last - first) / (n - 1)
    return [( (start + timedelta(days=i)).isoformat(), round(first + step * i, 6)) for i in range(n)]


def _companyfacts():
    """Minimal healthy companyfacts JSON (same shape as the AFRS tests)."""

    def fact(start, end, val):
        return {"start": start, "end": end, "val": val, "form": "10-K", "fp": "FY"}

    usgaap = {
        "PaymentsToAcquireProductiveAssets": {"units": {"USD": [
            fact("2024-01-29", "2025-01-26", 30_000_000_000),
            fact("2023-01-30", "2024-01-28", 10_000_000_000),
        ]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            fact("2024-01-29", "2025-01-26", 60_000_000_000),
            fact("2023-01-30", "2024-01-28", 50_000_000_000),
        ]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            fact("2024-01-29", "2025-01-26", 100_000_000_000),
            fact("2023-01-30", "2024-01-28", 100_000_000_000),
        ]}},
        "AccountsReceivableNetCurrent": {"units": {"USD": [
            {"end": "2025-01-26", "val": 11_000_000_000},
            {"end": "2024-01-28", "val": 10_000_000_000},
        ]}},
    }
    return {"facts": {"us-gaap": usgaap}}


class _RunFred:
    base_url = "https://api.stlouisfed.org/fred"

    def __init__(self):
        end = date(2026, 8, 20)
        self._series = {
            FRED_SERIES["market.spx_close"]: _ramp_end(end, 220, 100.0, 110.0),
            FRED_SERIES["credit.hy_oas_pct"]: _ramp_end(end, 40, 4.4, 5.0),
            FRED_SERIES["treasury.30y_yield"]: _ramp_end(end, 40, 4.0, 4.01),
        }

    def history(self, series_id):
        return self._series.get(series_id, [])

    def latest(self, series_id):
        return {
            FRED_SERIES["credit.bbb_oas_pct"]: ("2026-08-20", 1.75),
            FRED_SERIES["treasury.10y_yield"]: ("2026-08-20", 4.0),
            FRED_SERIES["market.vix"]: ("2026-08-20", 16.0),
        }.get(series_id, ("2026-08-20", 1.0))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RunYahoo:
    base_url = "https://query1.finance.yahoo.com/v8/finance/chart"

    def __init__(self):
        self._end = date(2026, 8, 20)

    def collect_closes(self, tickers):
        closes = {}
        for t in tickers:
            series = _series_end(self._end, 220, 100.0)
            series[-1] = (series[-1][0], 101.0)  # last close above 200dma -> green
            closes[t] = series
        return closes, {}


class _RunEdgar:
    def company_facts(self, cik):
        return _companyfacts()


def test_run_composition_and_replay_roundtrip(tmp_path, monkeypatch):
    """The full daily.run() pipeline composed from stub clients must produce all
    three scores and a deterministic action, persist the qualitative proxy
    labels in ``value_text`` (not drop them), and replay the same day back to
    the identical scores."""
    monkeypatch.setattr(daily_mod, "FredClient", _RunFred)
    monkeypatch.setattr(daily_mod, "YahooClient", _RunYahoo)
    monkeypatch.setattr(daily_mod, "EdgarClient", _RunEdgar)
    monkeypatch.setattr(daily_mod, "load_tickers", lambda: ["UP", "DOWN", "UP2"])

    result = run(db_path=str(tmp_path / "run.db"))

    policy = load_policy()
    assert result["as_of"] == "2026-08-20"
    assert result["mbs"] is not None
    assert result["css"] is not None
    assert result["afrs"] is not None
    assert result["state"] in ("NORMAL", "RISK_ACCUMULATION", "CREDIT_CONFIRMATION", "DELEVERAGING")
    assert result["action"] == policy["actions"][result["state"]]
    assert result["fred_failures"] == {}

    # The qualitative proxy labels were persisted in value_text, not dropped.
    engine = create_database_engine(tmp_path / "run.db")
    with Session(engine) as s:
        labels = {
            o.metric_id: o.value_text
            for o in s.scalars(select(Observation).where(
                Observation.metric_id.in_(["credit.ai_basket", "credit.term_financing"])))
        }
        assert labels["credit.ai_basket"] == "green"
        assert labels["credit.term_financing"] == "green"
        snapshot = s.scalars(select(ScoreSnapshot)).one()
        assert snapshot.quality_status == "ok"

    # Replay reproduces the identical scores from persisted observations.
    with Session(engine) as s:
        r = replay_score(s, policy, date.fromisoformat(result["as_of"]))
    assert r["matches_mbs"] is True
    assert r["matches_css"] is True
