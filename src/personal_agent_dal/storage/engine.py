"""Engine helpers for the DAL workflow database.

The SQLite setup is shared with the other services through
`personal_agent_core.sqlite` — the same PRAGMAs, the same real-transaction
control, the same `run_write_transaction` retry discipline — because two
services must not disagree about what a transaction is. Only the metadata
binding is DAL specific.
"""

from __future__ import annotations

from sqlalchemy import Engine

from personal_agent_dal.storage import machine_models  # noqa: F401 - registers tables
from personal_agent_dal.storage.models import Base
from personal_agent_core.sqlite import (
    BUSY_TIMEOUT_MS,
    DatabaseIntegrityError,
    check_integrity,
    create_database_engine,
    create_read_only_database_engine,
    session_factory,
)
from personal_agent_core.sqlite import create_all as _create_all


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
    """Create the DAL schema directly. Tests and fixtures only.

    Deployed databases are built and moved forward by Alembic so every change
    has a reviewable, reversible migration; `create_all` is never the
    production path.
    """
    _create_all(engine, Base.metadata)
