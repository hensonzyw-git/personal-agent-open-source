"""The publish protocol, and every way it is asked to overwrite something.

Multimodal design §5.3. The store's whole job is to make three properties true
that a naive implementation would quietly break: a persisted image is never
replaced by a later write, a file's existence never stands in for a committed
database row, and a published file can always be traced back to the attempt
that produced it.

The tests below are written around the design's crash matrix rather than
around the happy path, because the happy path here is two syscalls and proves
nothing about a crash between them.
"""

from __future__ import annotations

import os

import pytest

from personal_agent.media.container import (
    ContainerError,
    SealRecord,
    read_container,
    write_container,
)
from personal_agent.media.locking import ensure_lock_files, verify_lock_installation
from personal_agent.media.store import (
    FinalOutcome,
    MediaIntegrityError,
    MediaStore,
    MediaStoreError,
    probe_non_overwriting_publish,
    verify_media_installation,
)
from personal_agent_core.crypto import KeyRing, generate_key

CHUNK = 64
MEDIA = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing([generate_key("k1")], service="personal_agent")


@pytest.fixture()
def store(tmp_path, keyring) -> MediaStore:
    store = MediaStore(tmp_path / "media", keyring, chunk_bytes=CHUNK)
    store.prepare_directories()
    return store


def seal(store, media_id, attempt, payload):
    return store.write_staging(
        media_id, attempt, [payload[i : i + CHUNK] for i in range(0, len(payload), CHUNK)]
    )


# --- installation verification ---------------------------------------------


def test_the_publish_probe_passes_on_a_real_filesystem(tmp_path) -> None:
    # §5.3 requires the non-overwriting install to be environment-verified. On
    # a filesystem where this raised, publishing could clobber an image, and no
    # amount of application logic would notice.
    (tmp_path / "staging").mkdir()
    probe_non_overwriting_publish(tmp_path)
    assert not (tmp_path / "staging" / ".publish-probe").exists()


def test_the_probe_leaves_no_residue_even_when_it_fails(tmp_path, monkeypatch) -> None:
    def clobbering_link(source, target):
        raise OSError("pretend this host has no link")

    monkeypatch.setattr(os, "link", clobbering_link)
    (tmp_path / "staging").mkdir()
    with pytest.raises(MediaStoreError):
        probe_non_overwriting_publish(tmp_path)
    assert not (tmp_path / "staging" / ".publish-probe").exists()


def test_installation_verification_names_the_missing_lock_set(tmp_path) -> None:
    problems = verify_media_installation(tmp_path)
    assert problems, "an uninstalled root must not verify as usable"
    assert any("lock" in problem for problem in problems)


def _install(root, monkeypatch) -> None:
    """Build a root the lock check can accept, and say why each step is needed.

    Every directory in the chain has to be neither writable by the running
    identity nor *owned* by it -- an owner can chmod a read-only directory back
    to writable and then rename what is inside it. A test cannot drop its own
    ownership of a temporary directory, so the running identity is presented as
    a foreign one, which is the same lever the check uses.
    """
    ensure_lock_files(root)
    (root / "staging").mkdir(exist_ok=True)
    os.chmod(root / "locks", 0o555)
    os.chmod(root, 0o555)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(root).st_uid + 1)


def test_lock_verification_passes_for_an_installed_root(tmp_path, monkeypatch) -> None:
    """The positive case, which needs every directory in the chain protected."""
    root = tmp_path / "storage"
    root.mkdir()
    _install(root, monkeypatch)
    try:
        assert verify_lock_installation(root, boundary=tmp_path) == []
    finally:
        os.chmod(root, 0o755)


def test_a_writable_parent_that_could_swap_the_lock_directory_is_reported(
    tmp_path, monkeypatch
) -> None:
    """The repro: `locks/` is read-only, but its parent is not.

    Renaming the directory and creating a fresh one hands two writers different
    inodes for the same lock name, and both then believe they hold it. Checking
    only the lock directory cannot see this, which is why the check walks up.
    """
    root = tmp_path / "storage"
    root.mkdir()
    _install(root, monkeypatch)
    os.chmod(root, 0o755)  # the service can rename `locks/` again

    try:
        problems = verify_lock_installation(root, boundary=tmp_path)
        assert any("storage" in problem for problem in problems)
    finally:
        os.chmod(root, 0o555)


