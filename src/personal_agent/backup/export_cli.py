"""`DEV-035`: export the deletion manifest to its own encrypted-at-rest copy.

Run by the db-backup timer alongside the Agent DB snapshot. The output is a
small JSON file restic carries *separately* from the DB snapshot, so a restore
can replay deletions without depending on the snapshot's own copy of the
``deletion_manifest`` table (which an older snapshot might not have, or might
have at an older state).

The object ids stay sealed: this is a transport copy, not a view, and
decrypting here would put a plaintext id into a file that outlives the live
database. Replay decrypts under the data key at restore time.

Output is written atomically (temp + replace) at mode 0600; a half-written
manifest is the one file a restore must never read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from personal_agent.backup.deletion_manifest import export_manifest
from personal_agent.storage.engine import create_database_engine, session_factory


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export the deletion manifest as sealed, transportable JSON for "
            "offsite backup. Does not decrypt object ids."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="destination JSON path; written atomically at mode 0600",
    )
    args = parser.parse_args()

    engine = create_database_engine(args.database)
    with session_factory(engine)() as session:
        entries = export_manifest(session)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{args.out.name}.", suffix=".tmp", dir=str(args.out.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_path, args.out)
        os.chmod(args.out, 0o600)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    engine.dispose()
    print(f"exported {len(entries)} deletion-manifest entries -> {args.out}")
    return


if __name__ == "__main__":
    main()
