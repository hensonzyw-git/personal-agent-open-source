"""The five media endpoints' logic (design §4.2, §5.2, §5.4).

Written refusal-first, as §5.1 requires of anything sitting on a boundary an
untrusted client reaches. The happy path is one test per endpoint; everything
else is a way a client can be wrong, and each one has a stated answer rather
than an incidental one:

- a declaration the server already knows it cannot honour (§4.3: the client's
  declared values are checked against the ceiling even though they are not
  measurements);
- a key that names a second, different request (the idempotency column is a
  constraint, not a hint);
- bytes that disagree with their declared magic type -- rejected *before* a
  staging file exists (§5.4: "探测失败一律拒绝且不派生文件");
- a body that is longer than declared (a false declaration, so the object is
  rejected) versus shorter (a dropped connection, so the object is left alone);
- a second `PUT` against a live claim, and against an expired one (§5.2);
- a sealed stream whose digest disagrees with the declaration, found only at
  `complete` (§5.1's "声明与实际不符即整对象拒绝");
- a reader asking for a tombstone.

The store is real, the database is real, and the lock is the real stripe lock.
Nothing here is a fake, because a fake built from the same assumptions as the
code can only ever confirm them (§5.1).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.media.lifecycle import claim_upload
from personal_agent.media.locking import ensure_lock_files, media_locks
from personal_agent.media.store import MediaStore
from personal_agent.media.uploads import (
    CompleteOutcome,
    MediaBusyError,
    MediaError,
    MediaIncompleteUploadError,
    MediaLimits,
    MediaRejectedError,
    UploadDeclaration,
    complete_upload,
    delete_media,
    read_media,
    receive_upload,
    start_upload,
    state_of,
)
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import to_rfc3339

NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)

#: A header the registry names, and enough body to be a plausible image. The
#: bytes after the three magic ones are arbitrary: under option 1 the server
#: never decodes the image, so the only thing that can be wrong about them is
#: their length and their agreement with the declaration.
JPEG = b"\xff\xd8\xff"
PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

LIMITS = MediaLimits(
    max_content_bytes=64,
    max_dimension=4096,
    allowed_mimes=frozenset({"image/jpeg"}),
    target_ttl=timedelta(minutes=30),
    claim_ttl=timedelta(minutes=10),
    retention_ttl=timedelta(days=7),
)


def jpeg(size: int) -> bytes:
    return JPEG + b"\x00" * (size - len(JPEG))


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


def digest_of(body: bytes) -> str:
    """The digest an honest client declares, which `complete` will check.

    The happy-path helper, and the reason it exists rather than a constant: the
    server measures the stream and compares, so a fixture that declared a fixed
    fake digest would be testing the *refusal* path in every test that meant to
    test the publish path -- which is precisely what the first draft of this
    file did.
    """
    return hashlib.sha256(body).hexdigest()


def _declaration(body: bytes = b"", **overrides) -> UploadDeclaration:
    body = body or jpeg(16)
    kwargs = {
        "mime": "image/jpeg",
        "size": len(body),
        "sha256": digest_of(body),
        "width": 100,
        "height": 200,
    }
    kwargs.update(overrides)
    return UploadDeclaration(**kwargs)


def _start(session, keyring, *, key="key-1", declaration=None, media_id=None):
    created = start_upload(
        session,
        keyring=keyring,
        device_id="dev",
        client_request_id=key,
        declaration=declaration or _declaration(),
        limits=LIMITS,
        now=NOW,
        media_id=media_id,
    )
    session.commit()
    return created


def _receive(session, store, keyring, media_id, body, *, device_id="dev", now=NOW):
    return receive_upload(
        session,
        store=store,
        keyring=keyring,
        media_id=media_id,
        device_id=device_id,
        body=body,
        limits=LIMITS,
        now=now,
    )


def _entry_count(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT COUNT(*) FROM deletion_manifest")
        ).scalar_one()


# --- create: the declaration is checked at the door -----------------------


def test_a_size_over_the_ceiling_is_refused_before_a_row_exists(session, keyring, engine):
    """§4.3's "客户端声明值仍按上限校验（超出即拒绝）", applied at create."""
    with pytest.raises(MediaError):
        start_upload(
            session,
            keyring=keyring,
            device_id="dev",
            client_request_id="key-1",
            declaration=_declaration(size=LIMITS.max_content_bytes + 1),
            limits=LIMITS,
            now=NOW,
        )
    session.rollback()
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM media_objects")
        ).scalar_one() == 0


def test_a_declared_mime_outside_the_allowlist_is_refused(session, keyring):
    """The registry knows PNG; this round's configuration does not accept it."""
    with pytest.raises(MediaError):
        start_upload(
            session,
            keyring=keyring,
            device_id="dev",
            client_request_id="key-1",
            declaration=_declaration(mime="image/png"),
            limits=LIMITS,
            now=NOW,
        )
    session.rollback()


