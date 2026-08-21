"""SQLite engine + schema init + seeding for the risk monitor.

Separate DB from Personal Agent's ledger (different product, different
lifecycle). Single-writer, append-only; no compare-and-swap is needed here, so
this deliberately does not reuse the transaction-semantics machinery from
``personal_agent_core.sqlite`` — those rules exist for concurrent read-then-write
paths this monitor does not have.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    COMPANY_ENTITIES,
    MARKET_ENTITIES,
    SOURCES,
    Base,
    Entity,
    MetricDefinition,
    Source,
)

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "risk_monitor.db"


def create_database_engine(path: str | Path | None = None) -> Engine:
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def _seed_entities(session: Session) -> None:
    rows = [Entity(entity_type="company", **e) for e in COMPANY_ENTITIES]
    rows += [Entity(**e) for e in MARKET_ENTITIES]
    for r in rows:
        session.merge(r)
    for s in SOURCES:
        session.merge(Source(**s))


def seed_metric_definitions(session: Session, policy: dict) -> None:
    """Register metric definitions from the frozen policy (single source of
    truth for metric_id / unit / band semantics)."""
    version = policy["definition_version"]
    for score_key in ("mbs", "css"):
        for ind in policy[score_key]["indicators"]:
            session.merge(MetricDefinition(
                metric_id=ind["metric_id"],
                name=ind["metric_id"],
                unit=ind.get("unit", "mixed"),
                definition=str(ind.get("note", "")),
                definition_version=version,
            ))
    for ind in policy["afrs"]["company_indicators"]:
        session.merge(MetricDefinition(
            metric_id=ind["metric_id"],
            name=ind["metric_id"],
            unit=ind.get("unit", "mixed"),
            definition=str(ind.get("note", "")),
            definition_version=version,
        ))


def initialize(path: str | Path | None = None, policy: dict | None = None) -> Engine:
    """Create the DB, schema, and seed data. Idempotent."""
    engine = create_database_engine(path)
    init_schema(engine)
    with Session(engine) as session:
        _seed_entities(session)
        if policy is not None:
            seed_metric_definitions(session, policy)
        session.commit()
    return engine


def session_scope(engine: Engine) -> Iterator[Session]:
    """Context manager yielding a committed session."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
