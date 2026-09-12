"""The physical half of a media deletion: the reaper (design §6).

The decision is tested next door; this file is about the act that follows it.
§6 gives it four properties, and each one is a test below because each is
something a reasonable implementation gets wrong:

- it runs **under the lock**, and a lock it cannot take is a *deferral*, not a
  deletion -- "读者占锁超界只告警/延后，不能按租约到期强删", and "不把延后删报为
  已物理删除";
- it clears files **only** for an object someone decided to delete, so a bug
  that points it at a live object destroys nothing;
- it is **idempotent**: missing files, an already-``deleted`` row and a resumed
  ``reaping`` row all reach the same end state rather than erroring;
- the tombstone time is the **decision's**, not the reap's -- a reap that runs
  an hour later must not move the moment the image stopped being usable.

The deferral tests are the ones that matter most. A reaper that reports a
deferred deletion as complete is worse than one that fails loudly: the row says
``deleted``, the bytes are still there, and nothing will ever look again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.media.deletion import (
    ReapOutcome,
    mark_media_deleting,
    reap_media_object,
)
from personal_agent.media.locking import ensure_lock_files, media_locks
from personal_agent.media.store import MediaStore, MediaStoreError
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import to_rfc3339


NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
MEDIA_ID = "5c1f9a3e-2b74-4c8d-9f01-7a6e5d4c3b2a"


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    db.upgrade(engine, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev', 'phone', 'key', 'thumb', 'active', "
                "'[]', 'v1', :now)"
            ),
            {"now": to_rfc3339(NOW)},
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def store(tmp_path: Path, keyring: KeyRing) -> MediaStore:
    """A store over a real installation, lock files included.

    The lock set is created here rather than by the reaper: creating it is an
    installation step, and a reaper that conjured lock files on its way past
    would turn a misconfigured root into a working one.
    """
    root = tmp_path / "media"
    for name in ("staging", "final", "quarantine", "locks"):
        (root / name).mkdir(parents=True, exist_ok=True)
    ensure_lock_files(root)
    return MediaStore(root, keyring)


def _add_media(
    engine,
    *,
    media_id: str = MEDIA_ID,
    state: str = "bound",
    ready_at: datetime | None = NOW,
    deleted_at: datetime | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO media_objects (media_id, device_id, purpose, "
                "retention_class, state, state_version, ready_at, deleted_at, "
                "created_at, updated_at) VALUES (:mid, 'dev', 'chat_image', "
                "'timeline_media', :state, 1, :ready, :deleted, :now, :now)"
            ),
            {
                "mid": media_id,
                "state": state,
                "ready": to_rfc3339(ready_at) if ready_at else None,
                "deleted": to_rfc3339(deleted_at) if deleted_at else None,
                "now": to_rfc3339(NOW),
            },
        )


def _row(engine, media_id: str = MEDIA_ID) -> tuple[str, int, str | None]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT state, state_version, deleted_at FROM media_objects "
                "WHERE media_id = :mid"
            ),
            {"mid": media_id},
        ).one()


def _decide(
    engine, keyring: KeyRing, media_id: str = MEDIA_ID, *, now: datetime = NOW
) -> None:
    session = session_factory(engine)()
    try:
        mark_media_deleting(session, media_id=media_id, keyring=keyring, now=now)
        session.commit()
    finally:
        session.close()


def _publish(store: MediaStore, media_id: str = MEDIA_ID, *, body: bytes = b"sealed") -> Path:
    path = store.final_path(media_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _reap(engine, store: MediaStore, *, media_id: str = MEDIA_ID, now: datetime = LATER):
    session = session_factory(engine)()
    try:
        outcome = reap_media_object(session, store=store, media_id=media_id, now=now)
        session.commit()
        return outcome
    finally:
        session.close()


# --- the transition itself ------------------------------------------------


def test_the_reap_moves_a_decided_object_to_deleted(engine, store, keyring):
    """The whole path: decide, then reap, and the row ends ``deleted``."""
    _add_media(engine)
    final = _publish(store)
    _decide(engine, keyring)

    assert _row(engine)[0] == "deleting"
    assert _reap(engine, store) is ReapOutcome.REAPED
    state, version, _ = _row(engine)
    assert (state, version) == ("deleted", 4)
    assert not final.exists()


def test_the_version_marks_every_transition_of_the_reapers_two_step(engine, store, keyring):
    """1 → 2 → 3 → 4: one bump per state, and the reaper owns the last two.

    The two steps are separate on purpose. ``reaping`` is the state a crash
    leaves behind -- authorised, not yet removed -- so it has to be a
    distinguishable row, not a value passed through in memory.
    """
    _add_media(engine)
    _publish(store)
    assert _row(engine)[1] == 1
    _decide(engine, keyring)
    assert _row(engine)[1] == 2
    _reap(engine, store)
    assert _row(engine)[1] == 4


def test_the_tombstone_time_is_the_decisions_not_the_reaps(engine, store, keyring):
    """An hour between the two acts must not move when the image died.

    The tombstone is what a reader sees instead of the image, and §6 makes it a
    property of the decision -- "取消后续复用" starts there, not at removal. A
    reaper that re-stamped ``deleted_at`` would date the deletion to whenever
    the background job happened to run.
    """
    _add_media(engine)
    _publish(store)
    _decide(engine, keyring, now=NOW)
    _reap(engine, store, now=LATER)
    assert _row(engine)[2] == to_rfc3339(NOW)


# --- deferral: the failure mode that must never look like success ---------


def test_a_reaper_that_cannot_take_the_stripe_lock_defers(engine, store, keyring):
    """A reader holding the stripe defers the reap; nothing is removed.

    §6 forbids force-deleting past a reader on the strength of a lease, and
    forbids reporting the deferral as a completed deletion. So the object stays
    ``reaping`` -- deletion authorised, bytes not yet gone -- and the outcome
    says ``deferred`` rather than ``reaped``.
    """
    _add_media(engine)
    final = _publish(store)
    _decide(engine, keyring)

    with media_locks(store.roots.root, [MEDIA_ID]):
        assert _reap(engine, store) is ReapOutcome.DEFERRED

    state, version, _ = _row(engine)
    assert (state, version) == ("reaping", 3)
    assert final.exists()


def test_a_shared_storage_holder_does_not_defer_the_reaper(engine, store, keyring):
    """Which lock defers the reap is a fact worth pinning.

    The deferral above must come from the *stripe*, not from the storage lock:
    the reaper takes storage shared, exactly like a reader does, and two shared
    holders coexist by design -- that is what lets a reap run while other
    objects are being read. If locking.py ever made the storage lock exclusive
    this test fails, and the one above would still pass while silently testing
    the wrong thing.
    """
    _add_media(engine)
    final = _publish(store)
    _decide(engine, keyring)

    with media_locks(store.roots.root, []):
        assert _reap(engine, store) is ReapOutcome.REAPED

    assert not final.exists()


def test_a_deferred_reap_completes_once_the_reader_lets_go(engine, store, keyring):
    """A deferral is not terminal: the next pass finishes the job."""
    _add_media(engine)
    final = _publish(store)
    _decide(engine, keyring)

    with media_locks(store.roots.root, [MEDIA_ID]):
        _reap(engine, store)
    assert _row(engine)[0] == "reaping"

    assert _reap(engine, store) is ReapOutcome.REAPED
    assert _row(engine)[0] == "deleted"
    assert not final.exists()


def test_a_reap_resumes_from_reaping_without_a_second_stripe_bump(engine, store, keyring):
    """§5.3's "reaping 中断" row: re-enter, do not re-CAS the state you are in."""
    _add_media(engine, state="reaping")
    _publish(store)
    assert _reap(engine, store) is ReapOutcome.REAPED
    assert _row(engine) == ("deleted", 2, None)