def test_a_lock_directory_owned_by_the_running_identity_is_reported(
    tmp_path, monkeypatch
) -> None:
    """Read-only is reversible by whoever owns the directory.

    Mode 0o555 only stops a *non-owner*; the owner chmods it and unlinks. So the
    ownership is a finding on its own, not a strengthening of the mode check.
    """
    root = tmp_path / "storage"
    root.mkdir()
    ensure_lock_files(root)
    os.chmod(root / "locks", 0o555)
    os.chmod(root, 0o555)
    # The running identity owns everything here, which is the default in tests.
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(root).st_uid)

    try:
        problems = verify_lock_installation(root, boundary=tmp_path)
        assert any("owned" in problem for problem in problems)
    finally:
        os.chmod(root, 0o755)


def test_the_walk_stops_at_the_boundary_the_deployment_declares(
    tmp_path, monkeypatch
) -> None:
    """Everything above the boundary is the deployment's claim, not our check.

    The chain has to be examined hop by hop -- each hop is what stops the next
    directory down from being renamed -- but a caller that owns the region above
    its storage root says so explicitly rather than having the check guess.
    """
    root = tmp_path / "storage"
    root.mkdir()
    _install(root, monkeypatch)
    try:
        # The lock half only: the publish probe wants a writable root, which is
        # the disagreement the test below pins.
        assert verify_lock_installation(root, boundary=tmp_path) == []
    finally:
        os.chmod(root, 0o755)


def test_a_read_only_root_is_reported_by_the_publish_probe_not_raised(
    tmp_path,
) -> None:
    """A verifier reports; it does not crash.

    The probe creates its scratch directory under the root, so a root this
    identity cannot write raised `PermissionError` straight out of the check
    that exists to name exactly that condition.
    """
    root = tmp_path / "storage"
    root.mkdir()
    os.chmod(root, 0o555)
    try:
        with pytest.raises(MediaStoreError, match="publish probe"):
            probe_non_overwriting_publish(root)
    finally:
        os.chmod(root, 0o755)


def test_the_whole_installation_verifies_once_the_probe_runs_in_staging(
    tmp_path, monkeypatch
) -> None:
    """Both halves agree, which they could not while the probe used the root.

    Renaming `locks/` is a write on `root`, so the lock check needs `root`
    unwritable; the probe needs a writable directory of its own. Moving the
    probe into the staging area -- which the service writes anyway, and which is
    on the same filesystem as the published files -- lets a correctly installed
    root satisfy both. This is the case that was impossible before.
    """
    root = tmp_path / "storage"
    root.mkdir()
    _install(root, monkeypatch)
    try:
        assert verify_media_installation(root, boundary=tmp_path) == []
    finally:
        os.chmod(root, 0o755)


def test_the_probe_reports_a_missing_staging_area_rather_than_creating_it(
    tmp_path,
) -> None:
    """Installation owns the layout; the verifier only reports on it.

    Creating the staging directory here would make the check pass by mutating
    the thing it is checking, and it would need exactly the write permission on
    `root` that the lock check forbids.
    """
    root = tmp_path / "storage"
    root.mkdir()

    with pytest.raises(MediaStoreError, match="does not exist"):
        probe_non_overwriting_publish(root)


def test_verify_installation_fails_closed(tmp_path, keyring) -> None:
    store = MediaStore(tmp_path / "media", keyring)
    with pytest.raises(MediaStoreError):
        store.verify_installation()


# --- paths ------------------------------------------------------------------


def test_a_path_component_is_never_a_client_string(store) -> None:
    with pytest.raises(MediaStoreError):
        store.staging_path("../../etc/passwd", 1)
    with pytest.raises(MediaStoreError):
        store.final_path("not-a-uuid")
    with pytest.raises(MediaStoreError):
        store.quarantine_path("../escape")


def test_attempt_numbers_must_be_positive_integers(store) -> None:
    for bad in (0, -1, True, "1", 1.0):
        with pytest.raises(MediaStoreError):
            store.staging_path(MEDIA, bad)


