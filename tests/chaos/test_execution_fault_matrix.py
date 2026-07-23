"""DEV-006: the fault matrix from technical design 7.6.4.

Each test kills the "process" at a different point by disposing the engine and
reopening the database, then runs the recovery scan a fresh worker would run.
The property under test is always the same: exactly one external record, and a
final state that does not lie about it.

A crash here is simulated at the durability boundary rather than with real
signals. Whatever was committed survives, whatever was not is gone, which is the
only distinction recovery is allowed to depend on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import (
    UnverifiedReceiptError,
    acquire_recovery_lease,
    acquire_resource_lock,
    append_audit_event,
    prepare_execution,
    record_receipt,
    release_resource_lock,
    renew_resource_lock,
    resume_after_restart,
    transition,
    verify_audit_chain,
)
from personal_data_mcp.storage.models import ExternalReceipt, ToolExecution
from personal_data_mcp.storage.state_machine import StaleStateVersionError


KEY = "018f0000-0000-4000-8000-000000000001"
TOKEN = "018f0000-0000-4000-8000-0000000000ff"
T0 = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
SEALED = {
    "v": 1,
    "kid": "finance-data-2026-01",
    "nonce": "AAAAAAAAAAAAAAAA",
    "ciphertext": "AAAA",
    "tag": "AAAAAAAAAAAAAAAAAAAAAA",
}


class Node:
    """A service instance over a database file, disposable like a process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.engine = create_database_engine(path)
        self.factory = session_factory(self.engine)

    def session(self):
        return self.factory()

    def crash(self) -> "Node":
        """Drop this instance and return a cold one over the same file."""
        self.engine.dispose()
        return Node(self.path)


@pytest.fixture()
def node(tmp_path: Path) -> Node:
    node = Node(tmp_path / "finance.sqlite")
    create_all(node.engine)
    return node


def prepare(node: Node) -> None:
    with node.session() as session:
        prepare_execution(
            session,
            idempotency_key=KEY,
            tool="finance.log_expense",
            request_fingerprint="fp",
            client_token=TOKEN,
            encrypted_payload=SEALED,
            now=T0,
        )
        session.commit()


def advance(node: Node, target: str, **kwargs) -> None:
    with node.session() as session:
        execution = session.get(ToolExecution, KEY)
        assert execution is not None
        transition(
            session,
            idempotency_key=KEY,
            current_state=execution.state,
            current_version=execution.state_version,
            target_state=target,
            now=T0,
            **kwargs,
        )
        session.commit()


def state_of(node: Node) -> str:
    with node.session() as session:
        execution = session.get(ToolExecution, KEY)
        assert execution is not None
        return execution.state


def receipts(node: Node) -> list[str]:
    with node.session() as session:
        return [r.record_id for r in session.query(ExternalReceipt).all()]


def recover(node: Node, owner: str = "worker-2") -> list[tuple[str, str]]:
    with node.session() as session:
        claimed = resume_after_restart(session, owner=owner, now=T0)
        session.commit()
        return claimed


# --- the seven breakpoints --------------------------------------------------


def test_crash_before_prepare_leaves_nothing_to_recover(node: Node) -> None:
    restarted = node.crash()
    assert recover(restarted) == []
    with restarted.session() as session:
        assert session.get(ToolExecution, KEY) is None


def test_crash_after_prepare_may_still_submit_under_the_same_token(
    node: Node,
) -> None:
    prepare(node)
    restarted = node.crash()

    assert recover(restarted) == [(KEY, "prepared")]
    assert state_of(restarted) == "prepared"
    with restarted.session() as session:
        execution = session.get(ToolExecution, KEY)
        assert execution is not None
        # The token was persisted before any network activity, so the first
        # real submit still replays this exact value.
        assert execution.client_token == TOKEN
        assert execution.encrypted_payload == SEALED


def test_crash_while_submitting_becomes_commit_unknown_not_never_sent(
    node: Node,
) -> None:
    prepare(node)
    advance(node, "submitting")
    restarted = node.crash()

    assert recover(restarted) == [(KEY, "commit_unknown")]
    assert state_of(restarted) == "commit_unknown"
    assert receipts(restarted) == []


