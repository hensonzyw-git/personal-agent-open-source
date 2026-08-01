#!/usr/bin/env python
"""Seed one `drill-sample` deletion-manifest entry for the DEV-035 restore drill.

Design 10.5's G5 evidence requires that the recovery environment can inject the
off-machine key ring and decrypt a **fixed sample** from the restored database.
That sample has to be a real sealed row: `check_aead_sample` opens it under the
data key with the AAD bound to (service, table, column, entry_id), so a copied
or hand-written envelope cannot stand in for one.

Until the conversation-deletion feature lands, production writes nothing to
`deletion_manifest`, so a drill against a real backup would fail the sample
check for the trivial reason that there is nothing to open. This script writes
exactly one row so the drill exercises the real cipher path.

What it deliberately does NOT do:

- It never deletes anything. The sealed object id names a conversation id that
  does not exist, so replay reports it as `already_absent` -- which is a real
  replay through the real handler, not a skipped entry.
- It refuses to overwrite an existing entry, so re-running cannot mask a
  tampered or stale sample.
- It writes one fixed, obviously-synthetic entry id. A drill sample must never
  be mistakable for a user's real deletion, in this table or in evidence.

Run on the ECS as the Agent service user, BEFORE the backup whose snapshot the
drill will restore:

    sudo -u personal-agent-api /opt/personal-agent/.venv/bin/python \
        /opt/personal-agent/scripts/seed_drill_sample.py \
        --database /var/lib/personal-agent-api/agent.sqlite
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from personal_agent.backup.deletion_manifest import MANIFEST_COLUMN, MANIFEST_TABLE
from personal_agent.keys import load_agent_data_keyring
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent.storage.models import DeletionManifest


#: Fixed so the drill can name it without discovering it, and prefixed so it
#: cannot be read as a real deletion in an audit or an evidence file.
ENTRY_ID = "drill-sample"

#: The sealed payload. A conversation id that will never exist, for the same
#: reason: replay must run the real handler and find nothing to remove.
OBJECT_ID = "conv-drill-sample-does-not-exist"

#: One of `REPLAY_HANDLERS`' keys -- an unknown type is refused at replay, and
#: refusing is correct, so the drill must use a type replay actually handles.
OBJECT_TYPE = "conversation"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "delete an existing drill-sample row first. Only for re-sealing "
            "after a key rotation; it is not the normal path."
        ),
    )
    args = parser.parse_args()

    if not args.database.exists():
        print(f"database does not exist: {args.database}", file=sys.stderr)
        return 2

    try:
        keyring = load_agent_data_keyring(dict(os.environ))
    except Exception as exc:  # noqa: BLE001 - reported, never printed with a key
        print(f"could not load the data key ring: {type(exc).__name__}", file=sys.stderr)
        return 2

    engine = create_database_engine(args.database)
    try:
        with session_factory(engine)() as session:
            existing = session.execute(
                text(
                    f"SELECT entry_id FROM {MANIFEST_TABLE} WHERE entry_id = :eid"
                ),
                {"eid": ENTRY_ID},
            ).fetchone()
            if existing is not None:
                if not args.replace:
                    print(
                        f"{ENTRY_ID} already present; nothing to do "
                        "(pass --replace only to re-seal after a key rotation)"
                    )
                    return 0
                session.execute(
                    text(f"DELETE FROM {MANIFEST_TABLE} WHERE entry_id = :eid"),
                    {"eid": ENTRY_ID},
                )

            envelope = keyring.encrypt(
                OBJECT_ID.encode("utf-8"),
                table=MANIFEST_TABLE,
                column=MANIFEST_COLUMN,
                row_id=ENTRY_ID,
            )
            session.add(
                DeletionManifest(
                    entry_id=ENTRY_ID,
                    object_type=OBJECT_TYPE,
                    encrypted_object_id=envelope,
                    deleted_at=datetime.now(timezone.utc),
                    backup_expiry_after=None,
                )
            )
            session.commit()
    finally:
        engine.dispose()

    # The kid, not the key. Enough to tell which ring sealed it after a
    # rotation, and nothing that helps open it.
    print(f"sealed {ENTRY_ID} under kid={keyring.active_kid}")
    print("run the backup before drilling, or the snapshot will not carry it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
