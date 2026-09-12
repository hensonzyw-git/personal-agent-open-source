"""The media object state machine: create, claim, seal, publish (§5.2, §5.3).

Every step here is a compare-and-swap, and the failures they exist to stop are
the ones a happy path cannot show:

- two writers both believing they own the object, which would give one media id
  two staging files and no rule about which one wins;
- a `complete` that reports success against a final file that was never
  installed, or a second `complete` that replaces bytes the first one already
  published;
- a `PUT` whose response was lost turning into a *second* attempt against a
  live claim rather than a takeover of an expired one;
- a sealed digest read back under the wrong AAD, which would hand one object
  another's hash -- the value the request fingerprint is built from.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.media.container import SealRecord
from personal_agent.media.lifecycle import (
    CompleteOutcome,
    MediaClaimConflictError,
    MediaLifecycleError,
    claim_upload,
    content_sha256,
    create_upload,
    publish_upload,
    seal_upload,
)
from personal_agent.media.locking import ensure_lock_files
from personal_agent.media.store import MediaStore
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import to_rfc3339


NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)
EXPIRES = NOW + timedelta(hours=1)
DIGEST = "a" * 64


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
    root = tmp_path / "media"
    for name in ("staging", "final", "quarantine", "locks"):
        (root / name).mkdir(parents=True, exist_ok=True)
    ensure_lock_files(root)
    return MediaStore(root, keyring)


@pytest.fixture()
def session(engine):
    session = session_factory(engine)()
    yield session
    session.close()


def _create(session, keyring, **overrides) -> str:
    kwargs = {
        "keyring": keyring,
        "device_id": "dev",
        "declared_mime": "image/jpeg",
        "declared_size": 8,
        "declared_sha256": DIGEST,
        "now": NOW,
        "expires_at": EXPIRES,
    }
    kwargs.update(overrides)
    media_id = create_upload(session, **kwargs)
    session.commit()
    return media_id


def _upload(
    session, store, keyring, media_id: str, *, body: bytes = b"sealed!!"
) -> SealRecord:
    """Drive an object to ``uploaded``, returning the seal record it produced."""
    attempt = claim_upload(
        session,
        media_id=media_id,
        device_id="dev",
        owner_token="owner-1",
        now=NOW,
        claim_deadline=LATER,
    )
    session.commit()
    seal = store.write_staging(media_id, attempt, [body])
    seal_upload(
        session,
        keyring=keyring,
        media_id=media_id,
        attempt_number=attempt,
        owner_token="owner-1",
        seal=seal,
        actual_mime="image/jpeg",
        content_size=len(body),
        now=NOW,
    )
    session.commit()
    return seal


def _state(engine, media_id: str) -> tuple[str, int]:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT state, state_version FROM media_objects WHERE media_id = :m"),
            {"m": media_id},
        ).one()


# --- create ---------------------------------------------------------------


def test_create_makes_a_pending_object_with_a_sealed_digest(session, keyring):
    media_id = _create(session, keyring)
    assert _state(session.get_bind(), media_id) == ("pending", 1)
    # The digest is readable only through the AAD-bound helper, and it is the
    # server-measured one that a fingerprint may use.
    assert content_sha256(session, keyring=keyring, media_id=media_id) is None
    envelope = session.execute(
        text(
            "SELECT encrypted_declared_sha256 FROM media_objects WHERE media_id = :m"
        ),
        {"m": media_id},
    ).scalar_one()
    assert DIGEST not in str(envelope), "the declared digest must not be in the clear"


def test_create_refuses_a_purpose_the_aad_does_not_cover(session, keyring):
    """The chunk AAD binds one role; a second purpose must not reuse it."""
    with pytest.raises(MediaLifecycleError):
        create_upload(
            session,
            keyring=keyring,
            device_id="dev",
            declared_mime="image/jpeg",
            declared_size=8,
            declared_sha256=None,
            now=NOW,
            expires_at=EXPIRES,
            purpose="avatar",
        )


# --- claim ----------------------------------------------------------------


def test_the_target_is_single_use(session, keyring):
    """§5.2: a `PUT` consumes the target; a second one does not get a target."""
    media_id = _create(session, keyring)
    attempt = claim_upload(
        session,
        media_id=media_id,
        device_id="dev",
        owner_token="owner-1",
        now=NOW,
        claim_deadline=LATER,
    )
    session.commit()
    assert attempt == 1

    with pytest.raises(MediaClaimConflictError):
        claim_upload(
            session,
            media_id=media_id,
            device_id="dev",
            owner_token="owner-2",
            now=NOW,
            claim_deadline=LATER,
        )
    session.rollback()


def test_an_expired_claim_is_taken_over_with_a_new_attempt(session, keyring):
    """§5.2: the deadline bounds *that attempt*; an expired one is superseded."""
    media_id = _create(session, keyring)
    claim_upload(
        session,
        media_id=media_id,
        device_id="dev",
        owner_token="owner-1",
        now=NOW,
        claim_deadline=LATER,
    )
    session.commit()

    attempt = claim_upload(
        session,
        media_id=media_id,
        device_id="dev",
        owner_token="owner-2",
        now=LATER + timedelta(seconds=1),
        claim_deadline=LATER + timedelta(minutes=5),
    )
    session.commit()
    assert attempt == 2


def test_another_device_cannot_claim_the_object(session, keyring):
    """The refusal is the same one a missing id gives, so it leaks nothing."""
    media_id = _create(session, keyring)
    with pytest.raises(MediaLifecycleError) as excinfo:
        claim_upload(
            session,
            media_id=media_id,
            device_id="someone-else",
            owner_token="owner-1",
            now=NOW,
            claim_deadline=LATER,
        )
    assert "no media object" in str(excinfo.value)
    session.rollback()


# --- seal -----------------------------------------------------------------


def test_sealing_records_the_seal_and_moves_to_uploaded(session, store, keyring, engine):
    media_id = _create(session, keyring)
    _upload(session, store, keyring, media_id)
    assert _state(engine, media_id)[0] == "uploaded"
    attempted, attempt, state = session.execute(
        text(
            "SELECT attempt_id, attempt_number, state FROM media_attempts "
            "WHERE media_id = :m"
        ),
        {"m": media_id},
    ).one()
    assert (attempt, state) == (1, "sealed")


def test_a_writer_that_lost_its_claim_cannot_seal(session, store, keyring):
    """The owner token is what stops a superseded writer completing the object."""
    media_id = _create(session, keyring)
    claim_upload(
        session,
        media_id=media_id,
        device_id="dev",
        owner_token="owner-1",
        now=NOW,
        claim_deadline=LATER,
    )
    session.commit()
    seal = SealRecord(chunk_count=1, total_bytes=8, sha256=DIGEST)
    with pytest.raises(MediaClaimConflictError):
        seal_upload(
            session,
            keyring=keyring,
            media_id=media_id,
            attempt_number=1,
            owner_token="owner-2",
            seal=seal,
            actual_mime="image/jpeg",
            content_size=8,
            now=NOW,
        )
    session.rollback()


def test_sealing_an_object_that_is_not_uploading_is_refused(session, store, keyring):
    media_id = _create(session, keyring)
    seal = SealRecord(chunk_count=1, total_bytes=8, sha256=DIGEST)
    with pytest.raises(MediaLifecycleError):
        seal_upload(
            session,
            keyring=keyring,
            media_id=media_id,
            attempt_number=1,
            owner_token="owner-1",
            seal=seal,
            actual_mime="image/jpeg",
            content_size=8,
            now=NOW,
        )
    session.rollback()


# --- publish --------------------------------------------------------------


def test_complete_publishes_and_seals_the_measured_digest(session, store, keyring, engine):
    media_id = _create(session, keyring)
    seal = _upload(session, store, keyring, media_id)

    outcome = publish_upload(
        session, media_id=media_id, device_id="dev", store=store, keyring=keyring, now=NOW
    )
    session.commit()

    assert outcome is CompleteOutcome.PUBLISHED
    assert _state(engine, media_id)[0] == "ready"
    assert store.final_exists(media_id)
    measured = content_sha256(session, keyring=keyring, media_id=media_id)
    # The *server's* measurement, not the client's declaration: §5.1 forbids
    # the declared digest from being treated as authoritative, and the request
    # fingerprint is built from this value.
    assert measured == seal.sha256
    assert measured != DIGEST


def test_a_second_complete_returns_the_same_result_without_republishing(
    session, store, keyring
):
    """§5.2: "complete 在 ready/bound 返回同一完成结果" -- a lost response."""
    media_id = _create(session, keyring)
    _upload(session, store, keyring, media_id)
    publish_upload(
        session, media_id=media_id, device_id="dev", store=store, keyring=keyring, now=NOW
    )
    session.commit()
    published = store.final_path(media_id).read_bytes()

    second = publish_upload(
        session,
        media_id=media_id,
        device_id="dev",
        store=store,
        keyring=keyring,
        now=LATER,
    )
    assert second is CompleteOutcome.ALREADY_READY
    assert store.final_path(media_id).read_bytes() == published


def test_complete_before_the_upload_is_sealed_reports_in_progress(
    session, store, keyring
):
    """§5.2: do not report an unfinished upload as a bad image."""
    media_id = _create(session, keyring)
    claim_upload(
        session,
        media_id=media_id,
        device_id="dev",
        owner_token="owner-1",
        now=NOW,
        claim_deadline=LATER,
    )
    session.commit()
    outcome = publish_upload(
        session, media_id=media_id, device_id="dev", store=store, keyring=keyring, now=NOW
    )
    session.rollback()
    assert outcome is CompleteOutcome.IN_PROGRESS


def test_complete_after_a_deletion_decided_returns_a_tombstone(
    session, store, keyring, engine
):
    """§5.2: "删除后返回墓碑，不复活"."""
    from personal_agent.media.deletion import mark_media_deleting

    media_id = _create(session, keyring)
    _upload(session, store, keyring, media_id)
    mark_media_deleting(session, media_id=media_id, keyring=keyring, now=NOW)
    session.commit()

    outcome = publish_upload(
        session, media_id=media_id, device_id="dev", store=store, keyring=keyring, now=NOW
    )
    assert outcome is CompleteOutcome.TOMBSTONED
    assert _state(engine, media_id)[0] == "deleting"
