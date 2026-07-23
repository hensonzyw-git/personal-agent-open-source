"""Alembic environment for the Finance MCP database.

The database URL is supplied by the caller through `config.attributes`, never
read from an ini file, so a migration can never be pointed at the wrong service
database by an environment that happens to be lying around.
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported for this service")
run_migrations_online()
