"""`DEV-035`: the deletion-manifest export and replay.

The ``deletion_manifest`` table exists so a restore can re-apply the user's
deletions and deleted data does not silently come back from an older backup
(technical design 10.5: "恢复 live DB 后、开放读取前必须重放"). Until this
module there was a table and a sealed column but no code that exported or
replayed it, so the property was aspirational.

Two halves:

- :func:`export_manifest` reads every manifest row out of the live Agent
  database and returns them as plain dicts. The object id stays sealed: a
  manifest that named conversations in plaintext would leak exactly what the
  user asked to remove. The export only transports the sealed envelope, the
  type, the entry id and the timestamps. This is what gets its own small
  encrypted copy inside the restic repository, separate from the DB snapshot,
  so a restore that replays it is not depending on the snapshot's own copy of
  the table.

- :func:`replay_manifest` is run after a restore, before reads open. For each
  entry it opens the sealed id under the data key (AAD-bound to
  ``deletion_manifest.encrypted_object_id`` for that ``entry_id``) and applies
  the deletion the ``object_type`` names. An unknown type fails closed: a
  future type the replay code has not been taught must not be skipped, or a
  deletion would quietly come back.

The vocabulary is deliberately small and explicit. An ``object_type`` is only
legal if it appears in :data:`REPLAY_HANDLERS`; anything else is a refusal.

The write side is now real, and the two halves are deliberately in different
modules. Marking an image deleted lives in
:mod:`personal_agent.media.deletion`, because it is a state transition that has
to be correct on its own; that module calls
:func:`personal_agent.storage.deletion.write_manifest_entry` inside the same
transaction. This module owns the *replay* half -- what a restore does with
those entries -- and its handlers only mark, never physically remove.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from personal_agent.media.deletion import (
    origin_media_ids_for_conversation,
    origin_media_ids_for_event,
    replay_media_deletion,
)
from personal_agent.storage.deletion import MANIFEST_COLUMN, MANIFEST_TABLE
from personal_agent_core.crypto import DecryptionError, KeyRing
from personal_agent.storage.models import (
    Conversation,
    ConversationAlias,
    ConversationEvent,
    ContextSession,
    DeletionManifest,
)


#: Re-exported so the AAD a value was sealed under and the AAD it is opened
#: under have exactly one definition between them. They live in
#: :mod:`personal_agent.storage.deletion`, which the media state machine also
#: imports; two copies could drift, and the failure that causes lands on the one
#: code path -- restore -- that has to work after everything else has broken.


def export_manifest(session: Session) -> list[dict[str, Any]]:
    """Read the full deletion manifest as sealed, transportable dicts.

    Returns one dict per row with the entry id, object type, the sealed
    envelope (untouched), and the timestamps. No object id is decrypted here:
    the export is meant to live in its own encrypted copy inside the backup,
    and decrypting at export time would put a plaintext id into a file that
    outlives the live database.
    """
    rows = session.execute(select(DeletionManifest)).scalars().all()
    return [
        {
            "entry_id": row.entry_id,
            "object_type": row.object_type,
            "encrypted_object_id": row.encrypted_object_id,
            "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            "backup_expiry_after": row.backup_expiry_after.isoformat()
            if row.backup_expiry_after
            else None,
        }
        for row in rows
    ]


def _delete_conversation(session: Session, object_id: str) -> int:
    """Delete one conversation, its events/sessions, and the images it originated.

    Child rows are deleted explicitly rather than relying on the foreign-key
    ``ON DELETE CASCADE``: that cascade only fires when ``PRAGMA foreign_keys``
    is on for the connection, and a restored database opened through a plain
    ``sqlite3`` connection (as a restore drill might) has it off by default.
    Deleting the children in the same transaction is correct with or without
    the pragma, and a replay must not depend on a connection setting that is
    easy to forget on a fresh box.

    The media fan-out runs **before** the events are deleted. The bindings that
    say which images this conversation owned carry ``ON DELETE CASCADE`` against
    ``conversation_events``, so deleting the events first would take them with
    it and leave nothing to ask. On the connections this service opens the
    cascade does fire -- ``db.upgrade`` restores ``PRAGMA foreign_keys`` and it
    stays on for every connection the engine hands out afterwards -- so that
    order is load-bearing here, not merely defensive. On a connection where the
    pragma is off, as a bare ``sqlite3`` shell has it, the bindings survive and
    the fan-out still asks the right question. The order is correct either way,
    which is why it is stated as a rule rather than as a consequence of the
    current pragma.

    Returns the number of conversation rows removed, so a replay can tell a
    successful delete from an id that was already absent (a re-replay, or a
    deletion the snapshot had already absorbed).
    """
    now = _now()
    for media_id in origin_media_ids_for_conversation(
        session, conversation_id=object_id
    ):
        replay_media_deletion(session, media_id=media_id, now=now)
    session.execute(
        delete(ConversationEvent).where(ConversationEvent.conversation_id == object_id)
    )
    session.execute(
        delete(ContextSession).where(ContextSession.conversation_id == object_id)
    )
    session.execute(
        delete(ConversationAlias).where(ConversationAlias.conversation_id == object_id)
    )
    result = session.execute(
        delete(Conversation).where(Conversation.conversation_id == object_id)
    )
    return result.rowcount or 0


def _delete_conversation_event(session: Session, object_id: str) -> int:
    # Same order, same reason as the conversation handler: the deleting message
    # is the only thing that knows which images it originated.
    now = _now()
    for media_id in origin_media_ids_for_event(session, event_id=object_id):
        replay_media_deletion(session, media_id=media_id, now=now)
    result = session.execute(
        delete(ConversationEvent).where(ConversationEvent.event_id == object_id)
    )
    return result.rowcount or 0


def _delete_media_object(session: Session, object_id: str) -> int:
    """Mark one image deleted after a restore.

    Marks only. Physically removing the bytes is the reaper's job, and §6 puts
    it after this commit on purpose -- replay runs before reads open, and a
    replay that also tried to unlink files would have to hold the media lock for
    as long as the whole restore takes.

    Returns 1 when this call decided the deletion and 0 when the object was
    already deleting or gone, which is the same "already absent" a re-replay
    produces. The manifest entry that brought us here is not re-written: it is
    already the record of this deletion.
    """
    decided = replay_media_deletion(session, media_id=object_id, now=_now())
    return 1 if decided else 0


def _now() -> datetime:
    """The replay's clock.

    Read here rather than threaded through the handler signature, which §6
    fixes as ``Callable[[Session, str], int]``. A replay is a restore-time
    operation with no user waiting on it, so the wall clock is the right source;
    what matters is that all of a replay's rows carry one timestamp close to the
    restore, not that a caller could inject one.
    """
    return datetime.now(tz=timezone.utc)


#: The closed vocabulary of deletions a restore can replay. A type not here is
#: a future deletion kind the replay has not been taught; failing closed on it
#: is the property that keeps an unhandled deletion from coming back.
REPLAY_HANDLERS: dict[str, Callable[[Session, str], int]] = {
    "conversation": _delete_conversation,
    "conversation_event": _delete_conversation_event,
    "media_object": _delete_media_object,
}


class ManifestReplayError(RuntimeError):
    """A manifest entry could not be replayed.

    Replay runs after a restore and before reads open; any failure here must
    stop the restore rather than let a deletion silently fail, so this is
    raised, not logged-and-skipped.
    """


def replay_manifest(
    session: Session,
    entries: Iterable[Mapping[str, Any]],
    keyring: KeyRing,
) -> dict[str, Any]:
    """Apply each manifest entry's deletion to the restored database.

    For every entry the sealed object id is opened under ``keyring`` (AAD-bound
    to this manifest table/column/entry_id) and the matching
    :data:`REPLAY_HANDLERS` deletion runs. Unknown object types and decryption
    failures raise :class:`ManifestReplayError` rather than skip -- a skipped
    entry is a deletion that comes back.

    Returns a count summary. The session is committed only if every entry
    replayed without error; a failure leaves the transaction for the caller to
    roll back.
    """
    applied = 0
    already_absent = 0
    for entry in entries:
        object_type = entry.get("object_type")
        handler = REPLAY_HANDLERS.get(object_type) if isinstance(object_type, str) else None
        if handler is None:
            raise ManifestReplayError(
                f"unknown object_type {object_type!r} in manifest entry "
                f"{entry.get('entry_id')!r}; refusing to skip a deletion"
            )
        envelope = entry.get("encrypted_object_id")
        entry_id = entry.get("entry_id")
        if not isinstance(envelope, Mapping):
            raise ManifestReplayError(
                f"manifest entry {entry_id!r} has no sealed object id"
            )
        try:
            plaintext = keyring.decrypt(
                dict(envelope),
                table=MANIFEST_TABLE,
                column=MANIFEST_COLUMN,
                row_id=str(entry_id),
            )
        except DecryptionError as exc:
            raise ManifestReplayError(
                f"could not open sealed object id for entry {entry_id!r}: {exc}"
            ) from exc
        object_id = plaintext.decode("utf-8")
        removed = handler(session, object_id)
        if removed:
            applied += 1
        else:
            already_absent += 1
    session.commit()
    return {"applied": applied, "already_absent": already_absent}
