"""DEV-026 C: cross-database recovery projects Finance truth, never a guess.

The planner is pure and tested exhaustively; the applier and the startup scan run
against a real Agent SQLite database with an injected fake Finance reader. No
network and no Finance database are involved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_agent.api.operation_store import open_operation, transition_operation
from personal_agent.api.recovery import (
    FinanceExecutionStatus,
    RecoveryAction,
    apply_recovery,
    plan_recovery,
    recover_pending,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Device, Operation


NOW = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id="dev-1",
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint="T",
                status="active",
                scopes="[]",
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.flush()
        yield session
    engine.dispose()


def _status(state: str, *, record_id: str | None = None, verified: bool = False):
    return FinanceExecutionStatus(
        state=state, record_id=record_id, receipt_verified=verified
    )


# --- the pure planner --------------------------------------------------------


def test_non_recoverable_states_are_a_programming_error() -> None:
    with pytest.raises(ValueError):
        plan_recovery("accepted", None)


def test_finance_success_with_a_verified_receipt_resolves_with_the_record() -> None:
    plan = plan_recovery(
        "verifying", _status("succeeded", record_id="rec123", verified=True)
    )
    assert plan.action is RecoveryAction.RESOLVE
    assert plan.target_state == "succeeded"
    assert plan.safe_result == "rec123"


@pytest.mark.parametrize(
    "status",
    [
        _status("succeeded", record_id=None, verified=True),
        _status("succeeded", record_id="rec123", verified=False),
        _status("succeeded", record_id="  ", verified=True),
    ],
)
def test_finance_success_without_verified_proof_escalates(status) -> None:
    plan = plan_recovery("verifying", status)
    assert plan.target_state == "needs_manual_review"


def test_finance_failed_safe_and_manual_review_project_through() -> None:
    assert plan_recovery("dispatching", _status("failed_safe")).target_state == (
        "failed_safe"
    )
    assert plan_recovery(
        "source_in_progress", _status("needs_manual_review")
    ).target_state == "needs_manual_review"


def test_finance_cancel_only_projects_when_agent_is_pre_submit() -> None:
    assert plan_recovery(
        "dispatching", _status("cancelled_pre_submit")
    ).target_state == "cancelled_pre_submit"
    # A post-submit operation cannot honestly be cancelled; the disagreement is
    # escalated instead.
    assert plan_recovery(
        "source_in_progress", _status("cancelled_pre_submit")
    ).target_state == "needs_manual_review"


def test_no_finance_execution_fails_safe_without_a_replayable_intent() -> None:
    pre_submit = plan_recovery("dispatching", None)
    assert pre_submit.action is RecoveryAction.RESOLVE
    assert pre_submit.target_state == "failed_safe"
    past = plan_recovery("source_in_progress", None)
    assert past.action is RecoveryAction.RESOLVE
    assert past.target_state == "needs_manual_review"


def test_finance_prepared_leaves_a_pre_submit_operation_but_flags_a_conflict() -> None:
    assert plan_recovery("dispatching", _status("prepared")).action is (
        RecoveryAction.LEAVE
    )
    assert plan_recovery(
        "verifying", _status("prepared")
    ).target_state == "needs_manual_review"


def test_an_unknown_finance_state_fails_closed_to_manual_review() -> None:
    pre_submit = plan_recovery("dispatching", _status("corrupted"))
    post_submit = plan_recovery("source_in_progress", _status("corrupted"))
    assert pre_submit.target_state == "needs_manual_review"
    assert post_submit.target_state == "needs_manual_review"


@pytest.mark.parametrize(
    "finance_state",
    ["submitting", "commit_unknown", "reconciling_same_client_token",
     "committed_unverified"],
)
def test_a_possible_submit_advances_a_dispatching_operation(finance_state) -> None:
    plan = plan_recovery("dispatching", _status(finance_state))
    assert plan.action is RecoveryAction.ADVANCE_IN_PROGRESS


@pytest.mark.parametrize(
    "finance_state",
    ["submitting", "commit_unknown", "committed_unverified"],
)
def test_a_possible_submit_leaves_an_already_in_progress_operation(finance_state):
    plan = plan_recovery("source_in_progress", _status(finance_state))
    assert plan.action is RecoveryAction.LEAVE


# --- the applier, on a real database -----------------------------------------


def _operation_at(session, state: str, key: str = "req-1") -> Operation:
    op = open_operation(
        session,
        device_id="dev-1",
        client_request_id=key,
        request_fingerprint="fp",
        now=NOW,
    ).operation
    session.flush()
    path = {
        "dispatching": ["interpreting", "dispatching"],
        "source_in_progress": ["interpreting", "dispatching", "source_in_progress"],
        "verifying": ["interpreting", "dispatching", "source_in_progress",
                      "verifying"],
    }[state]
    for target in path:
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
    return op


def test_applying_success_walks_to_succeeded_with_the_record(session) -> None:
    op = _operation_at(session, "dispatching")
    plan = apply_recovery(
        session, op, _status("succeeded", record_id="rec9", verified=True), now=NOW
    )
    assert plan.action is RecoveryAction.RESOLVE
    session.refresh(op)
    assert op.state == "succeeded"
    assert op.safe_result == "rec9"


def test_applying_a_possible_submit_advances_but_does_not_resolve(session) -> None:
    op = _operation_at(session, "dispatching")
    apply_recovery(session, op, _status("submitting"), now=NOW)
    session.refresh(op)
    # It is no longer pre-submit, so a pending cancel can no longer lie.
    assert op.state == "source_in_progress"


def test_applying_failed_safe_records_the_reason(session) -> None:
    op = _operation_at(session, "source_in_progress")
    apply_recovery(session, op, _status("failed_safe"), now=NOW)
    session.refresh(op)
    assert op.state == "failed_safe"
    assert op.failure_reason is not None


def test_missing_finance_execution_resolves_pre_submit_to_failed_safe(session) -> None:
    op = _operation_at(session, "dispatching")
    plan = apply_recovery(session, op, None, now=NOW)
    assert plan.action is RecoveryAction.RESOLVE
    session.refresh(op)
    assert op.state == "failed_safe"
    assert op.failure_reason is not None


def test_leave_touches_no_state(session) -> None:
    op = _operation_at(session, "source_in_progress")
    plan = apply_recovery(session, op, _status("submitting"), now=NOW)
    assert plan.action is RecoveryAction.LEAVE
    session.refresh(op)
    assert op.state == "source_in_progress"


def test_the_scan_reconciles_every_recoverable_operation(session) -> None:
    a = _operation_at(session, "dispatching", key="req-a")
    b = _operation_at(session, "verifying", key="req-b")
    # A terminal operation must not be touched by the scan.
    c = _operation_at(session, "dispatching", key="req-c")
    apply_recovery(session, c, _status("failed_safe"), now=NOW)
    session.refresh(c)
    assert c.state == "failed_safe"

    statuses = {
        a.idempotency_key: _status("succeeded", record_id="recA", verified=True),
        b.idempotency_key: _status("failed_safe"),
    }
    results = recover_pending(session, lambda key: statuses.get(key), now=NOW)

    resolved = dict(results)
    assert resolved[a.operation_id].target_state == "succeeded"
    assert resolved[b.operation_id].target_state == "failed_safe"
    # c was already terminal, so the scan skipped it.
    assert c.operation_id not in resolved
    session.refresh(a)
    session.refresh(b)
    assert a.state == "succeeded"
    assert a.safe_result == "recA"
    assert b.state == "failed_safe"
