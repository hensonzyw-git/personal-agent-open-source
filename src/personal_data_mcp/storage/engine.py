"""Engine helpers for the Finance MCP database.

The SQLite setup is shared with the Agent API through
`personal_agent_core.sqlite`; only the metadata binding is service specific. The
two databases stay in separate files under separate service users, and neither
service may read the other's.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from personal_agent_core.sqlite import (
    BUSY_TIMEOUT_MS,
    DatabaseIntegrityError,
    check_integrity,
    create_database_engine,
    create_read_only_database_engine,
    session_factory,
)
from personal_agent_core.sqlite import create_all as _create_all
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
    """Create the Finance MCP schema, and the row that is part of it.

    Tests and fixtures only. The calendar ingest policy row is **schema rather
    than data** -- migration 0009 inserts it in the same statement-sequence as
    the table, and `calendar.policy.read_policy` refuses every calendar upload
    when it is missing (that refusal is the barrier's only fail-*closed*
    direction: a defaulted row would read as `min_ingest_protocol=1` and let an
    old App's late packets into a mirror that was rebuilt without them).

    So a schema built here has to carry it too. Without this, every path that
    bootstraps a database through `create_all` -- this fixture surface and the
    Finance write CLI's startup -- produces a shape the migration never
    produces, where calendar ingest fails closed at 500 with nothing to point
    at. One insert against one row, and the two schema paths agree.
    """
    _create_all(engine, Base.metadata)
    with Session(engine) as session, session.begin():
        if session.get(CalendarIngestPolicy, policy.POLICY_ID) is None:
            session.add(
                CalendarIngestPolicy(
                    policy_id=policy.POLICY_ID,
                    min_ingest_protocol=policy.PROTOCOL_BEFORE_REBUILD,
                    ingest_mode=policy.MODE_NORMAL,
                    updated_at=datetime.now(tz=timezone.utc),
                )
            )
