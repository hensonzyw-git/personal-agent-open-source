"""Migration commands for the Finance MCP database.

Alembic is configured in code rather than from an ini file. The database path is
always an explicit argument, so a stray `ALEMBIC_CONFIG` or a leftover working
directory cannot silently point a migration at the wrong service's database.

Every revision must have a working `downgrade`. Technical design 10.6 requires a
readable recovery path for the previous version before any forward-only change.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from personal_data_mcp.storage.engine import check_integrity, create_database_engine


MIGRATIONS_PATH = Path(__file__).parent / "migrations"


def _backup(database: Path, out: Path) -> int:
    """Produce one consistent SQLite snapshot of the Finance MCP database.

    Runs as the Finance service user against its own 0700 data directory, so
    the backup process never reads a secret or crosses a service boundary.
    Exit codes: 1 = snapshot failed integrity, 2 = source could not be opened.
    """
    from personal_agent_core.sqlite import (
        STAGED_SNAPSHOT_MODE,
        BackupError,
        BackupUnavailableError,
        online_backup,
    )

    try:
        # 0640: staged for the personal-agent-backup user via the staging dir's
        # group. See personal_agent.storage.db._backup for the same reasoning.
        online_backup(database, out, mode=STAGED_SNAPSHOT_MODE)
    except BackupUnavailableError as exc:
        print(f"backup unavailable: {exc}", file=sys.stderr)
        return 2
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backed up {database} -> {out}")
    return 0


def alembic_config(engine: Engine) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    # Alembic still wants a URL string present; the engine in `attributes` is
    # what actually gets used, and env.py refuses to run without it.
    config.set_main_option("sqlalchemy.url", str(engine.url))
    config.attributes["connection"] = engine
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    command.upgrade(alembic_config(engine), revision)


def downgrade(engine: Engine, revision: str) -> None:
    command.downgrade(alembic_config(engine), revision)


def current(engine: Engine) -> None:
    command.current(alembic_config(engine), verbose=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the Finance MCP database schema."
    )
    parser.add_argument("--database", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("current")
    upgrade_parser = subparsers.add_parser("upgrade")
    upgrade_parser.add_argument("revision", nargs="?", default="head")
    downgrade_parser = subparsers.add_parser("downgrade")
    downgrade_parser.add_argument("revision")
    backup_parser = subparsers.add_parser(
        "backup",
        description=(
            "Produce one consistent SQLite snapshot (Online Backup API) of the "
            "Finance MCP database for offsite backup. Does not run migrations."
        ),
    )
    backup_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="destination snapshot path; written atomically at mode 0600",
    )
    calendar_parser = subparsers.add_parser(
        "calendar-barrier", description="Operate the local calendar mirror rebuild barrier."
    )
    calendar_parser.add_argument(
        "calendar_action", choices=("enter", "leave", "rebuild")
    )
    calendar_parser.add_argument("--device-id", required=True)
    calendar_parser.add_argument(
        "--confirm", action="store_true",
        help="required for rebuild because it physically clears the server mirror cache",
    )

    args = parser.parse_args()

    if args.action == "backup":
        raise SystemExit(_backup(args.database, args.out))

    engine = create_database_engine(args.database)

    if args.database.exists():
        check_integrity(engine)

    if args.action == "calendar-barrier":
        if args.confirm is False and args.calendar_action == "rebuild":
            raise SystemExit("calendar rebuild requires --confirm")
        from datetime import datetime, timezone
        from personal_data_mcp.calendar import policy
        from personal_data_mcp.storage.engine import session_factory

        with session_factory(engine)() as session:
            now = datetime.now(tz=timezone.utc)
            if args.calendar_action == "enter":
                policy.enter_maintenance(session, now=now)
            elif args.calendar_action == "leave":
                policy.leave_maintenance(session, now=now)
            else:
                policy.begin_rebuild(session, device_id=args.device_id, now=now)
            session.commit()
        print(f"calendar barrier {args.calendar_action} completed for {args.device_id}")
    elif args.action == "current":
        current(engine)
    elif args.action == "upgrade":
        upgrade(engine, args.revision)
    else:
        downgrade(engine, args.revision)
