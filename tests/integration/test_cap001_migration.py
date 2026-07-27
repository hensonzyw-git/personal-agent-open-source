"""CAP-001 slice B: the Timeline migration, upgrade and downgrade.

Covers failure set F-B1..F-B11 (`docs/CAP-001失败集_v0.1.md` §2). Every test runs
the real Alembic revision against a real SQLite database holding real sealed
events, because the properties being defended -- that no message is lost,
reordered, re-encrypted or made unreadable -- are exactly the ones a mocked
migration cannot demonstrate.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from cap001_fixtures import IDENTIFIER_KEY
from personal_agent.storage import db
from personal_agent.storage.cap001_migration import (
    Cap001MigrationError,
    MigrationInputs,
    alias_hmac,
)
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key


NOW = datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)
BASE_REVISION = "0002_notification_outbox_constraints"


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


def _stamp(moment: datetime) -> str:
    from personal_agent_core.timeutil import to_rfc3339

    return to_rfc3339(moment)


def _seal(keyring: KeyRing, event_id: str, content: dict) -> str:
    envelope = keyring.encrypt(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        table="conversation_events",
        column="encrypted_content",
        row_id=event_id,
    )
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def _legacy_database(
    path: Path,
    keyring: KeyRing,
    *,
    events: list[tuple[str, str, datetime, str | None]],
    conversations: dict[str, datetime],
    operation_state: str = "succeeded",
):
    """A database at revision 0002 holding real sealed conversation events.

    `events` are `(event_id, conversation_id, created_at, operation_id)`.
    """
    engine = create_database_engine(path)
    db.upgrade(engine, BASE_REVISION)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev-1', 'iPhone', 'K', 'T', 'active', "
                "'[]', 'v1', :now)"
            ),
            {"now": _stamp(NOW)},
        )
        for conversation_id, created in conversations.items():
            connection.execute(
                text(
                    "INSERT INTO conversations (conversation_id, created_at) "
                    "VALUES (:cid, :created)"
                ),
                {"cid": conversation_id, "created": _stamp(created)},
            )
        operations = {
            operation_id for _, _, _, operation_id in events if operation_id
        }
        for index, operation_id in enumerate(sorted(operations)):
            connection.execute(
                text(
                    "INSERT INTO api_requests (request_id, device_id, "
                    "client_request_id, request_fingerprint, received_at) "
                    "VALUES (:rid, 'dev-1', :crid, 'fp', :now)"
                ),
                {
                    "rid": f"req-{index}",
                    "crid": f"client-{index}",
                    "now": _stamp(NOW),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO operations (operation_id, request_id, "
                    "trace_id, idempotency_key, state, state_version, "
                    "cancel_requested, client_detached, created_at, updated_at) "
                    "VALUES (:oid, :rid, 'tr', :key, :state, 1, 0, 0, :now, :now)"
                ),
                {
                    "oid": operation_id,
                    "rid": f"req-{index}",
                    "key": f"key-{index}",
                    "state": operation_state,
                    "now": _stamp(NOW),
                },
            )
        for event_id, conversation_id, created, operation_id in events:
            connection.execute(
                text(
                    "INSERT INTO conversation_events (event_id, "
                    "conversation_id, event_type, encrypted_content, "
                    "operation_id, created_at) VALUES (:eid, :cid, "
                    "'user_message', :content, :oid, :created)"
                ),
                {
                    "eid": event_id,
                    "cid": conversation_id,
                    "content": _seal(keyring, event_id, {"text": f"msg {event_id}"}),
                    "oid": operation_id,
                    "created": _stamp(created),
                },
            )
    return engine


def _inputs(keyring: KeyRing, backup: Path | None) -> MigrationInputs:
    return MigrationInputs(
        keyring=keyring, identifier_key=IDENTIFIER_KEY, backup_path=backup
    )


def _backup(engine, path: Path, tmp_path: Path) -> Path:
    engine.dispose()
    destination = tmp_path / "backup.sqlite"
    shutil.copyfile(path, destination)
    return destination


@pytest.fixture()
def migrated(tmp_path: Path, keyring: KeyRing):
    """Two legacy conversations, five events, one shared operation, migrated."""
    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={
            "conv-a": NOW - timedelta(days=2),
            "conv-b": NOW - timedelta(days=1),
        },
        events=[
            ("evt-1", "conv-a", NOW - timedelta(minutes=50), "op-1"),
            ("evt-2", "conv-a", NOW - timedelta(minutes=49), "op-1"),
            ("evt-3", "conv-b", NOW - timedelta(minutes=30), "op-2"),
            ("evt-4", "conv-b", NOW - timedelta(minutes=20), None),
            ("evt-5", "conv-a", NOW - timedelta(minutes=10), None),
        ],
    )
    backup = _backup(engine, path, tmp_path)
    engine = create_database_engine(path)
    db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, backup))
    yield engine, path, backup
    engine.dispose()


# -- F-B1: maintenance mode -----------------------------------------------


def test_migration_refuses_with_a_non_terminal_operation(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-a": NOW},
        events=[("evt-1", "conv-a", NOW, "op-1")],
        operation_state="dispatching",
    )
    backup = _backup(engine, path, tmp_path)
    engine = create_database_engine(path)
    with pytest.raises(Cap001MigrationError) as excinfo:
        db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, backup))
    assert "terminal" in str(excinfo.value)
    # Nothing was changed: the preflight runs before any DDL.
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "context_sessions" not in tables
    engine.dispose()


def test_migration_refuses_without_key_material(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-a": NOW},
        events=[("evt-1", "conv-a", NOW, None)],
    )
    engine.dispose()
    engine = create_database_engine(path)
    with pytest.raises(Cap001MigrationError):
        db.upgrade(engine, "head")
    engine.dispose()


def test_migration_refuses_without_a_verified_backup(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-a": NOW},
        events=[("evt-1", "conv-a", NOW, None)],
    )
    engine.dispose()
    engine = create_database_engine(path)
    with pytest.raises(Cap001MigrationError) as excinfo:
        db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, None))
    assert "backup" in str(excinfo.value)
    engine.dispose()


# -- F-B10: the restore fixture is a real restore --------------------------


def test_a_backup_of_a_different_database_is_refused(
    tmp_path: Path, keyring: KeyRing
) -> None:
    other = tmp_path / "other.sqlite"
    other_engine = _legacy_database(
        other,
        keyring,
        conversations={"conv-x": NOW},
        # The count deliberately matches the live database. Counting rows is
        # not proof that this is its backup.
        events=[("evt-x", "conv-x", NOW, None)],
    )
    wrong_backup = _backup(other_engine, other, tmp_path)

    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-a": NOW},
        events=[("evt-1", "conv-a", NOW, None)],
    )
    engine.dispose()
    engine = create_database_engine(path)
    with pytest.raises(Cap001MigrationError) as excinfo:
        db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, wrong_backup))
    assert "backup" in str(excinfo.value)
    engine.dispose()


def test_the_live_database_itself_is_not_a_backup(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-a": NOW},
        events=[("evt-1", "conv-a", NOW, None)],
    )
    engine.dispose()
    engine = create_database_engine(path)
    with pytest.raises(Cap001MigrationError, match="live database"):
        db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, path))
    engine.dispose()


def test_a_missing_backup_file_is_refused(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-a": NOW},
        events=[("evt-1", "conv-a", NOW, None)],
    )
    engine.dispose()
    engine = create_database_engine(path)
    with pytest.raises(Cap001MigrationError):
        db.upgrade(
            engine,
            "head",
            cap001_inputs=_inputs(keyring, tmp_path / "nope.sqlite"),
        )
    engine.dispose()


# -- F-B5: the AAD contract is proven, never guessed -----------------------


def test_migration_stops_when_the_event_aad_contract_differs(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-a": NOW},
        events=[("evt-1", "conv-a", NOW, None)],
    )
    # Seal the second event against a *different* row id: exactly what a
    # different AAD contract would look like from here.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO conversation_events (event_id, conversation_id, "
                "event_type, encrypted_content, operation_id, created_at) "
                "VALUES ('evt-2', 'conv-a', 'user_message', :content, NULL, "
                ":created)"
            ),
            {
                "content": _seal(keyring, "some-other-row", {"text": "x"}),
                "created": _stamp(NOW),
            },
        )
    backup = _backup(engine, path, tmp_path)
    engine = create_database_engine(path)
    with pytest.raises(Cap001MigrationError) as excinfo:
        db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, backup))
    assert "must not guess" in str(excinfo.value)
    engine.dispose()


# -- F-B2/F-B3: the merge is ordered and deterministic ---------------------


def test_multiple_conversations_merge_into_one_ordered_timeline(migrated) -> None:
    engine, _, _ = migrated
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT event_id, timeline_sequence FROM conversation_events "
                "ORDER BY timeline_sequence"
            )
        ).all()
        canonical = connection.execute(
            text("SELECT COUNT(*) FROM conversations")
        ).scalar_one()
    assert [row[0] for row in rows] == [
        "evt-1",
        "evt-2",
        "evt-3",
        "evt-4",
        "evt-5",
    ]
    assert [row[1] for row in rows] == [1, 2, 3, 4, 5]
    # The two legacy conversation rows are gone; one canonical Timeline remains.
    assert canonical == 1


def test_same_timestamp_events_are_ordered_deterministically(
    tmp_path: Path, keyring: KeyRing
) -> None:
    # Two devices writing in the same instant: `created_at` cannot decide, so
    # `event_id` does, and the result must be reproducible.
    orders = []
    for attempt in range(2):
        path = tmp_path / f"agent-{attempt}.sqlite"
        engine = _legacy_database(
            path,
            keyring,
            conversations={"conv-a": NOW, "conv-b": NOW},
            events=[
                ("evt-b", "conv-b", NOW, None),
                ("evt-a", "conv-a", NOW, None),
                ("evt-c", "conv-a", NOW, None),
            ],
        )
        (tmp_path / f"b{attempt}").mkdir(exist_ok=True)
        backup = _backup(engine, path, tmp_path / f"b{attempt}")
        engine = create_database_engine(path)
        db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, backup))
        with engine.connect() as connection:
            orders.append(
                [
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT event_id FROM conversation_events "
                            "ORDER BY timeline_sequence"
                        )
                    )
                ]
            )
        engine.dispose()
    assert orders[0] == orders[1] == ["evt-a", "evt-b", "evt-c"]


# -- F-B4: one closed legacy Session per old conversation ------------------


def test_each_legacy_conversation_becomes_one_closed_session(migrated) -> None:
    engine, _, _ = migrated
    with engine.connect() as connection:
        sessions = connection.execute(
            text(
                "SELECT session_id, status, relation_kind, boundary_reason "
                "FROM context_sessions"
            )
        ).all()
        by_session = connection.execute(
            text(
                "SELECT session_id, COUNT(*) FROM conversation_events "
                "GROUP BY session_id ORDER BY COUNT(*)"
            )
        ).all()
    assert len(sessions) == 2
    assert {row[1] for row in sessions} == {"closed"}
    assert {row[2] for row in sessions} == {"legacy"}
    # A legacy Session is not the outcome of a boundary decision, so it invents
    # no reason for one.
    assert {row[3] for row in sessions} == {None}
    # conv-b held two events, conv-a three.
    assert [row[1] for row in by_session] == [2, 3]


# -- F-B8: one turn per operation ------------------------------------------


def test_events_of_one_operation_share_a_turn(migrated) -> None:
    engine, _, _ = migrated
    with engine.connect() as connection:
        turns = dict(
            connection.execute(
                text("SELECT event_id, turn_id FROM conversation_events")
            ).all()
        )
    assert turns["evt-1"] == turns["evt-2"]
    assert turns["evt-3"] != turns["evt-1"]
    assert len({turns["evt-4"], turns["evt-5"]}) == 2


# -- F-B6: the upgrade verifies itself -------------------------------------


def test_every_migrated_event_still_decrypts_and_keeps_its_operation(
    migrated, keyring: KeyRing
) -> None:
    engine, _, _ = migrated
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT event_id, encrypted_content, operation_id "
                "FROM conversation_events ORDER BY timeline_sequence"
            )
        ).all()
    opened = [
        json.loads(
            keyring.decrypt(
                json.loads(row[1]),
                table="conversation_events",
                column="encrypted_content",
                row_id=row[0],
            ).decode("utf-8")
        )["text"]
        for row in rows
    ]
    assert opened == [f"msg evt-{n}" for n in range(1, 6)]
    assert [row[2] for row in rows] == ["op-1", "op-1", "op-2", None, None]


def test_legacy_ids_survive_only_as_aliases(migrated, keyring: KeyRing) -> None:
    engine, _, _ = migrated
    with engine.connect() as connection:
        aliases = {
            row[0]
            for row in connection.execute(
                text("SELECT alias_hmac FROM conversation_aliases")
            )
        }
        legacy = connection.execute(
            text(
                "SELECT event_id, encrypted_legacy_conversation_id "
                "FROM conversation_events ORDER BY timeline_sequence"
            )
        ).all()
    assert aliases == {
        alias_hmac(IDENTIFIER_KEY, "conv-a"),
        alias_hmac(IDENTIFIER_KEY, "conv-b"),
    }
    # No plaintext legacy id anywhere: the alias table stores digests, and the
    # per-event copy is sealed.
    assert "conv-a" not in str(aliases)
    recovered = [
        keyring.decrypt(
            json.loads(row[1]),
            table="conversation_events",
            column="encrypted_legacy_conversation_id",
            row_id=row[0],
        ).decode("utf-8")
        for row in legacy
    ]
    assert recovered == ["conv-a", "conv-a", "conv-b", "conv-b", "conv-a"]


def test_the_sequence_allocator_continues_after_the_backfill(migrated) -> None:
    engine, _, _ = migrated
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT next_sequence FROM conversations")
            ).scalar_one()
            == 6
        )


# -- F-B9: the migration log carries no plaintext ---------------------------


def test_migration_emits_no_plaintext_or_identifiers(
    tmp_path: Path, keyring: KeyRing, caplog
) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-secret": NOW},
        events=[("evt-1", "conv-secret", NOW, None)],
    )
    backup = _backup(engine, path, tmp_path)
    engine = create_database_engine(path)
    with caplog.at_level(logging.DEBUG):
        db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, backup))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "conv-secret" not in logged
    assert "msg evt-1" not in logged
    engine.dispose()


# -- F-B7: downgrade restores the legacy conversations ----------------------


def test_downgrade_restores_legacy_conversations_and_stays_readable(
    migrated, keyring: KeyRing
) -> None:
    engine, _, _ = migrated
    db.downgrade(
        engine,
        BASE_REVISION,
        cap001_inputs=MigrationInputs(
            keyring=keyring, identifier_key=IDENTIFIER_KEY
        ),
    )
    with engine.connect() as connection:
        conversations = {
            row[0]
            for row in connection.execute(
                text("SELECT conversation_id FROM conversations")
            )
        }
        rows = connection.execute(
            text(
                "SELECT event_id, conversation_id, encrypted_content "
                "FROM conversation_events ORDER BY event_id"
            )
        ).all()
        columns = {
            row[1]
            for row in connection.execute(
                text("PRAGMA table_info(conversation_events)")
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert {"conv-a", "conv-b"} <= conversations
    assert [(row[0], row[1]) for row in rows] == [
        ("evt-1", "conv-a"),
        ("evt-2", "conv-a"),
        ("evt-3", "conv-b"),
        ("evt-4", "conv-b"),
        ("evt-5", "conv-a"),
    ]
    # Still readable with the same key and the same AAD: the migration never
    # re-encrypted anything, so the downgrade has nothing to undo.
    for event_id, _, envelope in rows:
        keyring.decrypt(
            json.loads(envelope),
            table="conversation_events",
            column="encrypted_content",
            row_id=event_id,
        )
    assert "timeline_sequence" not in columns
    assert "context_sessions" not in tables


def test_downgrade_without_the_data_key_is_refused(migrated) -> None:
    engine, _, _ = migrated
    with pytest.raises(Cap001MigrationError):
        db.downgrade(engine, BASE_REVISION)


def test_downgrade_and_upgrade_again_round_trips(
    migrated, keyring: KeyRing, tmp_path: Path
) -> None:
    engine, path, _ = migrated
    db.downgrade(
        engine,
        BASE_REVISION,
        cap001_inputs=MigrationInputs(
            keyring=keyring, identifier_key=IDENTIFIER_KEY
        ),
    )
    engine.dispose()
    second_backup = tmp_path / "backup2.sqlite"
    shutil.copyfile(path, second_backup)
    engine = create_database_engine(path)
    db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, second_backup))
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM conversation_events")
            ).scalar_one()
            == 5
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM conversations WHERE is_canonical = 1")
            ).scalar_one()
            == 1
        )
    engine.dispose()


# -- a fresh database needs nothing ----------------------------------------


def test_a_fresh_database_migrates_with_no_key_material(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "fresh.sqlite")
    db.upgrade(engine, "head")
    with session_factory(engine)() as session:
        row = session.execute(
            text(
                "SELECT conversation_id, next_sequence, is_canonical "
                "FROM conversations"
            )
        ).one()
    assert row[0].startswith("tl_")
    assert (row[1], row[2]) == (1, 1)
    engine.dispose()


def test_an_empty_legacy_conversation_is_aliased_and_merged(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "empty-history.sqlite"
    engine = _legacy_database(
        path,
        keyring,
        conversations={"conv-empty": NOW},
        events=[],
    )
    engine.dispose()
    engine = create_database_engine(path)
    db.upgrade(engine, "head", cap001_inputs=_inputs(keyring, None))
    with engine.connect() as connection:
        conversations = connection.execute(
            text("SELECT conversation_id FROM conversations")
        ).all()
        aliases = connection.execute(
            text("SELECT alias_hmac FROM conversation_aliases")
        ).scalars().all()
    assert len(conversations) == 1
    assert aliases == [alias_hmac(IDENTIFIER_KEY, "conv-empty")]
    engine.dispose()


def test_only_one_canonical_timeline_can_exist(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "fresh.sqlite")
    db.upgrade(engine, "head")
    with engine.begin() as connection:
        with pytest.raises(Exception):
            connection.execute(
                text(
                    "INSERT INTO conversations (conversation_id, created_at, "
                    "next_sequence, is_canonical) VALUES ('tl_other', :now, 1, 1)"
                ),
                {"now": _stamp(NOW)},
            )
    engine.dispose()
