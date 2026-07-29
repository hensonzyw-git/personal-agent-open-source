"""Alembic environment for the Agent API database.

The database URL is supplied by the caller through `config.attributes`, never
read from an ini file, so a migration can never be pointed at the wrong service
database by an environment that happens to be lying around.

Foreign keys are switched **off** for the duration of a migration, and the
integrity they express is re-checked before the transaction commits. This is not
a relaxation; it is the only correct setting on SQLite. Batch migrations work by
recreating a table -- create a copy, move the rows, `DROP` the original, rename
-- and SQLite treats dropping a parent table as an implicit `DELETE` of every
row in it, which with `ON DELETE CASCADE` silently deletes the children too.
`CAP-001` found this the expensive way: adding a check constraint to
`conversations` deleted the entire conversation archive, and every assertion
still passed because the migration then verified a Timeline that was correctly
empty. `PRAGMA foreign_key_check` inside the transaction is what makes the
guarantee real -- any dangling reference aborts and rolls the whole migration
back.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Engine

from personal_agent.storage.models import Base


target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection")
    if not isinstance(connectable, Engine):
        raise RuntimeError(
            "personal_agent migrations require an Engine in "
            "config.attributes['connection']; use personal-agent-db"
        )

    with connectable.connect() as connection:
        # SQLite ignores `PRAGMA foreign_keys` inside a transaction, silently.
        # The engine hands transaction control to SQLAlchemy so that
        # `begin_nested()` is a real savepoint, which means any statement
        # executed through `connection` opens a transaction first -- including
        # this PRAGMA, which would then be a no-op and leave the cascade hazard
        # above in force. Driving the DBAPI cursor directly keeps it outside a
        # transaction, and leaves Alembic's `begin_transaction()` as the sole
        # owner of the migration's own.
        _pragma(connection, "foreign_keys=OFF")
        try:
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
