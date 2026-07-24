"""DEV-026 B: idempotent operation bookkeeping over the Agent database.

Offline: a real SQLite database from the Agent schema, no network and no model.
The focus is the two things that make the write path safe -- one operation per
client request key, and a cancel that never rewrites an accounting outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_agent.api.operation_state import StaleOperationVersionError
from personal_agent.api.operation_store import (
    chat_request_fingerprint,
    get_operation,
    mark_detached,
    open_operation,
    request_cancel,
    transition_operation,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import ApiRequest, Device, Operation
from personal_agent_core.errors import AppError, ErrorCode


NOW = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(_device())
        session.flush()
        yield session
    engine.dispose()


def _device(device_id: str = "dev-1") -> Device:
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


FP = chat_request_fingerprint(conversation_id="conv-1", text="午饭 45，个人支出")


def _open(session, key: str = "req-uuid-1", fingerprint: str = FP):
    return open_operation(
        session,
        device_id="dev-1",
        client_request_id=key,
        request_fingerprint=fingerprint,
        now=NOW,
    )


# --- the fingerprint ---------------------------------------------------------


def test_fingerprint_depends_only_on_meaning() -> None:
    a = chat_request_fingerprint(conversation_id="c1", text="午饭 45")
    b = chat_request_fingerprint(conversation_id="c1", text="午饭 45")
    c = chat_request_fingerprint(conversation_id="c1", text="午饭 46")
    d = chat_request_fingerprint(conversation_id="c2", text="午饭 45")
    assert a == b
    assert a != c
    assert a != d


# --- idempotent open ---------------------------------------------------------


def test_open_creates_a_single_accepted_operation(session) -> None:
    opened = _open(session)
    assert opened.created is True
    assert opened.operation.state == "accepted"
    assert opened.operation.state_version == 1
    # The client UUID is reused as the downstream idempotency key.
    assert opened.operation.idempotency_key == "req-uuid-1"
    assert session.query(Operation).count() == 1
    assert session.query(ApiRequest).count() == 1


def test_replaying_the_same_request_returns_the_same_operation(session) -> None:
    first = _open(session)
    session.flush()
    second = _open(session)
    assert second.created is False
    assert second.operation.operation_id == first.operation.operation_id
    assert session.query(Operation).count() == 1


def test_same_key_different_request_is_a_conflict(session) -> None:
    _open(session)
    session.flush()
    other = chat_request_fingerprint(conversation_id="conv-1", text="打车 30")
    with pytest.raises(AppError) as exc:
        _open(session, key="req-uuid-1", fingerprint=other)
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_a_key_reserved_by_a_non_chat_request_cannot_open_chat(session) -> None:
    session.add(
        ApiRequest(
            request_id="req-decision",
            device_id="dev-1",
            client_request_id="decision-key",
            request_fingerprint="decision-fingerprint",
            encrypted_request_payload=None,
            received_at=NOW,
        )
    )
    session.flush()
    with pytest.raises(AppError) as exc:
        _open(session, key="decision-key")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_different_keys_are_different_operations(session) -> None:
    a = _open(session, key="req-uuid-1")
    session.flush()
    b = _open(session, key="req-uuid-2")
    assert a.operation.operation_id != b.operation.operation_id
    assert session.query(Operation).count() == 2


# --- transitions -------------------------------------------------------------


def test_a_legal_transition_bumps_the_version(session) -> None:
    op = _open(session).operation
    session.flush()
    new_version = transition_operation(
        session,
        operation_id=op.operation_id,
        current_state="accepted",
        current_version=1,
        target_state="interpreting",
        now=NOW,
    )
    assert new_version == 2
    session.refresh(op)
    assert op.state == "interpreting"
    assert op.state_version == 2


def test_a_stale_version_cannot_transition(session) -> None:
    op = _open(session).operation
    session.flush()
    transition_operation(
        session,
        operation_id=op.operation_id,
        current_state="accepted",
        current_version=1,
        target_state="interpreting",
        now=NOW,
    )
    # A worker that still believes it is at v1 loses the compare-and-swap. The
    # target is legal from `accepted`, so it clears the safety table and fails
    # only on the stale (state, version) guard.
    with pytest.raises(StaleOperationVersionError):
        transition_operation(
            session,
            operation_id=op.operation_id,
            current_state="accepted",
            current_version=1,
            target_state="failed_safe",
            now=NOW,
        )


def test_a_terminal_transition_records_its_reason_and_result(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching", "source_in_progress",
                           "verifying"])
    transition_operation(
        session,
        operation_id=op.operation_id,
        current_state="verifying",
        current_version=op.state_version,
        target_state="succeeded",
        now=NOW,
        tool="finance.log_expense",
        safe_result="recXYZ",
    )
    session.refresh(op)
    assert op.state == "succeeded"
    assert op.tool == "finance.log_expense"
    assert op.safe_result == "recXYZ"


# --- cancellation that does not lie ------------------------------------------


def test_cancel_before_submit_produces_a_clean_cancellation(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching"])
    outcome = request_cancel(session, operation_id=op.operation_id, now=NOW)
    assert outcome.cancelled is True
    assert outcome.state == "cancelled_pre_submit"
    session.refresh(op)
    assert op.state == "cancelled_pre_submit"
    assert op.cancel_requested is True


def test_cancel_after_a_possible_submit_only_flags_and_does_not_lie(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching", "source_in_progress"])
    outcome = request_cancel(session, operation_id=op.operation_id, now=NOW)
    # The write may be in flight; the state is untouched and the flag records the
    # request, so the client is never told a possible write was rolled back.
    assert outcome.cancelled is False
    assert outcome.state == "source_in_progress"
    session.refresh(op)
    assert op.state == "source_in_progress"
    assert op.cancel_requested is True


def test_cancel_on_a_terminal_operation_is_a_truthful_no_op(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching", "source_in_progress",
                           "verifying", "succeeded"])
    outcome = request_cancel(session, operation_id=op.operation_id, now=NOW)
    assert outcome.cancelled is False
    assert outcome.state == "succeeded"
    session.refresh(op)
    assert op.cancel_requested is True


def test_cancel_racing_submit_keeps_the_flag_and_reports_the_new_state(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "cancel-race.sqlite")
    create_all(engine)
    factory = session_factory(engine)
    with factory() as setup:
        setup.add(_device())
        setup.commit()
        op = _open(setup, key="cancel-race").operation
        _advance(setup, op, ["interpreting", "dispatching"])
        setup.commit()
        operation_id = op.operation_id

    stale_session = factory()
    stale = stale_session.get(Operation, operation_id)
    assert stale.state == "dispatching"
    with factory() as worker:
        current = worker.get(Operation, operation_id)
        transition_operation(
            worker,
            operation_id=operation_id,
            current_state=current.state,
            current_version=current.state_version,
            target_state="source_in_progress",
            now=NOW,
        )
        worker.commit()

    outcome = request_cancel(
        stale_session, operation_id=operation_id, now=NOW
    )
    stale_session.commit()
    stale_session.close()

    assert outcome.cancelled is False
    assert outcome.state == "source_in_progress"
    with factory() as check:
        persisted = check.get(Operation, operation_id)
        assert persisted.state == "source_in_progress"
        assert persisted.cancel_requested is True
    engine.dispose()


def test_mark_detached_sets_the_flag_without_changing_state(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching", "source_in_progress"])
    mark_detached(session, operation_id=op.operation_id, now=NOW)
    session.refresh(op)
    assert op.client_detached is True
    assert op.state == "source_in_progress"


def test_cancel_on_an_unknown_operation_is_rejected(session) -> None:
    with pytest.raises(AppError) as exc:
        request_cancel(session, operation_id="op_nope", now=NOW)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    assert get_operation(session, "op_nope") is None


def _advance(session, op: Operation, states: list[str]) -> None:
    """Walk an operation through a legal sequence of states, for test setup."""
    for target in states:
        session.refresh(op)
        transition_operation(
            session,
            operation_id=op.operation_id,
            current_state=op.state,
            current_version=op.state_version,
            target_state=target,
            now=NOW,
        )
    session.refresh(op)
