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
        # A `PRAGMA` is not DML, so pysqlite does not open a transaction for it
        # and the setting actually takes effect. Inside a transaction it would
        # be a silent no-op.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        # SQLAlchemy 2.0 opens a transaction on the first statement, and
        # Alembic's `begin_transaction()` becomes a no-op when it finds one
        # already open -- leaving nobody to commit, so the whole migration
        # would roll back on close. Releasing it here hands ownership back to
        # Alembic; the PRAGMA is connection state and survives the rollback.
        connection.rollback()
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
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported for this service")
run_migrations_online()
