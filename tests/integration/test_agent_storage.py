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
    assert raw == "2026-07-23T07:00:00Z"


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


def test_a_sealed_envelope_is_accepted_and_round_trips(engine) -> None:
    with session_factory(engine)() as session:
        session.add(Conversation(conversation_id="conv-1", created_at=NOW))
        session.add(
            ConversationEvent(
                event_id="ev-3",
                conversation_id="conv-1",
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
