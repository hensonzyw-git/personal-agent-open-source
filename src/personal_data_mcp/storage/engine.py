"""Engine helpers for the Finance MCP database.

The SQLite setup is shared with the Agent API through
`personal_agent_core.sqlite`; only the metadata binding is service specific. The
two databases stay in separate files under separate service users, and neither
service may read the other's.
"""

from __future__ import annotations

from sqlalchemy import Engine

from personal_agent_core.sqlite import (
    BUSY_TIMEOUT_MS,
    DatabaseIntegrityError,
    check_integrity,
    create_database_engine,
    create_read_only_database_engine,
    session_factory,
)
from personal_agent_core.sqlite import create_all as _create_all
from personal_data_mcp.storage.models import Base


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
    """Create the Finance MCP schema. Tests and fixtures only."""
    _create_all(engine, Base.metadata)