def test_a_dimension_over_the_ceiling_is_refused(session, keyring):
    """The check is on the declaration and claims nothing about the pixels."""
    with pytest.raises(MediaError):
        start_upload(
            session,
            keyring=keyring,
            device_id="dev",
            client_request_id="key-1",
            declaration=_declaration(width=LIMITS.max_dimension + 1),
            limits=LIMITS,
            now=NOW,
        )
    session.rollback()


def test_a_declared_digest_that_is_not_a_sha256_is_refused(session, keyring):
    with pytest.raises(MediaError):
        start_upload(
            session,
            keyring=keyring,
            device_id="dev",
            client_request_id="key-1",
            declaration=_declaration(sha256="not-a-digest"),
            limits=LIMITS,
            now=NOW,
        )
    session.rollback()


def test_the_declared_digest_is_sealed_not_stored_in_the_clear(session, keyring, engine):
    created = _start(session, keyring)
    with engine.connect() as connection:
        envelope = connection.execute(
            text(
                "SELECT encrypted_declared_sha256 FROM media_objects "
                "WHERE media_id = :m"
            ),
            {"m": created.media_id},
        ).scalar_one()
    assert digest_of(jpeg(16)) not in str(envelope)


# --- create: idempotency --------------------------------------------------


def test_the_same_key_returns_the_object_it_already_made(session, keyring):
    """A lost response must be recoverable by re-asking, not by guessing an id."""
    first = _start(session, keyring)
    second = _start(session, keyring)
    assert second.media_id == first.media_id
    assert second.replayed is True
    assert first.replayed is False


def test_the_same_key_with_a_different_declaration_is_refused(session, keyring):
    """One key, one request. The alternative is a second object under one key."""
    _start(session, keyring)
    with pytest.raises(MediaError):
        _start(session, keyring, declaration=_declaration(size=32))
    session.rollback()


def test_two_keys_make_two_objects(session, keyring):
    first = _start(session, keyring, key="key-1")
    second = _start(session, keyring, key="key-2")
    assert first.media_id != second.media_id


def test_another_devices_key_does_not_collide(session, keyring, engine):
    """The constraint is per device, which is what `api_requests` uses too."""
    created = _start(session, keyring)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev2', 'tablet', 'key2', 'thumb2', 'active', "
                "'[]', 'v1', :now)"
            ),
            {"now": to_rfc3339(NOW)},
        )
    other = start_upload(
        session,
        keyring=keyring,
        device_id="dev2",
        client_request_id="key-1",
        declaration=_declaration(),
        limits=LIMITS,
        now=NOW,
    )
    session.commit()
    assert other.media_id != created.media_id


# --- upload: the refusals -------------------------------------------------


def test_a_body_whose_magic_contradicts_the_declaration_rejects_the_object(
    session, store, keyring, engine
):
    """§5.4's "整对象拒绝", and no file derived from the refused bytes."""
    created = _start(session, keyring)
    # Sixteen bytes of PNG header against a sixteen-byte JPEG declaration:
    # the length agrees, so this is the probe refusing, not the size check.
    body = (PNG_HEADER + b"\x00" * 32)[:16]
    with pytest.raises(MediaRejectedError):
        _receive(session, store, keyring, created.media_id, body)
    assert state_of(session, created.media_id) == "rejected"
    assert not store.staging_path(created.media_id, 1).exists()


def test_a_body_longer_than_declared_rejects_the_object(session, store, keyring):
    """A client that sends more than it declared has declared something untrue."""
    created = _start(session, keyring, declaration=_declaration(size=16))
    with pytest.raises(MediaRejectedError):
        _receive(session, store, keyring, created.media_id, jpeg(17))
    assert state_of(session, created.media_id) == "rejected"


def test_a_body_shorter_than_declared_leaves_the_target_usable(session, store, keyring):
    """The dropped-connection case: refuse the byte stream, not the object."""
    created = _start(session, keyring, declaration=_declaration(size=16))
    with pytest.raises(MediaIncompleteUploadError):
        _receive(session, store, keyring, created.media_id, jpeg(8))
    assert state_of(session, created.media_id) == "pending"

    receipt = _receive(session, store, keyring, created.media_id, jpeg(16))
    assert receipt.state == "uploaded"


def test_another_device_cannot_upload_to_the_object(session, store, keyring):
    created = _start(session, keyring)
    with pytest.raises(MediaError) as excinfo:
        _receive(session, store, keyring, created.media_id, jpeg(16), device_id="dev2")
    assert "no such media object" in str(excinfo.value)
    assert state_of(session, created.media_id) == "pending"