# --- writing ----------------------------------------------------------------


def test_a_sealed_staging_file_reads_back(store, keyring) -> None:
    payload = b"image bytes" * 12
    record = seal(store, MEDIA, 1, payload)
    assert store.read_staging(MEDIA, 1, record) == payload


def test_over_ceiling_upload_removes_its_staging_file(store) -> None:
    # §4.2: the ceiling is counted against what actually arrives, and a batch
    # that passes it is not left partially on disk for anything to adopt.
    with pytest.raises(ContainerError):
        store.write_staging(MEDIA, 1, [b"x" * 100], max_bytes=50)
    assert not store.staging_path(MEDIA, 1).exists()
    assert not store.staging_path(MEDIA, 1).parent.exists()


def test_a_refused_second_write_does_not_destroy_the_first(store) -> None:
    # The staging file is O_EXCL, so re-PUTting an attempt is refused. The
    # refusal must not clean up the directory it does not own: the file it
    # refuses to overwrite is a legitimate sealed upload.
    record = seal(store, MEDIA, 1, b"first" * 4)
    with pytest.raises(ContainerError):
        seal(store, MEDIA, 1, b"second" * 4)
    assert store.read_staging(MEDIA, 1, record) == b"first" * 4


def test_the_store_drives_an_incremental_upload(store) -> None:
    # The path the PUT endpoint takes: open, append per batch, seal, publish.
    payload = b"streamed in three batches" * 4
    writer = store.staging_writer(MEDIA, 1)
    writer.open()
    for index in range(0, len(payload), CHUNK):
        writer.append(payload[index : index + CHUNK])
    record = writer.seal()

    assert store.read_staging(MEDIA, 1, record) == payload
    store.publish(MEDIA, 1)
    assert store.read_final(MEDIA, 1, record) == payload


def test_the_store_hands_back_an_unopened_writer(store) -> None:
    # Opening is the caller's act, done only after the attempt has been
    # re-checked inside the lock. A writer that had already created the file
    # would have written before anyone established that it may.
    writer = store.staging_writer(MEDIA, 1)
    assert not store.staging_path(MEDIA, 1).exists()
    with pytest.raises(ContainerError):
        writer.append(b"x")


def test_a_later_attempt_gets_its_own_staging_file(store) -> None:
    # §4.2's retry: a failed attempt does not block the next one, and the two
    # do not share bytes.
    seal(store, MEDIA, 1, b"one" * 4)
    assert store.discard_staging(MEDIA, 1) is True
    second = seal(store, MEDIA, 2, b"two" * 4)
    assert store.read_staging(MEDIA, 2, second) == b"two" * 4


# --- publishing -------------------------------------------------------------


def test_publish_installs_the_bytes_and_removes_staging(store, keyring) -> None:
    payload = b"persisted" * 8
    record = seal(store, MEDIA, 1, payload)

    store.publish(MEDIA, 1)

    assert store.final_exists(MEDIA)
    assert not store.staging_path(MEDIA, 1).exists()
    assert store.read_final(MEDIA, 1, record) == payload


def test_publish_installs_the_same_inode_rather_than_a_copy(store) -> None:
    # Same bytes is not the property; same inode is. A copy would mean the
    # published file could diverge from the sealed one without either write
    # being wrong.
    seal(store, MEDIA, 1, b"bytes" * 4)
    staging_inode = os.stat(store.staging_path(MEDIA, 1)).st_ino

    store.publish(MEDIA, 1)

    assert os.stat(store.final_path(MEDIA)).st_ino == staging_inode


def test_publish_refuses_to_replace_an_existing_image(store) -> None:
    # The property the whole protocol exists for. A later attempt must not be
    # able to replace an image that is already published.
    first = b"the original image" * 4
    first_seal = seal(store, MEDIA, 1, first)
    store.publish(MEDIA, 1)

    seal(store, MEDIA, 2, b"a completely different image" * 4)
    with pytest.raises(MediaStoreError):
        store.publish(MEDIA, 2)

    assert store.read_final(MEDIA, 1, first_seal) == first


