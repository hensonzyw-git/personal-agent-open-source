"""DEV-006: the Finance database enforces the side-effect invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, StatementError

from personal_data_mcp.storage import db
from personal_data_mcp.storage.engine import (
    check_integrity,
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import (
    AuditEvent,
    Base,
    DuplicateCheck,
    ExternalReceipt,
    FxEvidence,
    ToolExecution,
)


NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
SEALED = {
    "v": 1,
    "kid": "finance-data-2026-01",
    "nonce": "AAAAAAAAAAAAAAAA",
    "ciphertext": "AAAA",
    "tag": "AAAAAAAAAAAAAAAAAAAAAA",
}


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine):
    with session_factory(engine)() as session:
        yield session


def make_execution(
    key: str = "key-1", state: str = "prepared", **overrides
) -> ToolExecution:
    fields = {
        "idempotency_key": key,
        "tool": "finance.log_expense",
        "request_fingerprint": "fp",
        "state": state,
        "client_token": f"token-{key}",
        "created_at": NOW,
        "updated_at": NOW,
    }
    if state not in {"prepared", "failed_safe", "cancelled_pre_submit"}:
        fields["submitted_at"] = NOW
    fields.update(overrides)
    return ToolExecution(**fields)


# --- schema and migrations --------------------------------------------------


def test_pragmas_and_integrity(engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    check_integrity(engine)


def test_upgrade_and_downgrade_round_trip(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "migrated.sqlite")
    db.upgrade(engine)
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set(
        Base.metadata.tables
    )
    db.downgrade(engine, "base")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()
    engine.dispose()


def test_audit_anchor_migration_witnesses_the_existing_tail(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "audit-anchor-migration.sqlite")
    db.upgrade(engine, "0002_duplicate_check_idempotency_key")
    tail_hash = "a" * 64
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO audit_events
                    (event_id, trace_id, event_type, redacted_summary,
                     prev_hash, event_hash, created_at)
                VALUES
                    ('event-before-anchor', 'trace-before-anchor', 'test',
                     'redacted', NULL, :tail_hash, :created_at)
                """
            ),
            {"tail_hash": tail_hash, "created_at": NOW.isoformat()},
        )

    db.upgrade(engine)
    with engine.connect() as connection:
        anchor = connection.execute(
            text(
                "SELECT event_count, tail_hash FROM audit_chain_anchor "
                "WHERE anchor_id = 1"
            )
        ).one()
    assert anchor == (1, tail_hash)
    engine.dispose()


def test_the_two_service_schemas_are_disjoint() -> None:
    from personal_agent.storage.models import Base as AgentBase

    assert set(Base.metadata.tables).isdisjoint(set(AgentBase.metadata.tables))


# --- execution invariants ---------------------------------------------------


def test_one_row_per_idempotency_key(session) -> None:
    session.add(make_execution())
    session.commit()
    session.add(make_execution(state="submitting"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_client_tokens_are_unique_across_executions(session) -> None:
    session.add(make_execution("key-1"))
    session.add(make_execution("key-2", client_token="token-key-1"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_unknown_execution_states_are_rejected(session) -> None:
    session.add(make_execution(state="rolled_back", submitted_at=NOW))
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_possibly_submitted_row_must_record_when_it_submitted(session) -> None:
    # Without this, recovery cannot bound how long an execution has been
    # unresolved before escalating to manual review.
    session.add(make_execution(state="commit_unknown", submitted_at=None))
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_recovery_lease_needs_both_owner_and_expiry(session) -> None:
    session.add(make_execution(recovery_lease_owner="worker-1"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_complete_lease_is_accepted(session) -> None:
    session.add(
        make_execution(
            recovery_lease_owner="worker-1",
            recovery_lease_until=NOW + timedelta(seconds=30),
        )
    )
    session.commit()
    stored = session.get(ToolExecution, "key-1")
    assert stored is not None and stored.recovery_lease_owner == "worker-1"


# --- receipts ---------------------------------------------------------------


def test_one_receipt_per_execution_and_table(session) -> None:
    session.add(make_execution(state="committed_unverified"))
    session.flush()
    for receipt_id in ("rc-1", "rc-2"):
        session.add(
            ExternalReceipt(
                receipt_id=receipt_id,
                idempotency_key="key-1",
                source_system="feishu_bitable",
                table_kind="expense",
                record_id=f"rec_{receipt_id}",
                created_at=NOW,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_receipt_cannot_reference_a_missing_execution(session) -> None:
    session.add(
        ExternalReceipt(
            receipt_id="rc-1",
            idempotency_key="no-such-key",
            source_system="feishu_bitable",
            table_kind="expense",
            record_id="rec_1",
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_receipt_table_kind_is_restricted(session) -> None:
    session.add(make_execution(state="committed_unverified"))
    session.flush()
    session.add(
        ExternalReceipt(
            receipt_id="rc-1",
            idempotency_key="key-1",
            source_system="feishu_bitable",
            table_kind="assets",
            record_id="rec_1",
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


# --- fx evidence ------------------------------------------------------------


def test_the_rate_provider_is_pinned_and_the_quote_is_cny(session) -> None:
    session.add(make_execution())
    session.flush()
    session.add(
        FxEvidence(
            evidence_id="fx-1",
            idempotency_key="key-1",
            base_currency="JPY",
            quote_currency="CNY",
            rate="0.0479",
            provider="google_search",
            quote_date="2026-07-22",
            fetched_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_the_rate_is_stored_as_exact_text(session) -> None:
    session.add(make_execution())
    session.flush()
    session.add(
        FxEvidence(
            evidence_id="fx-1",
            idempotency_key="key-1",
            base_currency="JPY",
            quote_currency="CNY",
            rate="0.04791234567",
            provider="frankfurter_ecb",
            quote_date="2026-07-22",
            fetched_at=NOW,
        )
    )
    session.commit()
    stored = session.get(FxEvidence, "fx-1")
    assert stored is not None
    assert stored.rate == "0.04791234567"


# --- duplicate checks -------------------------------------------------------


def test_candidate_record_ids_must_be_sealed(session) -> None:
    session.add(
        DuplicateCheck(
            check_id="dup-1",
            intent_fingerprint="fp",
            encrypted_candidate_record_ids=["rec_plain"],  # type: ignore[arg-type]
            status="awaiting_decision",
            created_at=NOW,
            expires_at=NOW,
        )
    )
    with pytest.raises((StatementError, ValueError)):
        session.commit()


def test_a_decided_check_must_record_when(session) -> None:
    session.add(
        DuplicateCheck(
            check_id="dup-1",
            intent_fingerprint="fp",
            encrypted_candidate_record_ids=SEALED,
            status="write_anyway",
            created_at=NOW,
            expires_at=NOW,
            decided_at=None,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


# --- audit ------------------------------------------------------------------


def test_audit_event_hashes_are_unique(session) -> None:
    for event_id in ("ev-1", "ev-2"):
        session.add(
            AuditEvent(
                event_id=event_id,
                trace_id="tr-1",
                event_type="tool_execution",
                redacted_summary="finance.log_expense prepared",
                prev_hash=None,
                event_hash="same-hash",
                created_at=NOW,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()