def test_a_live_claim_refuses_a_second_upload(session, store, keyring):
    """§5.2: "第二次 PUT 对 live claim 拒绝"."""
    created = _start(session, keyring)
    _receive(session, store, keyring, created.media_id, jpeg(16))
    with pytest.raises(MediaError):
        _receive(session, store, keyring, created.media_id, jpeg(16))
    # The refusal left the sealed upload intact rather than replacing it.
    assert state_of(session, created.media_id) == "uploaded"


def test_a_busy_stripe_defers_the_upload_instead_of_waiting(session, store, keyring):
    """§6's bounded grace, from the writer's side: refuse, do not queue."""
    created = _start(session, keyring)
    with media_locks(store.roots.root, [created.media_id]):
        with pytest.raises(MediaBusyError):
            _receive(session, store, keyring, created.media_id, jpeg(16))
    assert state_of(session, created.media_id) == "pending"


def test_an_expired_claim_is_taken_over_with_a_new_attempt(session, store, keyring):
    """§5.2: the deadline bounds *that attempt*, not the object.

    The state this recovers from is a `PUT` that claimed the target and then
    died before sealing, so the object sits in `uploading` with nobody writing
    to it. Reached here through the real `claim_upload` rather than by editing
    the row, because an object that already sealed is a *different* state and
    must not be re-uploadable -- which is what the sibling test above pins.
    """
    created = _start(session, keyring)
    claim_upload(
        session,
        media_id=created.media_id,
        device_id="dev",
        owner_token="a-writer-that-never-came-back",
        now=NOW,
        claim_deadline=NOW + timedelta(seconds=30),
    )
    session.commit()
    assert state_of(session, created.media_id) == "uploading"

    receipt = _receive(
        session, store, keyring, created.media_id, jpeg(16),
        now=NOW + timedelta(seconds=31),
    )
    assert receipt.state == "uploaded"
    attempts = session.execute(
        text("SELECT attempt_number, state FROM media_attempts WHERE media_id = :m "
             "ORDER BY attempt_number"),
        {"m": created.media_id},
    ).all()
    # The abandoned attempt survives rather than being overwritten: it owns the
    # staging bytes the cleanup path has to find.
    assert attempts == [(1, "claimed"), (2, "sealed")]


# --- upload: the happy path -----------------------------------------------


def test_receiving_seals_the_bytes_and_moves_to_uploaded(session, store, keyring):
    created = _start(session, keyring)
    receipt = _receive(session, store, keyring, created.media_id, jpeg(16))
    assert receipt.state == "uploaded"
    assert receipt.mime == "image/jpeg"
    assert receipt.size == 16
    assert store.staging_path(created.media_id, 1).exists()


# --- complete -------------------------------------------------------------


def _uploaded(session, store, keyring, *, size=16, **overrides):
    body = jpeg(size)
    created = _start(session, keyring, declaration=_declaration(body, **overrides))
    _receive(session, store, keyring, created.media_id, body)
    return created


def test_complete_publishes_and_reports_the_measured_hash(session, store, keyring):
    created = _uploaded(session, store, keyring)
    result = complete_upload(
        session,
        store=store,
        keyring=keyring,
        media_id=created.media_id,
        device_id="dev",
        limits=LIMITS,
        now=NOW,
    )
    assert result.outcome is CompleteOutcome.PUBLISHED
    assert result.state == "ready"
    assert store.final_exists(created.media_id)
    # The server's measurement of the bytes it received -- the same value an
    # honest client declared, and the one §5.1 forbids taking from the client.
    # `test_a_sealed_digest_that_disagrees_with_the_declaration_rejects` is the
    # test that keeps the two from being conflated: there the declaration lies
    # and the value returned is nothing at all.
    assert result.content_sha256 == digest_of(jpeg(16))
    # Dimensions travel as declared, which is why the columns say so.
    assert (result.declared_width, result.declared_height) == (100, 200)


def test_a_sealed_digest_that_disagrees_with_the_declaration_rejects(
    session, store, keyring, engine
):
    """§5.1: "声明与实际不符即整对象拒绝", found here because only the
    sealed record knows what actually arrived."""
    created = _uploaded(session, store, keyring)
    # Forge a wrong declaration on the row: the bytes that arrived hash to
    # something else, and `complete` is the first place that can notice -- the
    # probe only ever saw a MIME, and the seal is what the server measured.
    from sqlalchemy import update as sa_update

    from personal_agent.media.lifecycle import DECLARED_SHA_COLUMN, _seal
    from personal_agent.storage.models import MediaObject

    session.execute(
        sa_update(MediaObject)
        .where(MediaObject.media_id == created.media_id)
        .values(
            encrypted_declared_sha256=_seal(
                keyring, "b" * 64, created.media_id, DECLARED_SHA_COLUMN
            )
        )
    )
    session.commit()
    session.expire_all()
    with pytest.raises(MediaRejectedError):
        complete_upload(
            session,
            store=store,
            keyring=keyring,
            media_id=created.media_id,
            device_id="dev",
            limits=LIMITS,
            now=NOW,
        )
    assert state_of(session, created.media_id) == "rejected"
    assert not store.final_exists(created.media_id)


