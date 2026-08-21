"""SQLAlchemy models + seed constants.

Single-writer, append-only store (ADR-0001: SQLite, not PostgreSQL). ``raw_``
and ``observation`` rows are never updated in place — a correction is a new row
with the same (metric_id, entity_id, as_of_date) and a superseded marker.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Entity(Base):
    __tablename__ = "entities"

    entity_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    ticker: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    cik: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # SEC CIK, zero-padded 10
    entity_type: Mapped[str] = mapped_column(String(16))  # company | index | market
    active_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    active_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)


class Source(Base):
    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(80))
    base_url: Mapped[str] = mapped_column(String(240))
    reliability_tier: Mapped[str] = mapped_column(String(16))  # primary | proxy


class RawArtifact(Base):
    __tablename__ = "raw_artifacts"

    raw_record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(32))
    uri: Mapped[str] = mapped_column(String(500))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)
    content_sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # stored raw body (JSON) for replay


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"

    metric_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    unit: Mapped[str] = mapped_column(String(32))
    definition: Mapped[str] = mapped_column(Text)
    definition_version: Mapped[str] = mapped_column(String(32))


class Observation(Base):
    __tablename__ = "observations"

    observation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_id: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # None = not_disclosed
    unit: Mapped[str] = mapped_column(String(32))
    period_type: Mapped[str] = mapped_column(String(16))  # daily_close | quarter | ttm | instant
    period_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    period_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    filing_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)
    source_id: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    raw_record_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    extraction_method: Mapped[str] = mapped_column(String(16))  # vendor_api | xbrl | manual | parser | derived
    confidence: Mapped[str] = mapped_column(String(16))  # reported | derived | proxy | not_disclosed
    status: Mapped[str] = mapped_column(String(16))  # active | superseded
    definition_version: Mapped[str] = mapped_column(String(32))


class ScoreSnapshot(Base):
    __tablename__ = "score_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    mbs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    css: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    afrs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    component_json: Mapped[str] = mapped_column(Text)  # per-indicator results, for explain/replay
    policy_version: Mapped[str] = mapped_column(String(32))
    quality_status: Mapped[str] = mapped_column(String(24))  # ok | data_quality_warning
    created_at: Mapped[datetime] = mapped_column(DateTime)


class StateSnapshot(Base):
    __tablename__ = "state_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    state: Mapped[str] = mapped_column(String(24))
    transition_reason: Mapped[str] = mapped_column(Text)
    confirmations: Mapped[int] = mapped_column(Integer)  # consecutive days at this state
    policy_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# Seed constants
# ---------------------------------------------------------------------------

# The six AI-capex-cycle companies (PRD §3.1). CIKs are the public SEC Central
# Index Keys; they are re-verified against EDGAR when the Phase 3 adapter lands.
COMPANY_ENTITIES: list[dict] = [
    {"entity_id": "NVDA",  "name": "NVIDIA Corporation",            "ticker": "NVDA",  "cik": "0001045810"},
    {"entity_id": "ORCL",  "name": "Oracle Corporation",            "ticker": "ORCL",  "cik": "0001341439"},
    {"entity_id": "MSFT",  "name": "Microsoft Corporation",         "ticker": "MSFT",  "cik": "0000789019"},
    {"entity_id": "META",  "name": "Meta Platforms, Inc.",          "ticker": "META",  "cik": "0001326801"},
    {"entity_id": "AMZN",  "name": "Amazon.com, Inc.",              "ticker": "AMZN",  "cik": "0001018724"},
    {"entity_id": "GOOGL", "name": "Alphabet Inc.",                 "ticker": "GOOGL", "cik": "0001652044"},
]

MARKET_ENTITIES: list[dict] = [
    {"entity_id": "SPX",       "name": "S&P 500",                          "entity_type": "index"},
    {"entity_id": "VIX",       "name": "Cboe Volatility Index",            "entity_type": "index"},
    {"entity_id": "HY_OAS",    "name": "US High Yield OAS (ICE BofA)",     "entity_type": "market"},
    {"entity_id": "BBB_OAS",   "name": "US BBB OAS (ICE BofA)",            "entity_type": "market"},
    {"entity_id": "DGS10",     "name": "10-Year Treasury Yield",           "entity_type": "market"},
    {"entity_id": "DGS30",     "name": "30-Year Treasury Yield",           "entity_type": "market"},
    {"entity_id": "BREADTH",   "name": "S&P 500 breadth (% above 200dma)", "entity_type": "market"},
]

SOURCES: list[dict] = [
    {"source_id": "fred",       "provider": "FRED (St. Louis Fed)",     "base_url": "https://api.stlouisfed.org/fred", "reliability_tier": "primary"},
    {"source_id": "sec_edgar",  "provider": "SEC EDGAR",                "base_url": "https://data.sec.gov",            "reliability_tier": "primary"},
]
