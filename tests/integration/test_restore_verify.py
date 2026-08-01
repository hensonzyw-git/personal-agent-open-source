"""`DEV-035`: the library-side restore checks, pinned offline.

``scripts/restore_drill.sh`` orchestrates restic + a read-only service start;
this test pins the six checks that are pure library calls against a restored
fixture database, including the failure shapes: a snapshot at the wrong schema
revision, a broken operation reference, an AEAD sample that will not open
under the wrong key, and a manifest replay that must run after the sample.
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
    export_manifest,
)
from personal_agent.backup.restore_verify import (
    check_aead_sample,
    check_audit_chain_intact,
    check_integrity,
    check_replay_deletion_manifest,
    check_schema_version,
    run_all,
)
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import to_rfc3339


NOW = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def _seed(path: Path, keyring: KeyRing, *, conversations: list[str]) -> None:
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
            conn.execute(
                text(
                    "INSERT INTO context_sessions (session_id, conversation_id, "
                    "status, relation_kind, opened_at) VALUES "
                    "(:sid, :cid, 'open', 'new_topic', :now)"
                ),
                {"sid": f"sess-{cid}", "cid": cid, "now": to_rfc3339(NOW)},
            )
            sealed = keyring.encrypt(
                json.dumps({"text": cid}).encode("utf-8"),
                table="conversation_events",
                column="encrypted_content",
                row_id=f"evt-{cid}",
            )
            conn.execute(
                text(
                    "INSERT INTO conversation_events (event_id, conversation_id, "
                    "session_id, turn_id, event_type, encrypted_content, "
                    "timeline_sequence, created_at) VALUES "
                    "(:eid, :cid, :sid, :turn, 'user_message', :content, 1, :now)"
                ),
                {
                    "eid": f"evt-{cid}",
                    "cid": cid,
                    "sid": f"sess-{cid}",
                    "turn": f"turn-{cid}",
                    "content": json.dumps(sealed, ensure_ascii=False, sort_keys=True),
                    "now": to_rfc3339(NOW),
                },
            )
    return engine


def _add_manifest(engine, keyring: KeyRing, *, entry_id: str, object_id: str) -> None:
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
                "(:eid, 'conversation', :sealed, :now)"
            ),
            {
                "eid": entry_id,
                "sealed": json.dumps(sealed, ensure_ascii=False, sort_keys=True),
                "now": to_rfc3339(NOW),
            },
        )


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


def test_integrity_passes_on_a_fresh_restore(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    _seed(path, keyring, conversations=["c1"])
    result = check_integrity(path)
    assert result["ok"], result["detail"]


def test_schema_version_matches_head(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    _seed(path, keyring, conversations=["c1"])
    result = check_schema_version(path)
    assert result["ok"], result["detail"]


def test_schema_version_fails_on_a_stale_snapshot(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine, "0002_notification_outbox_constraints")
    engine.dispose()
    result = check_schema_version(path)
    assert not result["ok"]
    assert "0002" in result["detail"] or "expected" in result["detail"]


def test_reference_integrity_passes(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    _seed(path, keyring, conversations=["c1"])
    assert check_audit_chain_intact(path)["ok"]


def test_reference_integrity_catches_an_orphan_operation(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "agent.sqlite"
    _seed(path, keyring, conversations=["c1"])
    engine = create_database_engine(path)
    # Insert an operation whose request_id resolves to nothing -- the shape of
    # a restore that lost an api_requests row. The schema enforces the FK on
    # normal connections, so this uses a raw connection with foreign keys off,
    # modelling a DB file that was repaired or partially restored by a tool
    # that did not enforce constraints.
    import sqlite3

    raw = sqlite3.connect(str(path))
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO operations (operation_id, request_id, trace_id, "
            "idempotency_key, state, state_version, cancel_requested, "
            "client_detached, created_at, updated_at) VALUES "
            "('op-orphan', 'req-missing', 'tr', 'key-orphan', 'succeeded', "
            "1, 0, 0, ?, ?)",
            (to_rfc3339(NOW), to_rfc3339(NOW)),
        )
        raw.commit()
    finally:
        raw.close()
    engine.dispose()
    result = check_audit_chain_intact(path)
    assert not result["ok"]
    assert "orphan_operations=1" in result["detail"]


def test_aead_sample_opens_under_the_right_key(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed(path, keyring, conversations=["c1"])
    _add_manifest(engine, keyring, entry_id="drill-sample", object_id="c1")
    result = check_aead_sample(path, keyring, entry_id="drill-sample")
    assert result["ok"], result["detail"]


def test_aead_sample_fails_under_the_wrong_key(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed(path, keyring, conversations=["c1"])
    _add_manifest(engine, keyring, entry_id="drill-sample", object_id="c1")
    wrong = KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )
    result = check_aead_sample(path, wrong, entry_id="drill-sample")
    assert not result["ok"]
    assert "decrypt failed" in result["detail"]


def test_replay_deletes_a_restored_conversation(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed(path, keyring, conversations=["c1", "c2"])
    _add_manifest(engine, keyring, entry_id="m1", object_id="c1")
    with session_factory(engine)() as session:
        manifest = export_manifest(session)

    result = check_replay_deletion_manifest(path, keyring, manifest)
    assert result["ok"], result["detail"]
    assert "applied=1" in result["detail"]


def test_run_all_orders_aead_before_replay(tmp_path: Path, keyring: KeyRing) -> None:
    # The AEAD sample and the replay both touch the same manifest entry. If
    # replay ran first it would delete the entry's target but the entry itself
    # stays, so the sample would still decrypt -- the ordering matters for the
    # *replay* not racing the *sample* against a half-applied state. Pin that
    # run_all returns a result for each, in order.
    path = tmp_path / "agent.sqlite"
    engine = _seed(path, keyring, conversations=["c1"])
    _add_manifest(engine, keyring, entry_id="drill-sample", object_id="c1")
    with session_factory(engine)() as session:
        manifest = export_manifest(session)

    results = run_all(
        path,
        keyring,
        manifest_entries=manifest,
        aead_sample_entry_id="drill-sample",
    )
    names = [r["name"] for r in results]
    assert names.index("aead_sample") < names.index("deletion_manifest_replay")
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]]