# --- idempotence ----------------------------------------------------------


def test_missing_bytes_are_a_completed_reap(engine, store, keyring):
    """§5.3: "缺文件视为已完成". A crash after unlink must not wedge the row."""
    _add_media(engine)
    _decide(engine, keyring)
    assert _reap(engine, store) is ReapOutcome.REAPED
    assert _row(engine)[0] == "deleted"


def test_an_already_deleted_object_is_left_alone(engine, store, keyring):
    """A second reap is a no-op, and does not move the version."""
    _add_media(engine, state="deleted", deleted_at=NOW)
    assert _reap(engine, store) is ReapOutcome.ALREADY_DELETED
    assert _row(engine) == ("deleted", 1, to_rfc3339(NOW))


def test_an_object_that_never_existed_is_absent(engine, store, keyring):
    assert _reap(engine, store) is ReapOutcome.ABSENT


# --- fail-closed refusals -------------------------------------------------


def test_a_live_object_is_refused_and_keeps_its_bytes(engine, store, keyring):
    """No decision, no destruction.

    The reaper's precondition is a committed ``deleting``/``reaping``, and this
    is the test that stops a caller with a bug from pointing it at a healthy
    image. The refusal is an error rather than an outcome value: a live object
    reaching the reaper is a programming or operational fault, and reporting it
    as a routine result would invite a caller to ignore it.
    """
    _add_media(engine, state="bound")
    final = _publish(store)
    with pytest.raises(Exception) as excinfo:
        _reap(engine, store)
    assert "bound" in str(excinfo.value)
    assert _row(engine)[0] == "bound"
    assert final.exists()


def test_a_foreign_file_at_the_final_path_is_refused_not_unlinked(engine, store, keyring):
    """A symlink where the image should be is a refusal, not a delete target."""
    _add_media(engine)
    _decide(engine, keyring)
    target = store.final_path(MEDIA_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = store.roots.root / "not-ours.bin"
    elsewhere.write_bytes(b"somebody else's bytes")
    target.symlink_to(elsewhere)

    with pytest.raises(MediaStoreError):
        _reap(engine, store)

    assert elsewhere.exists()
    assert _row(engine)[0] == "reaping"
