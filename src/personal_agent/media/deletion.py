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

import enum

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from personal_agent.media.locking import MediaLockError, media_locks
from personal_agent.media.store import MediaStore
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


class ReapOutcome(enum.Enum):
    """What one reaper pass did, in the four shapes a caller must tell apart.

    An enum rather than a bool because "did not remove the bytes" has three
    different meanings and only one of them is a failure. Collapsing them is how
    a deferred deletion gets counted as a completed one (§6's explicit
    prohibition): ``REAPED`` is the only member that means the bytes are gone.
    """

    #: The persisted image is removed (or was already) and the row says ``deleted``.
    REAPED = "reaped"
    #: Someone else holds the lock. The deletion is authorised and not done.
    DEFERRED = "deferred"
    #: An earlier pass finished. Nothing to do.
    ALREADY_DELETED = "already_deleted"
    #: No such row. Nothing to do.
    ABSENT = "absent"


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
        .values(
            state="deleting",
            state_version=version + 1,
            deleted_at=now,
            updated_at=now,
        )
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


#: States the reaper may act on: a deletion has been decided and committed, and
#: the bytes are still expected to be there. ``deleting`` is the ordinary entry
#: point; ``reaping`` is a previous attempt that stopped between clearing the
#: files and committing the terminal state (§5.3's "reaping 中断").
REAPABLE_STATES: Final[frozenset[str]] = frozenset({"deleting", "reaping"})


class MediaNotDeletableError(RuntimeError):
    """The reaper was pointed at an object no deletion was ever decided for.

    A refusal rather than an outcome value on purpose. ``bound``, ``ready`` and
    the rest are healthy objects, and a caller that hands one to the reaper has
    a bug or a corrupted deployment; both want a loud failure, and returning a
    quotable result would let a loop treat it as routine and move on.
    """


def reap_media_object(
    session: Session,
    *,
    store: MediaStore,
    media_id: str,
    now: datetime,
) -> ReapOutcome:
    """Physically remove one decided object's persisted image (design §6).

    The order here *is* the contract, so it is worth stating plainly:

    1. Read the state, and refuse unless a deletion was committed for it. The
       "durable deletion intent" §6 asks for is the committed ``deleting`` row
       itself -- the manifest entry cannot be looked up by id, because the id
       is sealed in it by design, and it does not need to be: the decision
       writes the state change and the entry in one transaction, so a visible
       ``deleting`` means the entry committed with it.
    2. Compare-and-swap to ``reaping`` and commit, **before** touching a byte.
       §4's rule is "对象清理先 CAS reaping 并 commit"; a crash after this point
       leaves a row that says "authorised, not yet removed", which the next pass
       resumes.
    3. Take the lock. Non-blocking, and outside any transaction -- a reader that
       holds the stripe past its deadline defers this reap (§6: "不能按租约到期
       强删"), and a deferral leaves the row at ``reaping`` and reports itself as
       :attr:`ReapOutcome.DEFERRED`, never as a completed removal.
    4. Clear the files, then compare-and-swap ``reaping`` → ``deleted``.

    **This function owns its transaction boundaries**, which is unusual here and
    is why it is called out rather than left for a reader to infer: it commits
    twice with file I/O and a lock acquisition in between, so a caller must not
    wrap it in :func:`~personal_agent_core.sqlite.run_write_transaction` or in a
    unit of work of its own. Every step is idempotent, so re-running it after a
    crash, a deferral or an exception resumes rather than corrupts.

    Only the persisted image is removed. The upload pieces under ``staging``
    belong to the attempt GC -- §4.2 gives them a per-attempt cleanup marker and
    §5.3 has the ready-with-residue row remove them under the same intent -- and
    a quarantined file is a diagnostic copy of something that was never adopted.
    Neither is this function's to remove, and both are open questions rather than
    settled scope.
    """
    row = session.execute(
        select(MediaObject.state, MediaObject.state_version).where(
            MediaObject.media_id == media_id
        )
    ).one_or_none()
    if row is None:
        # §5.3's reaping-interrupted row reads "缺文件视为已完成"; the same
        # applies to a row that is gone entirely, which is what a replayed
        # manifest against an already-pruned database produces.
        return ReapOutcome.ABSENT
    state, version = row
    if state == "deleted":
        return ReapOutcome.ALREADY_DELETED
    if state not in REAPABLE_STATES:
        raise MediaNotDeletableError(
            f"{media_id} is {state}; the reaper only removes an object whose "
            "deletion was decided and committed"
        )

    expected_version = version
    if state == "deleting":
        result = session.execute(
            update(MediaObject)
            .where(
                MediaObject.media_id == media_id,
                MediaObject.state == "deleting",
                MediaObject.state_version == version,
            )
            .values(state="reaping", state_version=version + 1, updated_at=now)
        )
        if result.rowcount != 1:
            raise StaleMediaStateError(
                f"{media_id} is no longer deleting/v{version}; another writer moved it first"
            )
        expected_version = version + 1
        session.commit()
    else:
        # End the read transaction before taking the lock. §5.2: a session that
        # has read cannot upgrade its snapshot to a write once another session
        # has committed, and the lock is to be taken with no transaction open.
        session.rollback()

    try:
        with media_locks(store.roots.root, [media_id], blocking=False):
            from personal_agent.storage.models import MediaAttempt
            attempts = session.scalars(select(MediaAttempt.attempt_number).where(
                MediaAttempt.media_id == media_id
            )).all()
            session.execute(update(MediaAttempt).where(MediaAttempt.media_id == media_id)
                            .values(state="abandoned", updated_at=now))
            session.commit()
            for attempt in attempts:
                store.discard_staging(media_id, attempt)
            store.discard_object_staging(media_id)
            store.discard_final(media_id)
            store.discard_quarantine(media_id)
            session.execute(update(MediaAttempt).where(MediaAttempt.media_id == media_id)
                            .values(cleaned_at=now))
            result = session.execute(
                update(MediaObject)
                .where(
                    MediaObject.media_id == media_id,
                    MediaObject.state == "reaping",
                    MediaObject.state_version == expected_version,
                )
                .values(state="deleted", state_version=expected_version + 1, updated_at=now)
            )
            if result.rowcount != 1:
                raise StaleMediaStateError(
                    f"{media_id} is no longer reaping/v{expected_version}; "
                    "another writer moved it first"
                )
            session.commit()
    except MediaLockError:
        # Deliberately not an error return and deliberately not fatal: the lock
        # holder is a reader doing its job, and §6 says to warn or defer. The
        # row keeps saying ``reaping``, which is the honest description -- the
        # deletion is authorised and has not happened.
        return ReapOutcome.DEFERRED
    return ReapOutcome.REAPED


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

    The events are named by a subquery rather than expanded into one ``OR`` per
    event. The expanded form grew with the conversation and hit SQLAlchemy's
    expression-depth ceiling somewhere past a thousand messages, which made
    deleting a long conversation fail outright -- including one holding no
    images at all, which is the ordinary case. The statement here is the same
    size whatever the conversation contains.
    """
    return _origin_media_ids(
        session,
        MediaBinding.event_id.in_(
            select(ConversationEvent.event_id).where(
                ConversationEvent.conversation_id == conversation_id
            )
        ),
    )
