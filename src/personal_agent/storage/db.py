"""Migration commands for the Agent API database.

Alembic is configured in code rather than from an ini file. The database path is
always an explicit argument, so a stray `ALEMBIC_CONFIG` or a leftover working
directory cannot silently point a migration at the wrong service's database.

Every revision must have a working `downgrade`. Technical design 10.6 requires a
readable recovery path for the previous version before any forward-only change.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from personal_agent.storage.engine import check_integrity, create_database_engine


MIGRATIONS_PATH = Path(__file__).parent / "migrations"


def alembic_config(
    engine: Engine, *, cap001_inputs: object | None = None
) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    # Alembic still wants a URL string present; the engine in `attributes` is
    # what actually gets used, and env.py refuses to run without it.
    config.set_main_option("sqlalchemy.url", str(engine.url))
    config.attributes["connection"] = engine
    # `CAP-001` (revision 0003) needs key material and a verified backup to move
    # existing conversation events. It is passed in rather than read from the
    # environment inside the revision, so a migration cannot pick up whatever
    # key happens to be exported in the shell that ran it.
    if cap001_inputs is not None:
        config.attributes["cap001_inputs"] = cap001_inputs
    return config


def upgrade(
    engine: Engine,
    revision: str = "head",
    *,
    cap001_inputs: object | None = None,
) -> None:
    command.upgrade(
        alembic_config(engine, cap001_inputs=cap001_inputs), revision
    )


def downgrade(
    engine: Engine, revision: str, *, cap001_inputs: object | None = None
) -> None:
    command.downgrade(
        alembic_config(engine, cap001_inputs=cap001_inputs), revision
    )


def current(engine: Engine) -> None:
    command.current(alembic_config(engine), verbose=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the Agent API database schema."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help=(
            "the pre-migration encrypted backup, verified before CAP-001 moves "
            "any existing conversation event"
        ),
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("current")
    upgrade_parser = subparsers.add_parser("upgrade")
    upgrade_parser.add_argument("revision", nargs="?", default="head")
    downgrade_parser = subparsers.add_parser("downgrade")
    downgrade_parser.add_argument("revision")

    args = parser.parse_args()
    engine = create_database_engine(args.database)

    if args.database.exists():
        check_integrity(engine)

    if args.action == "current":
        current(engine)
        return

    inputs = _cap001_inputs(args.backup)
    if args.action == "upgrade":
        upgrade(engine, args.revision, cap001_inputs=inputs)
    else:
        downgrade(engine, args.revision, cap001_inputs=inputs)


def _cap001_inputs(backup: Path | None):
    """Load `CAP-001` migration material, if this environment carries it.

    Absent material is not an error here: a fresh database needs none, and the
    revision itself refuses when it needs material it was not given. Loading it
    optimistically would be worse -- a partially-configured environment would
    then fail deep inside a schema change instead of before one.
    """
    from personal_agent.keys import (
        AgentKeyConfigError,
        load_agent_data_keyring,
        load_identifier_key,
    )
    from personal_agent.storage.cap001_migration import MigrationInputs

    try:
        keyring = load_agent_data_keyring()
        identifier_key = load_identifier_key()
    except AgentKeyConfigError:
        return None
    return MigrationInputs(
        keyring=keyring, identifier_key=identifier_key, backup_path=backup
    )
