"""Marking a media object deleted, and the fan-out that finds its users.

Design §6. A media deletion has two halves that must not be confused:

- **The decision**, which is what this module makes. It marks the object
  ``deleting`` and writes a manifest entry in the *same* transaction, so that
  from that commit forward the object grants no new use and a restore can tell
  it was removed. ``deleted_at`` is stamped here rather than when the bytes
  finally go: the tombstone has to exist from the moment the deletion is
  decided, because "取消后续复用" and "已删除图显示墓碑" are properties of the
  decision, not of the reaper having run.

- **The physical removal**, which the reaper does later, under the lock, and
  which is the only thing allowed to call the object ``deleted``. A reaper that
  cannot get the lock defers and says so; it must never report a deferred
  deletion as a completed one (§6).

The asymmetry (§6, and the model's own comment on ``MediaBinding``) is why the
fan-out reads roles rather than just ids: deleting the message that *originated*
an image destroys the image, while deleting a message that merely *reused* it
removes that use relation and leaves the persisted image alone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from personal_agent.storage.deletion import write_manifest_entry
from personal_agent.storage.models import ConversationEvent, MediaBinding, MediaObject
from personal_agent_core.crypto import KeyRing


#: States a deletion may be decided from.
#:
#: The list is not a whitelist of what is safe to delete -- it is the states
#: where deleted bytes could still be reachable *or* still be produced. An
#: upload in ``pending``/``uploading`` is included on purpose: marking it
#: ``deleting`` is what makes the writer's own compare-and-swap fail, so an
#: upload that was already in flight cannot publish after the user deleted it.
DELETABLE_STATES: Final[frozenset[str]] = frozenset(
    {"pending", "uploading", "uploaded", "ready", "bound"}
)

#: A deletion has already been decided. Everything here is a no-op, so a second
#: request (a retried delete, a replay after a restore) behaves the same as the
#: first and does not write a second manifest entry for one object.
ALREADY_DELETED_STATES: Final[frozenset[str]] = frozenset(
    {"deleting", "reaping", "deleted"}
)

#: States whose objects never had persisted bytes, so there is nothing a
#: manifest entry could stop a restore from bringing back. They are still marked
#: (the row becomes a tombstone and the id is never reused) but no entry is
#: written: an entry here would only add noise to the one list that has to be
#: replayed correctly.
NEVER_PUBLISHED_STATES: Final[frozenset[str]] = frozenset({"expired", "rejected"})

#: Every state a deletion may be decided from: the two sets above, and nothing
#: else. They are not a partition of one policy but of one *precondition* --
#: "may this object be marked?" -- and the split is only about whether an entry
#: is owed. Writing the union out rather than deriving it in the guard keeps the
#: decision readable at the point it is enforced.
DECIDABLE_STATES: Final[frozenset[str]] = DELETABLE_STATES | NEVER_PUBLISHED_STATES


class StaleMediaStateError(RuntimeError):
    """The object moved between this call's read and its write.

    Raised rather than retried: the caller is normally inside
    :func:`personal_agent_core.sqlite.run_write_transaction`, which re-runs the
    whole unit against fresh state, and a retry loop here would nest inside it.
    """


def _decide_deletion(session: Session, *, media_id: str, now: datetime) -> str | None:
    """Compare-and-swap one object to ``deleting``. Returns its previous state.

    ``None`` means there was nothing to decide: the object does not exist, or a
    previous call already decided. Both are success for a retry or a replay.

    The state is read with a plain ``SELECT`` rather than through the ORM's
    identity map (§5.2): this runs under the restore and under concurrent
    deletes, and the identity map would hand back a version another session has
    already superseded.
    """
    row = session.execute(
        select(MediaObject.state, MediaObject.state_version).where(
            MediaObject.media_id == media_id
        )
    ).one_or_none()
    if row is None:
        return None
    state, version = row
    if state in ALREADY_DELETED_STATES:
        return None

    result = session.execute(
        update(MediaObject)
        .where(
            MediaObject.media_id == media_id,
            MediaObject.state == state,
            MediaObject.state_version == version,
            # In the compare-and-swap rather than in a branch above, so a state
            # this code has not been taught to delete from fails closed the same
            # way a lost race does. The column's CHECK constraint enumerates the
            # same vocabulary today, which makes the guard unreachable while the
            # two agree -- and that is the point: a later migration that adds a
            # state must not silently make it deletable.
            MediaObject.state.in_(tuple(DECIDABLE_STATES)),
        )
        .values(state="deleting", state_version=version + 1, deleted_at=now)
    )
    if result.rowcount != 1:
        raise StaleMediaStateError(
            f"{media_id} is no longer at {state}/v{version}; another writer moved it first"
        )
    return state


def mark_media_deleting(
    session: Session,
    *,
    media_id: str,
    keyring: KeyRing,
    now: datetime,
    backup_expiry_after: datetime | None = None,
) -> bool:
    """Decide that one media object is deleted, and record it in the manifest.

    Returns whether this call decided it. The entry is written here, in the same
    transaction as the state change, because that is the whole point of it: an
    entry written afterwards can be lost to a crash in between, and the deletion
    would then have nothing left to replay it.
    """
    previous = _decide_deletion(session, media_id=media_id, now=now)
    if previous is None:
        return False
    if previous not in NEVER_PUBLISHED_STATES:
        write_manifest_entry(
            session,
            object_type="media_object",
            object_id=media_id,
            keyring=keyring,
            now=now,
            backup_expiry_after=backup_expiry_after,
        )
    return True


def replay_media_deletion(session: Session, *, media_id: str, now: datetime) -> bool:
    """Apply a deletion that was decided before the backup was taken.

    Writes no manifest entry, deliberately. The entry being replayed *is* the
    record of this deletion and it is already in the manifest table; a second
    entry would give one deletion two rows, and ``backup_expiry_after`` would
    then have to expire both before either could be dropped.

    Kept separate from :func:`mark_media_deleting` rather than given an optional
    keyring, so that "this call did not record anything" is visible at the call
    site. A ``keyring=None`` argument would make skipping the entry look like a
    parameter, and the first caller to pass ``None`` for convenience would
    reintroduce exactly the resurrection this manifest exists to prevent.
    """
    return _decide_deletion(session, media_id=media_id, now=now) is not None


def _origin_media_ids(session: Session, condition) -> list[str]:
    """The media objects these bindings *originate*, in a stable order.

    Only ``origin`` bindings: a ``reuse`` is a use relation, and §6 keeps the
    two apart precisely so that dropping a reuse does not destroy the image.
    """
    return list(
        session.execute(
            select(MediaBinding.media_id)
            .where(condition, MediaBinding.role == "origin")
            .order_by(MediaBinding.binding_id)
        )
        .scalars()
        .all()
    )


def origin_media_ids_for_event(session: Session, *, event_id: str) -> list[str]:
    """The images one message originated, which deleting that message destroys.

    Deleting the message row itself is the caller's job, and so is marking each
    id -- :func:`mark_media_deleting` when the deletion is being decided now, or
    :func:`replay_media_deletion` when it is being re-applied after a restore.
    This function answers only "which images did this message own", which is the
    part that has to be read *before* the message goes: the bindings cascade
    away with the event, and afterwards there is nothing left to ask.

    Traversal is separated from marking rather than taking a keyring and
    returning a count, because the two callers differ in exactly that one
    respect and in no other. A shared helper that took an optional keyring would
    hide the difference at the call site, and the difference is "does this
    deletion get recorded or is it already recorded".
    """
    return _origin_media_ids(session, MediaBinding.event_id == event_id)


def origin_media_ids_for_conversation(
    session: Session, *, conversation_id: str
) -> list[str]:
    """The images any message of one conversation originated.

    Reads the events first and the bindings second, in that order, so the
    fan-out does not depend on how SQLite chooses to plan a subquery -- and so
    that a conversation whose events are already gone fans out to nothing rather
    than to an error.
    """
    event_ids = list(
        session.execute(
            select(ConversationEvent.event_id).where(
                ConversationEvent.conversation_id == conversation_id
            )
        )
        .scalars()
        .all()
    )
    if not event_ids:
        return []
    return _origin_media_ids(
        session, or_(*[MediaBinding.event_id == event_id for event_id in event_ids])
    )
