"""SQLite engine setup with the PRAGMAs the design depends on.

Two of these are not defaults and are easy to lose. `foreign_keys` is OFF in
SQLite unless set per connection, so declared foreign keys are decoration until
it is enabled. `journal_mode=WAL` is persistent in the file but is set here so a
freshly created database gets it without a manual step.

`integrity_check` runs at startup, per technical design 8.2. If audit or
idempotency state cannot be trusted, write tools must fail closed rather than
carry on, so the check raises rather than warns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from personal_agent.storage.models import Base


BUSY_TIMEOUT_MS: Final[int] = 5_000


class DatabaseIntegrityError(RuntimeError):
    """The database failed `PRAGMA integrity_check`."""


def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=FULL")
    finally:
        cursor.close()


def create_database_engine(path: Path | str, *, echo: bool = False) -> Engine:
    """Build an engine with the required PRAGMAs applied to every connection."""
    engine = create_engine(f"sqlite+pysqlite:///{path}", echo=echo, future=True)
    event.listen(engine, "connect", _configure_connection)
    return engine


def check_integrity(engine: Engine) -> None:
    """Raise unless SQLite reports the database as intact."""
    with engine.connect() as connection:
        result = connection.execute(text("PRAGMA integrity_check")).scalar_one()
    if result != "ok":
        raise DatabaseIntegrityError(
            f"integrity_check did not return ok: {result!r}"
        )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    """Create the schema directly.

    For tests and throwaway fixtures only. Deployed databases are built and
    moved forward by Alembic so that every change has a reviewable, reversible
    migration.
    """
    Base.metadata.create_all(engine)
