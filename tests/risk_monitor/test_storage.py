"""Storage tests: schema init, seeding, append-only observation writes."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from risk_monitor.domain.models import Entity, MetricDefinition, Observation, Source
from risk_monitor.domain.storage import create_database_engine, init_schema, initialize, seed_metric_definitions
from risk_monitor.scoring.policy import load_policy


@pytest.fixture()
def policy():
    return load_policy()


@pytest.fixture()
def engine(tmp_path):
    eng = create_database_engine(tmp_path / "test.db")
    init_schema(eng)
    return eng


def test_initialize_seeds_entities_and_metrics(policy, tmp_path):
    eng = initialize(tmp_path / "test.db", policy)
    with Session(eng) as s:
        companies = s.scalars(select(Entity).where(Entity.entity_type == "company")).all()
        assert {c.entity_id for c in companies} == {"NVDA", "ORCL", "MSFT", "META", "AMZN", "GOOGL"}
        assert s.get(Source, "fred") is not None
        metrics = s.scalars(select(MetricDefinition)).all()
        assert any(m.metric_id == "market.vix" for m in metrics)


def test_observation_append_only(engine):
    with Session(engine) as s:
        s.add(Observation(
            metric_id="market.vix", entity_id="VIX", value=16.0, unit="index",
            period_type="daily_close", as_of_date=date(2026, 8, 20),
            retrieved_at=datetime.now(timezone.utc), source_id="fred",
            extraction_method="derived", confidence="derived", status="active",
            definition_version="2026-08-21.1",
        ))
        s.commit()
    # A revision is a NEW row, not an in-place update.
    with Session(engine) as s:
        rows = s.scalars(select(Observation).where(Observation.metric_id == "market.vix")).all()
        assert len(rows) == 1
        assert rows[0].value == 16.0


def test_metric_definitions_from_policy_are_versioned(policy, tmp_path):
    eng = create_database_engine(tmp_path / "m.db")
    init_schema(eng)
    with Session(eng) as s:
        seed_metric_definitions(s, policy)
        s.commit()
        m = s.get(MetricDefinition, "market.spx_vs_200dma_pct")
        assert m is not None
        assert m.definition_version == policy["definition_version"]
