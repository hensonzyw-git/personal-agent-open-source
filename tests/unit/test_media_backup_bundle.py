"""The media bundle is a producer/consumer boundary, not a file copy."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.backup.media_bundle import (
    MediaBundleError,
    prepare_media_bundle,
    verify_published_media_bundle,
)
from personal_agent.backup.restore_verify import check_media_bundle_media
from personal_agent.backup.restore_verify import run_all
from personal_agent.media.lifecycle import (
    claim_upload,
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


NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing([generate_key("agent-data", state="active")], service="personal-agent-api")


def _database(path: Path):
    engine = create_database_engine(path)
    db.upgrade(engine, "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, created_at) "
                "VALUES ('device', 'phone', 'key', 'thumb', 'active', '[]', 'v1', :now)"
            ),
            {"now": to_rfc3339(NOW)},
        )
    return engine


def _media_root(tmp_path: Path, keyring: KeyRing) -> MediaStore:
    root = tmp_path / "media"
    for name in ("staging", "final", "quarantine"):
        (root / name).mkdir(parents=True)
    ensure_lock_files(root)
    return MediaStore(root, keyring, max_content_bytes=4096)


def _stage_root(tmp_path: Path) -> Path:
    root = tmp_path / "stage"
    (root / "media-runs").mkdir(parents=True)
    (root.parent / "media-bundle.lock").touch()
    return root


def _ready_media(engine, store: MediaStore, keyring: KeyRing) -> str:
    with session_factory(engine)() as session:
        media_id = create_upload(
            session, keyring=keyring, device_id="device", declared_mime="image/jpeg",
            declared_size=4, declared_sha256=None, now=NOW,
            expires_at=NOW + timedelta(hours=1), declared_width=1, declared_height=1,
        )
        attempt = claim_upload(
            session, media_id=media_id, device_id="device", owner_token="owner", now=NOW,
            claim_deadline=NOW + timedelta(minutes=5),
        )
        seal = store.write_staging(media_id, attempt, [b"\xff\xd8\xff\x00"])
        seal_upload(
            session, keyring=keyring, media_id=media_id, attempt_number=attempt,
            owner_token="owner", seal=seal, actual_mime="image/jpeg", content_size=4, now=NOW,
        )
        assert publish_upload(
            session, media_id=media_id, device_id="device", store=store, keyring=keyring,
            now=NOW, owner_token="owner",
        ).value == "published"
        session.commit()
    return media_id


def test_restore_cannot_omit_bundle_for_populated_media(tmp_path, keyring):
    database = tmp_path / "agent.sqlite"
    engine = _database(database)
    try:
        assert all(result["ok"] for result in run_all(database, keyring, manifest_entries=[]))
        _ready_media(engine, _media_root(tmp_path, keyring), keyring)
        results = run_all(database, keyring, manifest_entries=[])
        assert any(not result["ok"] and result["name"] == "media_restore" for result in results)
    finally:
        engine.dispose()


def test_bundle_captures_only_snapshot_referenced_ciphertext(tmp_path: Path, keyring: KeyRing):
    database = tmp_path / "agent.sqlite"
    engine = _database(database)
    store = _media_root(tmp_path, keyring)
    media_id = _ready_media(engine, store, keyring)
    stage = _stage_root(tmp_path)

    prepared = prepare_media_bundle(database=database, media_root=store.roots.root, stage_root=stage)
    verified = verify_published_media_bundle(stage)

    assert verified == prepared
    manifest = json.loads((prepared.path / "manifest.json").read_text())
    assert [entry["media_id"] for entry in manifest["media"]] == [media_id]
    assert (prepared.path / "agent.sqlite").is_file()
    assert (prepared.path / "deletion-manifest.json").is_file()
    assert (prepared.path / "media" / f"{media_id}.bin").read_bytes() == store.final_path(media_id).read_bytes()
    assert check_media_bundle_media(prepared.path / "agent.sqlite", keyring, prepared.path) == {
        "name": "media_bundle", "ok": True, "detail": "authenticated_media=1"
    }
    engine.dispose()


def test_ready_media_without_current_attempt_fails_restore(tmp_path, keyring):
    database = tmp_path / "agent.sqlite"
    engine = _database(database)
    store = _media_root(tmp_path, keyring)
    media_id = _ready_media(engine, store, keyring)
    with engine.begin() as connection:
        connection.execute(text("UPDATE media_objects SET current_attempt_number=99 WHERE media_id=:id"),
                           {"id": media_id})
    assert not check_media_bundle_media(database, keyring, tmp_path)["ok"]
    engine.dispose()


def test_text_only_backup_and_local_run_retention(tmp_path, keyring):
    database = tmp_path / "agent.sqlite"
    engine = _database(database)
    stage = _stage_root(tmp_path)
    first = prepare_media_bundle(database=database, media_root=None, stage_root=stage)
    second = prepare_media_bundle(database=database, media_root=None, stage_root=stage)
    assert not first.path.exists()
    assert verify_published_media_bundle(stage) == second
    engine.dispose()


def test_restore_replays_new_manifest_and_physically_removes_deleted_media(tmp_path, keyring):
    from personal_agent.media.deletion import mark_media_deleting
    from personal_agent.backup.deletion_manifest import export_manifest
    from personal_agent.backup.restore_verify import run_all
    database = tmp_path / "agent.sqlite"
    engine = _database(database)
    store = _media_root(tmp_path, keyring)
    media_id = _ready_media(engine, store, keyring)
    stage = _stage_root(tmp_path)
    bundle = prepare_media_bundle(database=database, media_root=store.roots.root, stage_root=stage)
    with session_factory(engine)() as session:
        mark_media_deleting(session, media_id=media_id, keyring=keyring, now=NOW)
        session.commit()
        latest = export_manifest(session)
    results = run_all(bundle.path / "agent.sqlite", keyring,
                      manifest_entries=latest, media_bundle=bundle.path)
    assert all(result["ok"] for result in results), results
    assert not (bundle.path / "media" / f"{media_id}.bin").exists()
    restored = bundle.path.parent / ("restored-media-" + bundle.path.name)
    assert not (restored / "final" / media_id[:2] / f"{media_id}.bin").exists()
    engine.dispose()


def test_publish_adopts_authenticated_final_after_database_rollback(tmp_path, keyring):
    engine = _database(tmp_path / "agent.sqlite")
    store = _media_root(tmp_path, keyring)
    media_id = _ready_media(engine, store, keyring)
    with engine.begin() as connection:
        connection.execute(text("UPDATE media_objects SET state='uploaded' WHERE media_id=:id"),
                           {"id": media_id})
    with session_factory(engine)() as session:
        assert publish_upload(session, media_id=media_id, device_id="device",
                              store=store, keyring=keyring, now=NOW).value == "published"
        session.commit()
    engine.dispose()


def test_missing_or_tampered_ciphertext_refuses_publish_or_consume(tmp_path: Path, keyring: KeyRing):
    database = tmp_path / "agent.sqlite"
    engine = _database(database)
    store = _media_root(tmp_path, keyring)
    media_id = _ready_media(engine, store, keyring)
    stage = _stage_root(tmp_path)
    store.final_path(media_id).unlink()
    with pytest.raises(MediaBundleError, match="missing"):
        prepare_media_bundle(database=database, media_root=store.roots.root, stage_root=stage)

    # Simulate the reaper's post-incident terminal state before proving the
    # consumer-side hash check with an otherwise valid, newer run.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE media_objects SET state = 'deleted' WHERE media_id = :media_id"),
            {"media_id": media_id},
        )
    _ready_media(engine, store, keyring)
    prepared = prepare_media_bundle(database=database, media_root=store.roots.root, stage_root=stage)
    (prepared.path / "agent.sqlite").write_bytes(b"tampered")
    with pytest.raises(MediaBundleError, match="does not match"):
        verify_published_media_bundle(stage)
    # A manifest is an allow-list: an extra file cannot hitch a ride in restic.
    prepared = prepare_media_bundle(database=database, media_root=store.roots.root, stage_root=stage)
    (prepared.path / "unexpected").write_bytes(b"not declared")
    with pytest.raises(MediaBundleError, match="unlisted"):
        verify_published_media_bundle(stage)
    engine.dispose()


def test_restore_verifier_rejects_ciphertext_bound_to_another_media_id(tmp_path: Path, keyring: KeyRing):
    database = tmp_path / "agent.sqlite"
    engine = _database(database)
    store = _media_root(tmp_path, keyring)
    first = _ready_media(engine, store, keyring)
    second = _ready_media(engine, store, keyring)
    stage = _stage_root(tmp_path)
    prepared = prepare_media_bundle(database=database, media_root=store.roots.root, stage_root=stage)

    # This models an internally consistent but wrongly-associated ciphertext
    # copy: hashes alone are insufficient, so the restore gate must authenticate
    # each container under the media ID and sealed attempt from the snapshot.
    first_path = prepared.path / "media" / f"{first}.bin"
    first_path.write_bytes((prepared.path / "media" / f"{second}.bin").read_bytes())
    manifest_path = prepared.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256(first_path.read_bytes()).hexdigest()
    size = first_path.stat().st_size
    relative = f"media/{first}.bin"
    for entry in manifest["files"] + manifest["media"]:
        if entry["path"] == relative:
            entry["sha256"] = digest
            entry["size"] = size
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert verify_published_media_bundle(stage) == prepared
    result = check_media_bundle_media(prepared.path / "agent.sqlite", keyring, prepared.path)
    assert result["name"] == "media_bundle"
    assert not result["ok"]
    assert "ciphertext verification failed" in result["detail"]
    engine.dispose()
