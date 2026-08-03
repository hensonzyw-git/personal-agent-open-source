"""DEV-026 C: cross-database recovery projects Finance truth, never a guess.

The planner is pure and tested exhaustively; the applier and the startup scan run
against a real Agent SQLite database with an injected fake Finance reader. No
network and no Finance database are involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_agent.api.operation_store import open_operation, transition_operation
from personal_agent.api.recovery import (
    RECOVERY_QUIET_PERIOD,
    FinanceExecutionStatus,
    RecoveryAction,
    apply_recovery,
    is_quiet,
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
#: Far enough past NOW that an operation last touched at NOW counts as
#: abandoned rather than in flight.
LONG_AFTER = NOW + RECOVERY_QUIET_PERIOD


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
        plan_recovery("accepted", None, quiet=True)


def test_finance_success_with_a_verified_receipt_resolves_with_the_record() -> None:
    plan = plan_recovery(
        "verifying", _status("succeeded", record_id="rec123", verified=True),
        quiet=True,
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
    plan = plan_recovery("verifying", status, quiet=True)
    assert plan.target_state == "needs_manual_review"


def test_finance_failed_safe_and_manual_review_project_through() -> None:
    assert plan_recovery("dispatching", _status("failed_safe"), quiet=True).target_state == (
        "failed_safe"
    )
    assert plan_recovery(
        "source_in_progress", _status("needs_manual_review"),
        quiet=True,
    ).target_state == "needs_manual_review"


def test_finance_manual_review_preserves_a_known_record_id() -> None:
    plan = plan_recovery(
        "source_in_progress",
        _status("needs_manual_review", record_id="rec123"),
        quiet=True,
    )
    assert plan.target_state == "needs_manual_review"
    assert plan.safe_result == "rec123"


def test_finance_cancel_only_projects_when_agent_is_pre_submit() -> None:
    assert plan_recovery(
        "dispatching", _status("cancelled_pre_submit"),
        quiet=True,
    ).target_state == "cancelled_pre_submit"
    # A post-submit operation cannot honestly be cancelled; the disagreement is
    # escalated instead.
    assert plan_recovery(
        "source_in_progress", _status("cancelled_pre_submit"),
        quiet=True,
    ).target_state == "needs_manual_review"


def test_no_finance_execution_fails_safe_without_a_replayable_intent() -> None:
    pre_submit = plan_recovery("dispatching", None, quiet=True)
    assert pre_submit.action is RecoveryAction.RESOLVE
    assert pre_submit.target_state == "failed_safe"
    past = plan_recovery("source_in_progress", None, quiet=True)
    assert past.action is RecoveryAction.RESOLVE
    assert past.target_state == "needs_manual_review"


def test_finance_prepared_leaves_a_pre_submit_operation_but_flags_a_conflict() -> None:
    assert plan_recovery("dispatching", _status("prepared"), quiet=True).action is (
        RecoveryAction.LEAVE
    )
    assert plan_recovery(
        "verifying", _status("prepared"),
        quiet=True,
    ).target_state == "needs_manual_review"


def test_an_unknown_finance_state_fails_closed_to_manual_review() -> None:
    pre_submit = plan_recovery("dispatching", _status("corrupted"), quiet=True)
    post_submit = plan_recovery("source_in_progress", _status("corrupted"), quiet=True)
    assert pre_submit.target_state == "needs_manual_review"
    assert post_submit.target_state == "needs_manual_review"


@pytest.mark.parametrize(
    "finance_state",
    ["submitting", "commit_unknown", "reconciling_same_client_token",
     "committed_unverified"],
)
def test_a_possible_submit_advances_a_dispatching_operation(finance_state) -> None:
    plan = plan_recovery("dispatching", _status(finance_state), quiet=True)
    assert plan.action is RecoveryAction.ADVANCE_IN_PROGRESS


@pytest.mark.parametrize(
    "finance_state",
    ["submitting", "commit_unknown", "committed_unverified"],
)
def test_a_possible_submit_leaves_an_already_in_progress_operation(finance_state):
    plan = plan_recovery("source_in_progress", _status(finance_state), quiet=True)
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
        session, op, _status("succeeded", record_id="rec9", verified=True), now=NOW,
        quiet=True,
    )
    assert plan.action is RecoveryAction.RESOLVE
    session.refresh(op)
    assert op.state == "succeeded"
    assert op.safe_result == "rec9"


def test_applying_a_possible_submit_advances_but_does_not_resolve(session) -> None:
    op = _operation_at(session, "dispatching")
    apply_recovery(session, op, _status("submitting"), now=NOW, quiet=True)
    session.refresh(op)
    # It is no longer pre-submit, so a pending cancel can no longer lie.
    assert op.state == "source_in_progress"


def test_applying_failed_safe_records_the_reason(session) -> None:
    op = _operation_at(session, "source_in_progress")
    apply_recovery(session, op, _status("failed_safe"), now=NOW, quiet=True)
    session.refresh(op)
    assert op.state == "failed_safe"
    assert op.failure_reason is not None


def test_missing_finance_execution_resolves_pre_submit_to_failed_safe(session) -> None:
    op = _operation_at(session, "dispatching")
    plan = apply_recovery(session, op, None, now=NOW, quiet=True)
    assert plan.action is RecoveryAction.RESOLVE
    session.refresh(op)
    assert op.state == "failed_safe"
    assert op.failure_reason is not None


def test_leave_touches_no_state(session) -> None:
    op = _operation_at(session, "source_in_progress")
    plan = apply_recovery(session, op, _status("submitting"), now=NOW, quiet=True)
    assert plan.action is RecoveryAction.LEAVE
    session.refresh(op)
    assert op.state == "source_in_progress"


def test_the_scan_reconciles_every_recoverable_operation(session) -> None:
    a = _operation_at(session, "dispatching", key="req-a")
    b = _operation_at(session, "verifying", key="req-b")
    # A terminal operation must not be touched by the scan.
    c = _operation_at(session, "dispatching", key="req-c")
    apply_recovery(session, c, _status("failed_safe"), now=NOW, quiet=True)
    session.refresh(c)
    assert c.state == "failed_safe"

    statuses = {
        a.idempotency_key: _status("succeeded", record_id="recA", verified=True),
        b.idempotency_key: _status("failed_safe"),
    }
    # Aged past the quiet period: these are abandoned, which is what the scan
    # exists for.
    results = recover_pending(
        session, lambda key: statuses.get(key), now=LONG_AFTER
    )

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


# --- the 2026-08-03 P0: recovery adopted an operation a live worker owned -----
#
# Live evidence: docs/evidence/DEV038_线上半_2026-08-03.md. Recovery resolved a
# three-second-old operation to `needs_manual_review` with "finance has no
# execution" 3.2 seconds BEFORE Finance committed that execution; the write then
# succeeded in full, and the client got an HTTP 500 with no operation id.


def test_the_quiet_period_exceeds_the_longest_call_a_worker_can_make() -> None:
    """The derivation, pinned so it cannot silently invert.

    `RECOVERY_QUIET_PERIOD` is only a sound ownership proxy while it is longer
    than the longest budget a live worker can spend without changing state. If
    someone raises the transport ceiling past it, this fails rather than quietly
    reopening the race.
    """
    from personal_agent.mcp_client.core import TRANSPORT_READ_TIMEOUT

    assert RECOVERY_QUIET_PERIOD > TRANSPORT_READ_TIMEOUT


def test_an_operation_that_just_moved_is_not_quiet() -> None:
    assert is_quiet(NOW, now=NOW) is False
    assert is_quiet(NOW, now=NOW + RECOVERY_QUIET_PERIOD / 2) is False
    assert is_quiet(NOW, now=LONG_AFTER) is True


@pytest.mark.parametrize(
    "agent_state",
    ["dispatching", "source_in_progress", "verifying"],
)
@pytest.mark.parametrize(
    "status",
    [
        None,
        _status("succeeded", record_id="rec9", verified=True),
        _status("failed_safe"),
        _status("submitting"),
        _status("prepared"),
        _status("corrupted"),
    ],
)
def test_a_live_operation_is_left_alone_whatever_finance_says(
    agent_state, status
) -> None:
    """Not just the absent-row case: recovery must not touch a live operation at
    all. Even a correct-looking projection races the worker's own transition,
    and that collision is what surfaced as `database is locked`.
    """
    plan = plan_recovery(agent_state, status, quiet=False)
    assert plan.action is RecoveryAction.LEAVE


def test_a_missing_execution_on_a_live_operation_is_a_race_not_evidence() -> None:
    """The exact P0, at the planner.

    Past submit, Finance has no row yet, the operation is seconds old. Before
    the fix this returned `needs_manual_review` / "finance has no execution".
    """
    racing = plan_recovery("source_in_progress", None, quiet=False)
    assert racing.action is RecoveryAction.LEAVE

    # And the same inputs, once the operation really has been abandoned, still
    # escalate -- the fix must not have removed the escalation, only delayed it
    # until absence means something.
    abandoned = plan_recovery("source_in_progress", None, quiet=True)
    assert abandoned.target_state == "needs_manual_review"


def test_the_scan_leaves_a_live_operation_and_never_asks_finance_about_it(
    session,
) -> None:
    """Asserted as an absence, which is the part that matters.

    Reading the control plane for a live operation cannot produce a usable
    answer -- it is a snapshot of a race -- and the write that followed it is
    what collided with the worker.
    """
    live = _operation_at(session, "source_in_progress", key="req-live")
    asked: list[str] = []

    def read(key: str):
        asked.append(key)
        return None

    results = recover_pending(session, read, now=NOW)

    assert asked == [], "a live operation must not be read from the control plane"
    assert dict(results)[live.operation_id].action is RecoveryAction.LEAVE
    session.refresh(live)
    assert live.state == "source_in_progress"
    assert live.failure_reason is None


def test_one_scan_adopts_the_abandoned_operation_and_leaves_the_live_one(
    session,
) -> None:
    """A mixed scan, because that is the real shape: one stranded row from a
    crash beside one turn that is still running."""
    abandoned = _operation_at(session, "source_in_progress", key="req-old")
    live = _operation_at(session, "source_in_progress", key="req-new")
    # Only the live one has just moved.
    transition_operation(
        session,
        operation_id=live.operation_id,
        current_state=live.state,
        current_version=live.state_version,
        target_state="verifying",
        now=LONG_AFTER,
    )
    session.refresh(live)

    asked: list[str] = []

    def read(key: str):
        asked.append(key)
        return _status("succeeded", record_id="recOld", verified=True)

    results = dict(recover_pending(session, read, now=LONG_AFTER))

    assert asked == [abandoned.idempotency_key]
    assert results[abandoned.operation_id].target_state == "succeeded"
    assert results[live.operation_id].action is RecoveryAction.LEAVE
    session.refresh(abandoned)
    session.refresh(live)
    assert abandoned.state == "succeeded"
    assert live.state == "verifying"


def test_a_recovery_scan_concurrent_with_a_live_worker_changes_nothing(
    tmp_path: Path,
) -> None:
    """Two sessions, one real SQLite file, in the incident's order.

    §5.2: a single-session test always sees its own writes, so only a concurrent
    one can show this. The worker holds an operation at `source_in_progress`
    while the recovery scan runs in another session, and then finishes the turn.
    Before the fix the scan resolved the row to `needs_manual_review` and the
    worker's next transition died with `database is locked`; the operation ended
    permanently wrong while the ledger record existed.
    """
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    sessions = session_factory(engine)
    try:
        with sessions() as setup:
            setup.add(
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
            setup.flush()
            operation = _operation_at(setup, "source_in_progress")
            operation_id = operation.operation_id
            version = operation.state_version
            setup.commit()

        # The recovery worker, in its own session, while the turn is still
        # running. `now` is seconds after the last transition, exactly as on
        # 2026-08-03. Finance genuinely has no row yet.
        with sessions() as recovery:
            results = dict(
                recover_pending(
                    recovery,
                    lambda key: None,
                    now=NOW + timedelta(seconds=3),
                )
            )
            recovery.commit()
        assert results[operation_id].action is RecoveryAction.LEAVE

        # The worker then finishes, from the version it was holding all along.
        with sessions() as worker:
            version = transition_operation(
                worker,
                operation_id=operation_id,
                current_state="source_in_progress",
                current_version=version,
                target_state="verifying",
                now=NOW + timedelta(seconds=4),
            )
            transition_operation(
                worker,
                operation_id=operation_id,
                current_state="verifying",
                current_version=version,
                target_state="succeeded",
                now=NOW + timedelta(seconds=5),
                safe_result="rec-real",
            )
            worker.commit()

        with sessions() as check:
            final = check.get(Operation, operation_id)
            assert final.state == "succeeded"
            assert final.safe_result == "rec-real"
            assert final.failure_reason is None
    finally:
        engine.dispose()
