"""Alembic environment for the DAL workflow database.

The database URL is supplied by the caller through `config.attributes`, never
read from an ini file, so a migration can never be pointed at the wrong service
database by an environment that happens to be lying around.

Foreign keys are switched **off** for the duration of a migration, and the
integrity they express is re-checked before the transaction commits. This is
the same discipline the other services use, and the reason is unchanged: a
SQLite batch migration recreates a table, and dropping a parent table is an
implicit `DELETE` of its rows, which with `ON DELETE CASCADE` silently deletes
the children too. `PRAGMA foreign_key_check` inside the transaction is what
makes the guarantee real -- any dangling reference aborts and rolls the whole
migration back.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Engine

from personal_agent_dal.storage import machine_models  # noqa: F401 - registers tables
from personal_agent_dal.storage import timeline_models  # noqa: F401 - Timeline tables
from personal_agent_dal.storage.models import Base


target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection")
    if not isinstance(connectable, Engine):
        raise RuntimeError(
            "personal_agent_dal migrations require an Engine in "
            "config.attributes['connection']; use personal-agent-dal-db"
        )

    with connectable.connect() as connection:
        # SQLite ignores `PRAGMA foreign_keys` inside a transaction, silently.
        # Driving the DBAPI cursor directly keeps it outside a transaction, and
        # wraps all revisions and the integrity check in one explicit transaction.
        # Restoring the pragma must happen after that transaction ends.
        _pragma(connection, "foreign_keys=OFF")
        try:
            with connection.begin():
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    render_as_batch=True,
                    compare_type=True,
                )
                with context.begin_transaction():
                    context.run_migrations()
                    violations = connection.exec_driver_sql(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    if violations:
                        # Raised inside the transaction, so this rolls the whole
                        # migration back rather than committing a broken schema.
                        raise RuntimeError(
                            f"migration left {len(violations)} foreign key "
                            "violation(s); rolling back"
                        )
        finally:
            _pragma(connection, "foreign_keys=ON")


def _pragma(connection, statement: str) -> None:
    """Apply a PRAGMA outside any transaction, through the DBAPI cursor."""
    cursor = connection.connection.cursor()
    try:
        cursor.execute(f"PRAGMA {statement}")
    finally:
        cursor.close()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported for this service")
run_migrations_online()
