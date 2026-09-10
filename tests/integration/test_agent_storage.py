"""DEV-005: the Agent database enforces its invariants, not just describes them."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DatabaseError, IntegrityError, StatementError

from personal_agent.storage import db
from personal_agent.storage.engine import (
    DatabaseIntegrityError,
    check_integrity,
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    OPERATION_STATES,
    TERMINAL_OPERATION_STATES,
    ApiRequest,
    AuthChallenge,
    Base,
    ContextSession,
    Conversation,
    ConversationEvent,
    DailyReview,
    Device,
    Operation,
)


NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
SEALED = {
    "v": 1,
    "kid": "agent-data-2026-01",
    "nonce": "AAAAAAAAAAAAAAAA",
    "ciphertext": "AAAA",
    "tag": "AAAAAAAAAAAAAAAAAAAAAA",
}


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine):
    with session_factory(engine)() as session:
        yield session


def make_device(device_id: str = "dev-1") -> Device:
    return Device(
        device_id=device_id,
        display_name="iPhone",
        public_key="BASE64URL",
        device_key_thumbprint="THUMB",
        status="active",
        scopes="[]",
        allowed_tools_version="v1",
        created_at=NOW,
    )


# --- PRAGMAs ---------------------------------------------------------------


def test_required_pragmas_are_applied_to_every_connection(engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000


def test_foreign_keys_are_actually_enforced(session) -> None:
    # SQLite ignores foreign keys unless the PRAGMA is on, so a declared key is
    # decoration until this passes.
    session.add(
        AuthChallenge(
            challenge_id="ch-1",
            device_id="no-such-device",
            nonce_hash="hash",
            created_at=NOW,
            expires_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_integrity_check_passes_on_a_healthy_database(engine) -> None:
    check_integrity(engine)


def test_integrity_check_raises_on_a_damaged_database(tmp_path: Path) -> None:
    path = tmp_path / "damaged.sqlite"
    engine = create_database_engine(path)
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(make_device())
        session.commit()
    engine.dispose()

    payload = bytearray(path.read_bytes())
    # Corrupt a b-tree page well past the header so the file still opens.
    for offset in range(4096, min(6144, len(payload))):
        payload[offset] = 0xFF
    path.write_bytes(bytes(payload))

    reopened = create_database_engine(path)
    with pytest.raises((DatabaseIntegrityError, DatabaseError)):
        check_integrity(reopened)
    reopened.dispose()


# --- migrations ------------------------------------------------------------


def test_upgrade_creates_exactly_the_declared_schema(tmp_path: Path) -> None:
    path = tmp_path / "migrated.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine)

    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    engine.dispose()


def test_downgrade_returns_to_an_empty_database(tmp_path: Path) -> None:
    # A migration without a working downgrade has no rollback path, which the
    # deployment plan requires before any forward-only change.
    path = tmp_path / "rollback.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine)
    assert set(inspect(engine).get_table_names()) - {"alembic_version"}

    db.downgrade(engine, "base")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()
    engine.dispose()


def test_receipt_record_migration_round_trips_a_populated_database(
    tmp_path: Path,
) -> None:
    """`0006` adds the `G1` business fields to a database that already has rows.

    Two things are proven here that the model alone cannot: that the SQLite
    batch copy keeps existing operations, and that the check constraint really
    exists in the database rather than only in `__table_args__`. A constraint
    that lives only in the model is not a constraint -- recovery, the reconciler
    and a migration all write through raw SQL.
    """
    path = tmp_path / "receipt-record-migration.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine, "0005_finance_safe_retry")
    timestamp = "2026-08-15T00:00:00Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev', 'phone', 'key', 'thumb', 'active', "
                "'[]', 'v1', :timestamp)"
            ),
            {"timestamp": timestamp},
        )
        for suffix in ("written", "refused"):
            connection.execute(
                text(
                    "INSERT INTO api_requests (request_id, device_id, "
                    "client_request_id, request_fingerprint, received_at) "
                    "VALUES (:request_id, 'dev', :client_id, :fingerprint, "
                    ":timestamp)"
                ),
                {
                    "request_id": f"req-{suffix}",
                    "client_id": f"client-{suffix}",
                    "fingerprint": f"fp-{suffix}",
                    "timestamp": timestamp,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO operations (operation_id, request_id, trace_id, "
                    "idempotency_key, state, state_version, cancel_requested, "
                    "client_detached, created_at, updated_at) VALUES "
                    "(:operation_id, :request_id, :trace_id, :key, :state, 1, "
                    "0, 0, :timestamp, :timestamp)"
                ),
                {
                    "operation_id": f"op-{suffix}",
                    "request_id": f"req-{suffix}",
                    "trace_id": f"trace-{suffix}",
                    "key": f"key-{suffix}",
                    "state": "succeeded" if suffix == "written" else "failed_safe",
                    "timestamp": timestamp,
                },
            )

    db.upgrade(engine)
    envelope = '{"v": 1, "ct": "x"}'
    with engine.begin() as connection:
        # No backfill: a write that predates `G1` says so by holding NULL.
        assert connection.execute(
            text("SELECT encrypted_result_record FROM operations")
        ).scalars().all() == [None, None]
        connection.execute(
            text(
                "UPDATE operations SET encrypted_result_record = :envelope "
                "WHERE operation_id = 'op-written'"
            ),
            {"envelope": envelope},
        )

    with engine.begin() as connection:
        # A `failed_safe` row carrying a name and an amount would render as a
        # receipt for a write that never happened.
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE operations SET encrypted_result_record = :envelope "
                    "WHERE operation_id = 'op-refused'"
                ),
                {"envelope": envelope},
            )

    db.downgrade(engine, "0005_finance_safe_retry")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT operation_id FROM operations ORDER BY operation_id")
        ).scalars().all() == ["op-refused", "op-written"]
        assert "encrypted_result_record" not in {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(operations)"))
        }
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_finance_retry_migration_round_trips_a_populated_database(
    tmp_path: Path,
) -> None:
    """SQLite batch-copy keeps existing rows and the self-reference usable."""
    path = tmp_path / "retry-migration.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine, "0004_manual_review_resolution")
    timestamp = "2026-08-11T00:00:00Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev', 'phone', 'key', 'thumb', 'active', "
                "'[]', 'v1', :timestamp)"
            ),
            {"timestamp": timestamp},
        )
        for suffix in ("source", "retry"):
            connection.execute(
                text(
                    "INSERT INTO api_requests (request_id, device_id, "
                    "client_request_id, request_fingerprint, received_at) "
                    "VALUES (:request_id, 'dev', :client_id, :fingerprint, "
                    ":timestamp)"
                ),
                {
                    "request_id": f"req-{suffix}",
                    "client_id": f"client-{suffix}",
                    "fingerprint": f"fp-{suffix}",
                    "timestamp": timestamp,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO operations (operation_id, request_id, trace_id, "
                    "idempotency_key, state, state_version, cancel_requested, "
                    "client_detached, created_at, updated_at) VALUES "
                    "(:operation_id, :request_id, :trace_id, :key, :state, 1, "
                    "0, 0, :timestamp, :timestamp)"
                ),
                {
                    "operation_id": f"op-{suffix}",
                    "request_id": f"req-{suffix}",
                    "trace_id": f"trace-{suffix}",
                    "key": f"key-{suffix}",
                    "state": "failed_safe" if suffix == "source" else "accepted",
                    "timestamp": timestamp,
                },
            )

    db.upgrade(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE operations SET retry_of_operation_id='op-source' "
                "WHERE operation_id='op-retry'"
            )
        )
        assert connection.execute(
            text("PRAGMA foreign_key_check")
        ).all() == []

    db.downgrade(engine, "0004_manual_review_resolution")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT operation_id FROM operations ORDER BY operation_id")
        ).scalars().all() == ["op-retry", "op-source"]
        assert "retry_of_operation_id" not in {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(operations)"))
        }
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    db.upgrade(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT retry_of_operation_id FROM operations")
        ).scalars().all() == [None, None]
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_calendar_override_migration_round_trips_a_populated_database(
    tmp_path: Path,
) -> None:
    """Design 3.3's three columns survive a downgrade and an upgrade.

    Two of them are load-bearing in a way a plain `ADD COLUMN` would not show:
    the lineage is a self-referencing foreign key (so the rebuild has to keep
    `foreign_keys=ON` honest through a table copy) and `parent_operation_derives_once`
    is the constraint that makes a double tap one operation. Both are asserted
    here against real rows rather than only against the schema.
    """
    path = tmp_path / "calendar-override-migration.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine, "0009_device_action_seal")
    timestamp = "2026-09-07T00:00:00Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (device_id, display_name, public_key, "
                "device_key_thumbprint, status, scopes, allowed_tools_version, "
                "created_at) VALUES ('dev', 'phone', 'key', 'thumb', 'active', "
                "'[]', 'v1', :timestamp)"
            ),
            {"timestamp": timestamp},
        )
        for suffix in ("source", "derived", "other"):
            connection.execute(
                text(
                    "INSERT INTO api_requests (request_id, device_id, "
                    "client_request_id, request_fingerprint, received_at) "
                    "VALUES (:request_id, 'dev', :client_id, :fingerprint, "
                    ":timestamp)"
                ),
                {
                    "request_id": f"req-{suffix}",
                    "client_id": f"client-{suffix}",
                    "fingerprint": f"fp-{suffix}",
                    "timestamp": timestamp,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO operations (operation_id, request_id, trace_id, "
                    "idempotency_key, state, state_version, cancel_requested, "
                    "client_detached, created_at, updated_at) VALUES "
                    "(:operation_id, :request_id, :trace_id, :key, "
                    "'source_in_progress', 1, 0, 0, :timestamp, :timestamp)"
                ),
                {
                    "operation_id": f"op-{suffix}",
                    "request_id": f"req-{suffix}",
                    "trace_id": f"trace-{suffix}",
                    "key": f"key-{suffix}",
                    "timestamp": timestamp,
                },
            )

    db.upgrade(engine)
    with engine.begin() as connection:
        # No backfill: a write that predates the override has no parent and no
        # report, which is exactly what NULL means here.
        assert connection.execute(
            text("SELECT parent_operation_id FROM operations")
        ).scalars().all() == [None, None, None]
        connection.execute(
            text(
                "UPDATE operations SET parent_operation_id = 'op-source', "
                "device_result = 'duplicate' WHERE operation_id = 'op-derived'"
            )
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    with engine.begin() as connection:
        # One derived operation per source, enforced rather than intended: the
        # unique index is what makes a double tap read the first one back.
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE operations SET parent_operation_id = 'op-source' "
                    "WHERE operation_id = 'op-other'"
                )
            )
    with engine.begin() as connection:
        # The report vocabulary is closed at the storage layer too, so a typo
        # cannot make an operation look overridable -- or hide one that is.
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE operations SET device_result = 'already_there' "
                    "WHERE operation_id = 'op-other'"
                )
            )
    with engine.begin() as connection:
        # An operation cannot be its own origin.
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE operations SET parent_operation_id = 'op-other' "
                    "WHERE operation_id = 'op-other'"
                )
            )

    db.downgrade(engine, "0009_device_action_seal")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT operation_id FROM operations ORDER BY operation_id")
        ).scalars().all() == ["op-derived", "op-other", "op-source"]
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(operations)"))
        }
        assert not {"encrypted_request", "parent_operation_id", "device_result"} & columns
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    db.upgrade(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT parent_operation_id FROM operations")
        ).scalars().all() == [None, None, None]
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


# --- invariants ------------------------------------------------------------


def test_one_client_request_id_per_device(session) -> None:
    session.add(make_device())
    session.commit()
    for request_id in ("req-1", "req-2"):
        session.add(
            ApiRequest(
                request_id=request_id,
                device_id="dev-1",
                client_request_id="same-key",
                request_fingerprint="fp",
                received_at=NOW,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_idempotency_key_is_globally_unique(session) -> None:
    session.add(make_device())
    session.flush()
    for suffix in ("1", "2"):
        session.add(
            ApiRequest(
                request_id=f"req-{suffix}",
                device_id="dev-1",
                client_request_id=f"client-{suffix}",
                request_fingerprint="fp",
                received_at=NOW,
            )
        )
        session.add(
            Operation(
                operation_id=f"op-{suffix}",
                request_id=f"req-{suffix}",
                trace_id="tr",
                idempotency_key="shared-key",
                state="accepted",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_one_failed_operation_can_be_the_source_of_only_one_retry(session) -> None:
    session.add(make_device())
    session.flush()
    for suffix in ("source", "retry-1", "retry-2"):
        session.add(
            ApiRequest(
                request_id=f"req-{suffix}",
                device_id="dev-1",
                client_request_id=f"client-{suffix}",
                request_fingerprint=f"fp-{suffix}",
                received_at=NOW,
            )
        )
    session.flush()
    session.add(
        Operation(
            operation_id="op-source",
            request_id="req-source",
            trace_id="tr-source",
            idempotency_key="key-source",
            state="failed_safe",
            failure_reason="model_unavailable",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    for suffix in ("1", "2"):
        session.add(
            Operation(
                operation_id=f"op-retry-{suffix}",
                request_id=f"req-retry-{suffix}",
                trace_id=f"tr-retry-{suffix}",
                idempotency_key=f"key-retry-{suffix}",
                retry_of_operation_id="op-source",
                state="accepted",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_unknown_operation_states_are_rejected(session) -> None:
    session.add(make_device())
    session.add(
        ApiRequest(
            request_id="req-1",
            device_id="dev-1",
            client_request_id="client-1",
            request_fingerprint="fp",
            received_at=NOW,
        )
    )
    session.flush()
    session.add(
        Operation(
            operation_id="op-1",
            request_id="req-1",
            trace_id="tr",
            idempotency_key="key-1",
            state="cancelled",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_cancellation_is_a_flag_and_only_pre_submit_is_terminal() -> None:
    # A client giving up must never be recordable as a cancelled ledger write.
    assert "cancelled" not in OPERATION_STATES
    assert "cancelled_pre_submit" in TERMINAL_OPERATION_STATES
    flags = {column.name for column in Operation.__table__.columns}
    assert {"cancel_requested", "client_detached"}.issubset(flags)


def test_one_review_card_per_date(session) -> None:
    for review_id in ("rev-1", "rev-2"):
        session.add(
            DailyReview(
                review_id=review_id,
                review_date="2026-07-22",
                status="pending",
                created_at=NOW,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_revoked_device_must_carry_its_revocation_time(session) -> None:
    device = make_device()
    device.status = "revoked"
    session.add(device)
    with pytest.raises(IntegrityError):
        session.commit()


# --- column types ----------------------------------------------------------


def test_naive_timestamps_are_refused(session) -> None:
    device = make_device()
    device.created_at = datetime(2026, 7, 23, 15, 0)
    session.add(device)
    with pytest.raises((StatementError, ValueError)):
        session.commit()


def test_timestamps_round_trip_as_utc(engine) -> None:
    with session_factory(engine)() as session:
        session.add(make_device())
        session.commit()
    with session_factory(engine)() as session:
        stored = session.get(Device, "dev-1")
        assert stored is not None
        assert stored.created_at == NOW
        assert stored.created_at.tzinfo is not None

    with engine.connect() as connection:
        raw = connection.execute(
            text("SELECT created_at FROM devices")
        ).scalar_one()
    assert raw == "2026-07-23T07:00:00.000000Z"


def test_encrypted_columns_refuse_plaintext(session) -> None:
    session.add(make_device())
    session.commit()
    session.add(
        ConversationEvent(
            event_id="ev-1",
            conversation_id="conv-1",
            event_type="user_message",
            encrypted_content="午饭45，个人支出",  # type: ignore[arg-type]
            created_at=NOW,
        )
    )
    with pytest.raises((StatementError, ValueError)):
        session.commit()


def test_encrypted_columns_refuse_an_incomplete_envelope(session) -> None:
    session.add(
        ConversationEvent(
            event_id="ev-2",
            conversation_id="conv-1",
            event_type="user_message",
            encrypted_content={"v": 1, "ciphertext": "AAAA"},
            created_at=NOW,
        )
    )
    with pytest.raises((StatementError, ValueError)):
        session.commit()


def test_encrypted_columns_refuse_plaintext_hidden_in_an_extra_field(engine) -> None:
    with session_factory(engine)() as session:
        session.add(Conversation(conversation_id="conv-1", created_at=NOW))
        session.add(
            ConversationEvent(
                event_id="ev-extra",
                conversation_id="conv-1",
                event_type="user_message",
                encrypted_content={
                    **SEALED,
                    "plaintext": "午饭45，个人支出",
                },
                created_at=NOW,
            )
        )
        with pytest.raises((StatementError, ValueError)):
            session.commit()


def test_a_sealed_envelope_is_accepted_and_round_trips(engine) -> None:
    with session_factory(engine)() as session:
        session.add(Conversation(conversation_id="conv-1", created_at=NOW))
        # `CAP-001`: an event now carries its ordering and grouping keys, and
        # the columns are `NOT NULL` precisely so an untracked event cannot
        # exist.
        session.add(
            ContextSession(
                session_id="ses-1",
                conversation_id="conv-1",
                status="open",
                relation_kind="new_topic",
                opened_at=NOW,
            )
        )
        session.add(
            ConversationEvent(
                event_id="ev-3",
                conversation_id="conv-1",
                timeline_sequence=1,
                session_id="ses-1",
                turn_id="trn-1",
                event_type="user_message",
                encrypted_content=SEALED,
                created_at=NOW,
            )
        )
        session.commit()
    with session_factory(engine)() as session:
        stored = session.get(ConversationEvent, "ev-3")
        assert stored is not None
        assert stored.encrypted_content == SEALED
