"""Console entrypoint for the periodic cleanup job (`personal-agent-cleanup`).

`DEV-036`. A systemd timer runs this daily; it may also be run by hand. Both are
safe: the job only deletes rows that are *already* useless, and deleting them
again deletes nothing, so a second run the same day is a no-op.

The deletion scope is a **whitelist, not a blacklist**. This module names every
table it is allowed to touch and deletes nothing else. The permanent archive --
`conversation_events`, `conversations`, `conversation_aliases`,
`deletion_manifest` -- is not on the list and has no code path here, so the
DEV-036 acceptance rule "永久 conversation 不被清理" holds structurally rather
than by the job's discretion. Adding a table is a code change in this file, not
a flag.

Today the whitelist has one entry: expired enrollment codes. A code past
`expires_at` can never enrol a device (the consume path refuses on
`expires_at <= now`), so the row is dead the moment it expires, whether or not
it was used. It has no foreign keys pointing at it, so removing it cannot
orphan anything. Other expirable rows (auth challenges, undeliverable
notifications, spent duplicate checks) are deliberately *not* cleared yet: each
needs its audit/recovery dependency confirmed before it joins the whitelist,
and a cleanup job that waits is safer than one that guesses.

Like the review CLI, it composes only what it needs -- the Agent database, no
model, no MCP, no Feishu, no data key. The summary it prints is counts only:
enrollment code hashes are credentials, even expired ones, and this output goes
to the journal.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import delete, select

from personal_agent.storage.engine import (
    check_integrity,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import EnrollmentCode
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now


def purge_expired_enrollment_codes(session, *, now) -> int:
    """Delete enrollment codes past their expiry.

    Returns the row count. `expires_at <= now` is exactly the complement of the
    consume path's `expires_at > now` gate, so a row removed here could not have
    enrolled anything under any code path -- the deletion is of dead state, not
    of a live option.
    """
    expired = (
        select(EnrollmentCode.code_hash).where(EnrollmentCode.expires_at <= now)
    )
    result = session.execute(
        delete(EnrollmentCode).where(EnrollmentCode.code_hash.in_(expired))
    )
    return result.rowcount or 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Delete expired non-permanent state from the Agent database. "
            "Whitelist-only; the permanent conversation archive is never touched."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--now",
        default=None,
        help=(
            "Override the UTC instant treated as now (ISO 8601), for testing. "
            "Defaults to the real clock."
        ),
    )
    args = parser.parse_args()

    now = utc_now()
    if args.now is not None:
        from personal_agent_core.timeutil import parse_rfc3339

        try:
            now = parse_rfc3339(args.now)
        except ValueError as exc:
            raise SystemExit(f"--now must be ISO 8601 UTC: {exc}") from exc

    if not args.database.exists():
        # Creating one here would silently produce an empty cleanup history and
        # mask a misconfigured timer.
        raise SystemExit(f"no Agent database at {args.database}")

    engine = create_database_engine(args.database)
    check_integrity(engine)
    sessions = session_factory(engine)

    try:
        with sessions() as session:
            deleted = run_write_transaction(
                session,
                lambda: purge_expired_enrollment_codes(session, now=now),
            )
    finally:
        engine.dispose()

    print(f"expired enrollment codes deleted: {deleted}")


if __name__ == "__main__":  # pragma: no cover
    main()
