"""Replay tests: reproduce scores/state from persisted rows (no network)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from risk_monitor.domain.models import Observation, ScoreSnapshot
from risk_monitor.domain.storage import create_database_engine, init_schema
from risk_monitor.replay import replay_score
from risk_monitor.scoring.policy import load_policy


def _now():
    return datetime.now(timezone.utc)


def _obs(metric_id, entity_id, value, as_of):
    return Observation(
        metric_id=metric_id, entity_id=entity_id, value=value, unit="percent",
        period_type="daily_close", as_of_date=as_of, retrieved_at=_now(),
        source_id="fred", extraction_method="derived", confidence="derived",
        status="active", definition_version="2026-08-21.1",
    )


def _seed(engine, as_of):
    rows = [
        # mbs
        _obs("market.spx_vs_200dma_pct", "SPX", 5.0, as_of),
        _obs("market.spx_pct_above_200dma", "BREADTH", 70.0, as_of),
        _obs("market.vix", "VIX", 16.0, as_of),
        _obs("market.breadth_20d_change", "BREADTH", 0.0, as_of),
        # css
        _obs("credit.hy_oas_pct", "HY_OAS", 2.75, as_of),
        _obs("credit.hy_oas_20d_change", "HY_OAS", -2.0, as_of),
        _obs("credit.bbb_oas_pct", "BBB_OAS", 1.0, as_of),
    ]
    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.add(ScoreSnapshot(
            as_of_date=as_of, mbs=0.0, css=0.0, afrs=None,
            component_json="{}", policy_version="2026-08-21.1",
            quality_status="ok", created_at=_now(),
        ))
        s.commit()


def test_replay_score_matches(tmp_path):
    policy = load_policy()
    engine = create_database_engine(tmp_path / "r.db")
    init_schema(engine)
    as_of = date(2026, 8, 20)
    _seed(engine, as_of)

    with Session(engine) as s:
        result = replay_score(s, policy, as_of)

    assert result["recomputed"]["mbs"] == 0.0
    assert result["recomputed"]["css"] == 0.0
    assert result["matches_mbs"] is True
    assert result["matches_css"] is True


def test_replay_score_mismatch_detected(tmp_path):
    policy = load_policy()
    engine = create_database_engine(tmp_path / "r2.db")
    init_schema(engine)
    as_of = date(2026, 8, 20)
    _seed(engine, as_of)
    # Corrupt the stored snapshot to force a mismatch.
    with Session(engine) as s:
        snap = s.query(ScoreSnapshot).filter_by(as_of_date=as_of).one()
        snap.mbs = 65.0
        s.commit()

    with Session(engine) as s:
        result = replay_score(s, policy, as_of)

    assert result["matches_mbs"] is False
    assert result["matches_css"] is True
