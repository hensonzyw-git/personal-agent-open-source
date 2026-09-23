"""`DEV-035`: deletion-manifest export and replay, pinned.

Design 10.5: after a live DB is restored, deleted data must not come back. The
``deletion_manifest`` table held sealed object ids but nothing exported or
replayed them, so the property was a column and a hope. This pins the loop:
export carries sealed ids (never plaintext), replay opens them under the data
key and applies the deletion, and anything that cannot be opened or is an
unknown type fails closed rather than skipping -- a skipped deletion is a
deletion that comes back.

The restore half only: Phase 1 has no business path that *writes* to the
manifest yet, so entries are seeded directly here. The first writer
(conversation deletion) will add its type to ``REPLAY_HANDLERS``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.backup.deletion_manifest import (
    MANIFEST_COLUMN,
    MANIFEST_TABLE,
    ManifestReplayError,
    export_manifest,
    replay_manifest,
)
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key


NOW = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


def _seed_database(path: Path, keyring: KeyRing, *, conversations: list[str]) -> None:
    """A latest-schema Agent DB with the named conversations present.

    Each conversation gets one session and one event so the row is real, but
    the point is the conversation row that replay will delete.
    """
    from personal_agent_core.timeutil import to_rfc3339

    engine = create_database_engine(path)
    db.upgrade(engine, "head")
    with engine.begin() as conn:
        for cid in conversations:
            conn.execute(
                text(
                    "INSERT INTO conversations (conversation_id, created_at, "
                    "next_sequence) VALUES (:cid, :now, 2)"
                ),
                {"cid": cid, "now": to_rfc3339(NOW)},
            )
            sid = f"sess-{cid}"
            conn.execute(
                text(
                    "INSERT INTO context_sessions (session_id, conversation_id, "
                    "status, relation_kind, opened_at) VALUES "
                    "(:sid, :cid, 'open', 'new_topic', :now)"
                ),
                {"sid": sid, "cid": cid, "now": to_rfc3339(NOW)},
            )
            eid = f"evt-{cid}"
            sealed = keyring.encrypt(
                json.dumps({"text": cid}).encode("utf-8"),
                table="conversation_events",
                column="encrypted_content",
                row_id=eid,
            )
            conn.execute(
                text(
                    "INSERT INTO conversation_events (event_id, conversation_id, "
                    "session_id, turn_id, event_type, encrypted_content, "
                    "timeline_sequence, created_at) VALUES "
                    "(:eid, :cid, :sid, :turn, 'user_message', :content, 1, :now)"
                ),
                {
                    "eid": eid,
                    "cid": cid,
                    "sid": sid,
                    "turn": f"turn-{cid}",
                    "content": json.dumps(sealed, ensure_ascii=False, sort_keys=True),
                    "now": to_rfc3339(NOW),
                },
            )
    return engine


def _add_manifest_entry(engine, keyring: KeyRing, *, entry_id: str, object_id: str, object_type: str = "conversation") -> None:
    from personal_agent_core.timeutil import to_rfc3339

    sealed = keyring.encrypt(
        object_id.encode("utf-8"),
        table=MANIFEST_TABLE,
        column=MANIFEST_COLUMN,
        row_id=entry_id,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO deletion_manifest (entry_id, object_type, "
                "encrypted_object_id, deleted_at) VALUES "
                "(:eid, :otype, :sealed, :now)"
            ),
            {
                "eid": entry_id,
                "otype": object_type,
                "sealed": json.dumps(sealed, ensure_ascii=False, sort_keys=True),
                "now": to_rfc3339(NOW),
            },
        )


def _conversation_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM conversations")).scalar_one()


def test_export_carries_sealed_ids_not_plaintext(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    plaintext_canary = "plaintext-conversation-id-canary-2026-09-23"
    engine = _seed_database(path, keyring, conversations=["c1", "c2"])
    _add_manifest_entry(engine, keyring, entry_id="m1", object_id=plaintext_canary)

    with session_factory(engine)() as session:
        exported = export_manifest(session)

    assert len(exported) == 1
    entry = exported[0]
    assert entry["entry_id"] == "m1"
    assert entry["object_type"] == "conversation"
    # The sealed envelope is a dict with the AEAD shape, never a plaintext id.
    assert isinstance(entry["encrypted_object_id"], dict)
    assert "ciphertext" in entry["encrypted_object_id"]
    assert plaintext_canary not in json.dumps(entry)


def test_replay_deletes_a_restored_conversation(tmp_path: Path, keyring: KeyRing) -> None:
    # Live state: conversation c1 exists and was deleted, so the manifest
    # records it. The backup captures both the (post-delete) DB snapshot and
    # the manifest export.
    path = tmp_path / "agent.sqlite"
    engine = _seed_database(path, keyring, conversations=["c1", "c2"])
    _add_manifest_entry(engine, keyring, entry_id="m1", object_id="c1")

    with session_factory(engine)() as session:
        manifest = export_manifest(session)

    # The restore brings back an OLDER snapshot where c1 was still present --
    # this is exactly the case the manifest exists for. The canonical Timeline
    # row the migration seeds is also present and must survive replay.
    restored_path = tmp_path / "restored.sqlite"
    restored_engine = _seed_database(restored_path, keyring, conversations=["c1", "c2"])
    assert _conversation_count(restored_engine) == 3

    with session_factory(restored_engine)() as session:
        result = replay_manifest(session, manifest, keyring)

    assert result["applied"] == 1
    assert _conversation_count(restored_engine) == 2
    # The CASCADE took the restored c1's event with it.
    with restored_engine.connect() as conn:
        events = conn.execute(
            text("SELECT count(*) FROM conversation_events WHERE conversation_id='c1'")
        ).scalar_one()
        assert events == 0


def test_replay_is_idempotent_on_an_already_absent_row(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed_database(path, keyring, conversations=["c2"])
    _add_manifest_entry(engine, keyring, entry_id="m1", object_id="c1")

    with session_factory(engine)() as session:
        manifest = export_manifest(session)

    # Restored snapshot never had c1. Replay opens the id and deletes nothing,
    # which is success, not a failure: a re-replay after a prior replay must
    # not error.
    with session_factory(engine)() as session:
        result = replay_manifest(session, manifest, keyring)

    assert result["applied"] == 0
    assert result["already_absent"] == 1


def test_replay_fails_closed_on_unknown_object_type(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed_database(path, keyring, conversations=["c1"])
    _add_manifest_entry(engine, keyring, entry_id="m1", object_id="c1", object_type="future_kind")

    with session_factory(engine)() as session:
        manifest = export_manifest(session)

    # A future deletion type the replay code has not been taught must not be
    # skipped: skipping is how a deletion comes back.
    with session_factory(engine)() as session:
        with pytest.raises(ManifestReplayError, match="unknown object_type"):
            replay_manifest(session, manifest, keyring)
        session.rollback()


def test_replay_fails_closed_on_a_tampered_aad(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed_database(path, keyring, conversations=["c1"])
    _add_manifest_entry(engine, keyring, entry_id="m1", object_id="c1")

    with session_factory(engine)() as session:
        manifest = export_manifest(session)

    # A second key ring sealed nothing here, so decryption under it fails. This
    # models a restore that injected the wrong data key, or a manifest copied
    # from another service: the AAD binding refuses rather than opening.
    wrong_keyring = KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )
    with session_factory(engine)() as session:
        with pytest.raises(ManifestReplayError, match="could not open"):
            replay_manifest(session, manifest, wrong_keyring)
        session.rollback()