def test_publish_without_staging_is_refused(store) -> None:
    with pytest.raises(MediaStoreError):
        store.publish(MEDIA, 1)


def test_publish_refuses_a_symlinked_staging_file(store, tmp_path) -> None:
    real = tmp_path / "elsewhere.bin"
    real.write_bytes(b"not ours")
    link = store.staging_path(MEDIA, 1)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)

    with pytest.raises(MediaStoreError):
        store.publish(MEDIA, 1)
    assert not store.final_exists(MEDIA)


# --- reading a published file back ------------------------------------------


def test_a_published_file_is_read_by_the_attempt_that_produced_it(store) -> None:
    # Each chunk's AAD binds the attempt number, so a store that assumed the
    # first attempt would work for every initial upload and fail for every
    # retry. This is that retry.
    payload = b"retried upload" * 6
    seal(store, MEDIA, 1, b"the first try")
    store.publish(MEDIA, 1)
    store.discard_final(MEDIA)
    record = seal(store, MEDIA, 3, payload)
    store.publish(MEDIA, 3)

    assert store.read_final(MEDIA, 3, record) == payload
    with pytest.raises(ContainerError):
        store.read_final(MEDIA, 1, record)


def test_verify_ready_final_reports_an_integrity_failure(store) -> None:
    # §5.3: a ready object whose file is missing or does not authenticate is an
    # integrity failure, refused and alerted -- not silently regenerated.
    record = seal(store, MEDIA, 1, b"here" * 4)
    with pytest.raises(MediaIntegrityError):
        store.verify_ready_final(MEDIA, 1, record)


# --- recovery ---------------------------------------------------------------


def test_recovery_reports_absent_when_nothing_was_persisted(store) -> None:
    record = seal(store, MEDIA, 1, b"x" * 8)
    assert store.recover_final(MEDIA, 1, record) is FinalOutcome.ABSENT


def test_recovery_adopts_a_persisted_file_that_matches_its_seal(store) -> None:
    # The "final persisted, database not committed" crash row: the bytes are
    # good, so the object is adopted rather than re-uploaded.
    payload = b"survived the crash" * 4
    record = seal(store, MEDIA, 1, payload)
    store.publish(MEDIA, 1)

    assert store.recover_final(MEDIA, 1, record) is FinalOutcome.ADOPTED
    assert store.read_final(MEDIA, 1, record) == payload


def test_recovery_quarantines_a_persisted_file_that_does_not_match(store) -> None:
    # The dangerous case: there is a file, it decrypts, and it is not the image
    # the seal describes. Adopting it would publish the wrong photo; deleting
    # it would destroy the only copy. Quarantine keeps both options open.
    seal(store, MEDIA, 1, b"the real image" * 4)
    store.publish(MEDIA, 1)
    unrelated = seal(store, OTHER, 1, b"a different image entirely" * 4)

    outcome = store.recover_final(MEDIA, 1, unrelated)

    assert outcome is FinalOutcome.QUARANTINED
    assert not store.final_exists(MEDIA)
    assert store.quarantine_path(MEDIA).exists()


def test_recovery_refuses_to_quarantine_over_a_previous_quarantine(store) -> None:
    # A second incident on the same object needs a human, not an overwrite: the
    # first quarantined file is the evidence someone is still looking at.
    seal(store, MEDIA, 1, b"real" * 4)
    store.publish(MEDIA, 1)
    store.quarantine_final(MEDIA)
    quarantine_inode = os.stat(store.quarantine_path(MEDIA)).st_ino

    seal(store, MEDIA, 2, b"second" * 4)
    store.publish(MEDIA, 2)
    with pytest.raises(MediaStoreError):
        store.quarantine_final(MEDIA)

    assert os.stat(store.quarantine_path(MEDIA)).st_ino == quarantine_inode


# --- cleanup ----------------------------------------------------------------


def test_discarding_staging_is_idempotent(store) -> None:
    seal(store, MEDIA, 1, b"bytes" * 4)
    assert store.discard_staging(MEDIA, 1) is True
    # A replayed cleanup and a crash midway through one both land here, and
    # neither is a failure.
    assert store.discard_staging(MEDIA, 1) is False


