"""Alembic environment for the Finance MCP database.

The database URL is supplied by the caller through `config.attributes`, never
read from an ini file, so a migration can never be pointed at the wrong service
database by an environment that happens to be lying around.

Foreign keys are switched **off** for the duration of a migration, and the
integrity they express is re-checked before the transaction commits -- the same
setting and re-check the Agent database uses. Batch migrations recreate a table
(create a copy, move the rows, `DROP` the original, rename), and SQLite treats
dropping a parent table as an implicit `DELETE` of every child row, so the
`tool_executions` -> `external_receipts` foreign key would otherwise block the
drop (with `ON DELETE RESTRICT`) or, with `CASCADE`, silently delete receipts.
`PRAGMA foreign_key_check` inside the transaction is what makes the guarantee
real -- any dangling reference aborts and rolls the whole migration back.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Engine

from personal_data_mcp.storage.models import Base


target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection")
    if not isinstance(connectable, Engine):
        raise RuntimeError(
            "personal_data_mcp migrations require an Engine in "
            "config.attributes['connection']; use personal-data-mcp-db"
        )

    with connectable.connect() as connection:
        # SQLite ignores `PRAGMA foreign_keys` inside a transaction, silently.
        # Driving the DBAPI cursor directly keeps it outside a transaction, and
        # leaves Alembic's `begin_transaction()` as the sole owner of the
        # migration's own -- the same reasoning as the Agent database's env.
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
