"""Snapshot the risk monitor database for offsite backup.

Mirrors the Agent API db's ``backup`` subcommand, but scoped to the risk
monitor's own SQLite database — ADR-0001 keeps the two deliberately distinct, so
the risk monitor gets its own entrypoint rather than borrowing the Agent db
CLI. The snapshot is produced by the Online Backup API in
``personal_agent_core.sqlite``, so it is a consistent point-in-time copy even
while the daily job is mid-write. Exit codes match ``observe_cli``: 1 = snapshot
failed integrity, 2 = source could not be opened.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from personal_agent_core.sqlite import (
    STAGED_SNAPSHOT_MODE,
    BackupError,
    BackupUnavailableError,
    online_backup,
)


def _backup(database: Path, out: Path) -> int:
    try:
        # 0640, not 0600: the snapshot exists to be read by the backup user
        # through the staging dir's group (see personal-agent-db-backup).
        online_backup(database, out, mode=STAGED_SNAPSHOT_MODE)
    except BackupUnavailableError as exc:
        print(f"backup unavailable: {exc}", file=sys.stderr)
        return 2
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backed up {database} -> {out}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Snapshot the risk monitor database for offsite backup."
    )
    parser.add_argument("--database", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "backup":
        raise SystemExit(_backup(args.database, args.out))