def test_feishu_received_the_write_but_the_response_was_lost(node: Node) -> None:
    prepare(node)
    advance(node, "submitting")
    restarted = node.crash()
    recover(restarted)

    # Reconciliation under the same client token finds the record that was in
    # fact created, and adopts it instead of writing a second one.
    with restarted.session() as session:
        execution = session.get(ToolExecution, KEY)
        assert execution is not None
        version = transition(
            session,
            idempotency_key=KEY,
            current_state="commit_unknown",
            current_version=execution.state_version,
            target_state="reconciling_same_client_token",
            now=T0,
        )
        record_receipt(
            session,
            receipt_id="rc-1",
            idempotency_key=KEY,
            table_kind="expense",
            record_id="rec_from_lost_response",
            now=T0,
        )
        transition(
            session,
            idempotency_key=KEY,
            current_state="reconciling_same_client_token",
            current_version=version,
            target_state="committed_unverified",
            now=T0,
        )
        session.commit()

    assert receipts(restarted) == ["rec_from_lost_response"]


def test_crash_after_record_id_but_before_read_back(node: Node) -> None:
    prepare(node)
    advance(node, "submitting")
    with node.session() as session:
        record_receipt(
            session,
            receipt_id="rc-1",
            idempotency_key=KEY,
            table_kind="expense",
            record_id="rec_1",
            now=T0,
        )
        session.commit()
    advance(node, "committed_unverified")
    restarted = node.crash()

    # Nothing is re-created: the recorded record id is what gets verified.
    assert recover(restarted) == [(KEY, "committed_unverified")]
    assert receipts(restarted) == ["rec_1"]


def test_a_verified_read_back_is_the_only_route_to_success(node: Node) -> None:
    prepare(node)
    advance(node, "submitting")
    with node.session() as session:
        record_receipt(
            session,
            receipt_id="rc-1",
            idempotency_key=KEY,
            table_kind="expense",
            record_id="rec_1",
            now=T0,
            verified=True,
        )
        session.commit()
    advance(node, "committed_unverified")
    advance(node, "succeeded")

    restarted = node.crash()
    assert recover(restarted) == []
    assert state_of(restarted) == "succeeded"
    assert receipts(restarted) == ["rec_1"]


def test_success_without_a_verified_receipt_is_rejected(node: Node) -> None:
    prepare(node)
    advance(node, "submitting")
    advance(node, "committed_unverified")

    with pytest.raises(UnverifiedReceiptError):
        advance(node, "succeeded")
    assert state_of(node) == "committed_unverified"

    with node.session() as session:
        record_receipt(
            session,
            receipt_id="rc-unverified",
            idempotency_key=KEY,
            table_kind="expense",
            record_id="rec_unverified",
            now=T0,
        )
        session.commit()

    with pytest.raises(UnverifiedReceiptError):
        advance(node, "succeeded")
    assert state_of(node) == "committed_unverified"


def test_cancelling_after_submit_cannot_produce_a_cancelled_outcome(
    node: Node,
) -> None:
    prepare(node)
    advance(node, "submitting")
    with pytest.raises(Exception) as excinfo:
        advance(node, "cancelled_pre_submit")
    assert "not permitted" in str(excinfo.value)
    assert state_of(node) == "submitting"


def test_cancelling_before_submit_is_allowed(node: Node) -> None:
    prepare(node)
    advance(node, "cancelled_pre_submit")
    assert state_of(node) == "cancelled_pre_submit"
    assert receipts(node) == []


# --- concurrency ------------------------------------------------------------


def test_only_one_worker_can_hold_a_recovery_lease(node: Node) -> None:
    prepare(node)
    advance(node, "submitting")
    with node.session() as session:
        assert acquire_recovery_lease(
            session, idempotency_key=KEY, owner="worker-1", now=T0
        )
        session.commit()

    other = node.crash()
    with other.session() as session:
        assert not acquire_recovery_lease(
            session, idempotency_key=KEY, owner="worker-2", now=T0
        )
        # Only once the lease has expired may another worker take over.
        assert acquire_recovery_lease(
            session,
            idempotency_key=KEY,
            owner="worker-2",
            now=T0 + timedelta(seconds=31),
        )
        session.commit()


