"""Engine helpers for the Agent API database.

The SQLite setup itself is shared with Finance MCP through
`personal_agent_core.sqlite`; only the metadata binding is service specific.
"""

from __future__ import annotations

from sqlalchemy import Engine

from personal_agent.storage.models import Base
from personal_agent_core.sqlite import (
    BUSY_TIMEOUT_MS,
    DatabaseIntegrityError,
    check_integrity,
    create_database_engine,
    session_factory,
)
from personal_agent_core.sqlite import create_all as _create_all


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DatabaseIntegrityError",
    "check_integrity",
    "create_all",
    "create_database_engine",
    "session_factory",
]


def create_all(engine: Engine) -> None:
    """Create the Agent API schema. Tests and fixtures only."""
    _create_all(engine, Base.metadata)