def test_discarding_absent_staging_is_not_an_error(store) -> None:
    assert store.discard_staging(MEDIA, 7) is False


def test_discarding_a_final_is_idempotent(store) -> None:
    seal(store, MEDIA, 1, b"bytes" * 4)
    store.publish(MEDIA, 1)
    assert store.discard_final(MEDIA) is True
    assert store.discard_final(MEDIA) is False
    assert not store.final_exists(MEDIA)


def test_discarding_a_final_refuses_a_symlink(store, tmp_path) -> None:
    # Deletion must not follow a link and unlink whatever it points at. The
    # whole reason ids are random and the root is controlled is that this path
    # never has to guess what it is removing.
    elsewhere = tmp_path / "outside.bin"
    elsewhere.write_bytes(b"not ours")
    final = store.final_path(MEDIA)
    final.parent.mkdir(parents=True, exist_ok=True)
    final.symlink_to(elsewhere)

    with pytest.raises(MediaStoreError):
        store.discard_final(MEDIA)
    assert elsewhere.exists()


# --- the properties that tie the pieces together ----------------------------


def test_a_store_cannot_read_another_roles_objects(tmp_path, keyring) -> None:
    # The role is bound into every chunk's AAD, so a store configured for
    # another purpose refuses these bytes instead of returning someone else's
    # images. Without the binding this would decrypt cleanly.
    root = tmp_path / "media"
    images = MediaStore(root, keyring, chunk_bytes=CHUNK, role="chat_image")
    images.prepare_directories()
    record = images.write_staging(MEDIA, 1, [b"payload" * 4])
    images.publish(MEDIA, 1)

    other = MediaStore(root, keyring, chunk_bytes=CHUNK, role="another_purpose")
    with pytest.raises(ContainerError):
        other.read_final(MEDIA, 1, record)


def test_a_second_publish_of_the_same_attempt_finds_staging_gone(store) -> None:
    # Replaying the publish step of a completed attempt is a no-op refusal, not
    # a second install.
    seal(store, MEDIA, 1, b"bytes" * 4)
    store.publish(MEDIA, 1)
    with pytest.raises(MediaStoreError):
        store.publish(MEDIA, 1)


def test_the_container_written_by_the_store_is_the_container_published(
    store, keyring
) -> None:
    # A last cross-check that the store adds no envelope of its own: the bytes
    # at the final path are the bytes the container reader understands, read
    # without going through the store at all.
    payload = b"cross-check" * 5
    record = seal(store, MEDIA, 1, payload)
    store.publish(MEDIA, 1)

    assert (
        read_container(
            store.final_path(MEDIA),
            SealRecord.from_dict(record.to_dict()),
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=1,
            chunk_bytes=CHUNK,
        )
        == payload
    )


def test_writing_the_same_plaintext_twice_yields_different_ciphertext(
    store, tmp_path
) -> None:
    # Random per-chunk nonces mean a re-upload of identical bytes is a new
    # ciphertext. Nothing may treat that difference as corruption.
    payload = b"identical" * 8
    first = seal(store, MEDIA, 1, payload)
    first_bytes = store.staging_path(MEDIA, 1).read_bytes()
    store.discard_staging(MEDIA, 1)
    second = seal(store, MEDIA, 2, payload)
    second_bytes = store.staging_path(MEDIA, 2).read_bytes()

    assert first.sha256 == second.sha256 != ""
    assert first_bytes != second_bytes


def test_a_tampered_published_file_is_not_adopted(store) -> None:
    # End to end through the recovery path: a file that verified when it was
    # written, then had a byte flipped, must be quarantined rather than
    # installed as the image.
    record = seal(store, MEDIA, 1, b"the real image" * 4)
    store.publish(MEDIA, 1)
    assert store.recover_final(MEDIA, 1, record) is FinalOutcome.ADOPTED

    raw = bytearray(store.final_path(MEDIA).read_bytes())
    raw[-1] ^= 0x01
    store.final_path(MEDIA).write_bytes(bytes(raw))

    assert store.recover_final(MEDIA, 1, record) is FinalOutcome.QUARANTINED