def test_a_second_complete_returns_the_same_result(session, store, keyring):
    """§5.2: "complete 在 ready/bound 返回同一完成结果"."""
    created = _uploaded(session, store, keyring)
    first = complete_upload(
        session, store=store, keyring=keyring, media_id=created.media_id,
        device_id="dev", limits=LIMITS, now=NOW,
    )
    second = complete_upload(
        session, store=store, keyring=keyring, media_id=created.media_id,
        device_id="dev", limits=LIMITS, now=LATER,
    )
    assert second.outcome is CompleteOutcome.ALREADY_READY
    assert second.content_sha256 == first.content_sha256


def test_complete_before_the_upload_reports_progress(session, store, keyring):
    """§5.2: do not report an unfinished upload as a bad image."""
    created = _start(session, keyring)
    result = complete_upload(
        session, store=store, keyring=keyring, media_id=created.media_id,
        device_id="dev", limits=LIMITS, now=NOW,
    )
    assert result.outcome is CompleteOutcome.IN_PROGRESS
    assert result.state == "pending"


# --- fetch ----------------------------------------------------------------


def _ready(session, store, keyring, **overrides):
    created = _uploaded(session, store, keyring, **overrides)
    complete_upload(
        session, store=store, keyring=keyring, media_id=created.media_id,
        device_id="dev", limits=LIMITS, now=NOW,
    )
    return created


def test_fetch_returns_the_published_bytes(session, store, keyring):
    created = _ready(session, store, keyring)
    fetched = read_media(
        session, store=store, keyring=keyring, media_id=created.media_id,
        device_id="dev",
    )
    assert fetched.body == jpeg(16)
    assert fetched.mime == "image/jpeg"


def test_fetch_refuses_another_device(session, store, keyring):
    created = _ready(session, store, keyring)
    with pytest.raises(MediaError) as excinfo:
        read_media(
            session, store=store, keyring=keyring, media_id=created.media_id,
            device_id="dev2",
        )
    assert "no such media object" in str(excinfo.value)


def test_fetch_refuses_an_image_that_is_not_ready(session, store, keyring):
    created = _start(session, keyring)
    with pytest.raises(MediaError):
        read_media(
            session, store=store, keyring=keyring, media_id=created.media_id,
            device_id="dev",
        )


def test_fetch_refuses_a_tombstone_and_says_why(session, store, keyring):
    """§5.2: a tombstone is not an absence, and the difference is actionable."""
    created = _ready(session, store, keyring)
    delete_media(
        session, keyring=keyring, media_id=created.media_id, device_id="dev", now=NOW
    )
    with pytest.raises(MediaError) as excinfo:
        read_media(
            session, store=store, keyring=keyring, media_id=created.media_id,
            device_id="dev",
        )
    assert "deleted" in str(excinfo.value)


def test_a_busy_stripe_defers_a_read(session, store, keyring):
    created = _ready(session, store, keyring)
    with media_locks(store.roots.root, [created.media_id]):
        with pytest.raises(MediaBusyError):
            read_media(
                session, store=store, keyring=keyring, media_id=created.media_id,
                device_id="dev",
            )


# --- delete ---------------------------------------------------------------


def test_delete_decides_once_and_says_so_on_a_repeat(session, store, keyring):
    """§5.2: "幂等删除标记与 manifest 同事务"."""
    created = _ready(session, store, keyring)
    assert (
        delete_media(
            session, keyring=keyring, media_id=created.media_id, device_id="dev", now=NOW
        )
        is True
    )
    assert state_of(session, created.media_id) == "deleting"
    assert (
        delete_media(
            session, keyring=keyring, media_id=created.media_id, device_id="dev",
            now=LATER,
        )
        is False
    )
    assert state_of(session, created.media_id) == "deleting"


def test_delete_writes_exactly_one_manifest_entry(session, store, keyring, engine):
    """A second entry would make one deletion two, and `backup_expiry_after`
    would then have to expire both."""
    created = _ready(session, store, keyring)
    delete_media(
        session, keyring=keyring, media_id=created.media_id, device_id="dev", now=NOW
    )
    delete_media(
        session, keyring=keyring, media_id=created.media_id, device_id="dev", now=LATER
    )
    assert _entry_count(engine) == 1


def test_delete_refuses_another_device(session, store, keyring, engine):
    created = _ready(session, store, keyring)
    with pytest.raises(MediaError):
        delete_media(
            session, keyring=keyring, media_id=created.media_id, device_id="dev2",
            now=NOW,
        )
    assert state_of(session, created.media_id) == "ready"
    assert _entry_count(engine) == 0