def test_resource_lock_can_only_be_renewed_by_its_live_owner(node: Node) -> None:
    with node.session() as session:
        assert acquire_resource_lock(
            session,
            lock_key="family-fund-2026",
            owner="worker-1",
            now=T0,
        )
        session.commit()

    with node.session() as session:
        assert not renew_resource_lock(
            session,
            lock_key="family-fund-2026",
            owner="worker-2",
            now=T0 + timedelta(seconds=10),
        )
        assert renew_resource_lock(
            session,
            lock_key="family-fund-2026",
            owner="worker-1",
            now=T0 + timedelta(seconds=10),
        )
        session.commit()

    with node.session() as session:
        assert not acquire_resource_lock(
            session,
            lock_key="family-fund-2026",
            owner="worker-2",
            now=T0 + timedelta(seconds=31),
        )
        assert release_resource_lock(
            session, lock_key="family-fund-2026", owner="worker-1"
        )
        session.commit()


def test_a_stale_worker_cannot_overwrite_newer_state(node: Node) -> None:
    prepare(node)
    with node.session() as session:
        execution = session.get(ToolExecution, KEY)
        assert execution is not None
        stale_version = execution.state_version
        transition(
            session,
            idempotency_key=KEY,
            current_state="prepared",
            current_version=stale_version,
            target_state="submitting",
            now=T0,
        )
        session.commit()

    with node.session() as session:
        with pytest.raises(StaleStateVersionError):
            transition(
                session,
                idempotency_key=KEY,
                current_state="prepared",
                current_version=stale_version,
                target_state="failed_safe",
                now=T0,
            )
    assert state_of(node) == "submitting"


def test_replaying_a_key_with_different_arguments_is_a_conflict(
    node: Node,
) -> None:
    prepare(node)
    with node.session() as session:
        # Same request: idempotent, returns the existing row.
        again = prepare_execution(
            session,
            idempotency_key=KEY,
            tool="finance.log_expense",
            request_fingerprint="fp",
            client_token="another-token",
            encrypted_payload=SEALED,
            now=T0,
        )
        assert again.client_token == TOKEN

        from personal_agent_core.errors import AppError

        with pytest.raises(AppError) as excinfo:
            prepare_execution(
                session,
                idempotency_key=KEY,
                tool="finance.log_expense",
                request_fingerprint="different-fingerprint",
                client_token="another-token",
                encrypted_payload=SEALED,
                now=T0,
            )
        assert excinfo.value.code.value == "IDEMPOTENCY_CONFLICT"


# --- audit chain ------------------------------------------------------------


def test_the_audit_chain_detects_an_edited_event(node: Node) -> None:
    with node.session() as session:
        for index in range(3):
            append_audit_event(
                session,
                event_id=f"ev-{index}",
                trace_id="tr-1",
                event_type="tool_execution",
                redacted_summary=f"step {index}",
                now=T0 + timedelta(seconds=index),
            )
        session.commit()

    with node.session() as session:
        assert verify_audit_chain(session) == []

    with node.session() as session:
        from personal_data_mcp.storage.models import AuditEvent

        event = session.query(AuditEvent).filter_by(event_id="ev-1").one()
        assert event is not None
        event.redacted_summary = "step 1 (edited)"
        session.commit()

    with node.session() as session:
        broken = verify_audit_chain(session)
        assert "ev-1" in broken


def test_same_second_audit_events_follow_append_order_not_uuid_order(
    node: Node,
) -> None:
    with node.session() as session:
        append_audit_event(
            session,
            event_id="ev-z",
            trace_id="tr-1",
            event_type="tool_execution",
            redacted_summary="first",
            now=T0,
        )
        append_audit_event(
            session,
            event_id="ev-a",
            trace_id="tr-1",
            event_type="tool_execution",
            redacted_summary="second",
            now=T0,
        )
        session.commit()
        assert verify_audit_chain(session) == []
