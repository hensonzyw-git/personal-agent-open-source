#!/usr/bin/env python3
"""DAL database snapshot for the offsite backup set.

Runs as the personal-agent-dal service user against its own 0700 data dir.
The backup user cannot read the live DB, so this process produces the
snapshot itself via the shared online_backup primitive; the backup user only
reads the staged file. Mirrors the Finance db CLI's backup action
(personal_agent.storage.db), including its exit-code contract:
1 = snapshot failed integrity, 2 = source could not be opened at all.
"""

import sys
from pathlib import Path

from personal_agent_core.sqlite import (
    STAGED_SNAPSHOT_MODE,
    BackupError,
    BackupUnavailableError,
    online_backup,
)


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: dal_snapshot.py SOURCE_DB DEST_SNAPSHOT",
            file=sys.stderr,
        )
        return 64
    source, out = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        # 0640 staged mode: the snapshot exists to be read by the
        # personal-agent-backup user through the staging dir's group.
        online_backup(source, out, mode=STAGED_SNAPSHOT_MODE)
    except BackupUnavailableError as exc:
        print(f"backup unavailable: {exc}", file=sys.stderr)
        return 2
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backed up {source} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
