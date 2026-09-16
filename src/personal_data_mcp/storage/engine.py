"""Engine helpers for the Finance MCP database.

The SQLite setup is shared with the Agent API through
`personal_agent_core.sqlite`; only the metadata binding is service specific. The
two databases stay in separate files under separate service users, and neither
service may read the other's.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Engine, inspect, select

from personal_agent_core.sqlite import (
    BUSY_TIMEOUT_MS,
    DatabaseIntegrityError,
    check_integrity,
    create_database_engine,
    create_read_only_database_engine,
    session_factory,
)
from personal_data_mcp.calendar import policy
from personal_data_mcp.storage.models import Base, CalendarIngestPolicy


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DatabaseIntegrityError",
    "check_integrity",
    "create_all",
    "create_database_engine",
    "create_read_only_database_engine",
    "session_factory",
]


def create_all(engine: Engine) -> None:
    """Bootstrap an empty database without repairing a lost ingest barrier.

    Used by fixtures and the Finance write CLI. Only an empty database may
    receive the initial permissive policy. An existing database without the
    table or singleton must fail closed; deployments upgrade through Alembic.
    Inspection, DDL and the initial row share one real SQLite transaction, so
    neither a partial bootstrap nor a concurrent writer can reset the floor.
    """
    with engine.begin() as connection:
        tables = inspect(connection).get_table_names()
        if tables:
            if CalendarIngestPolicy.__tablename__ not in tables:
                raise DatabaseIntegrityError(
                    "existing database has no calendar ingest policy table; "
                    "create_all cannot migrate or repair its barrier"
                )
            existing = connection.execute(
                select(CalendarIngestPolicy.policy_id).where(
                    CalendarIngestPolicy.policy_id == policy.POLICY_ID
                )
            ).scalar_one_or_none()
            if existing is None:
                raise DatabaseIntegrityError(
                    "existing database has no calendar ingest policy row; "
                    "refusing to reset its barrier"
                )
        Base.metadata.create_all(connection)
        if not tables:
            connection.execute(
                CalendarIngestPolicy.__table__.insert().values(
                    policy_id=policy.POLICY_ID,
                    min_ingest_protocol=policy.PROTOCOL_BEFORE_REBUILD,
                    ingest_mode=policy.MODE_NORMAL,
                    updated_at=datetime.now(tz=timezone.utc),
                )
            )
