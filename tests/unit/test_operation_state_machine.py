"""DEV-026 A: the operation transition table refuses every unsafe move.

The one safety property this machine exists for: a client cancelling or
disconnecting must never be reported as the *write* being cancelled. Cancellation
is therefore reachable only from states where no source submit could have
happened yet.
"""

from __future__ import annotations

import pytest

from personal_agent.api.operation_state import (
    ALLOWED_TRANSITIONS,
    POST_SUBMIT_STATES,
    PRE_SUBMIT_STATES,
    IllegalOperationTransitionError,
    assert_transition,
    can_cancel_pre_submit,
    can_transition,
    is_recoverable,
    is_terminal,
    may_have_reached_source,
    recovery_target,
)
from personal_agent.storage.models import (
    OPERATION_STATES,
    RECOVERABLE_OPERATION_STATES,
    TERMINAL_OPERATION_STATES,
)


def test_the_table_covers_every_declared_state() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(OPERATION_STATES)
    for targets in ALLOWED_TRANSITIONS.values():
        assert targets.issubset(set(OPERATION_STATES))


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_OPERATION_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
        assert is_terminal(state)
        with pytest.raises(IllegalOperationTransitionError):
            assert_transition(state, "succeeded")


def test_cancellation_is_reachable_only_before_a_possible_submit() -> None:
    # A client giving up after the write may be in flight must not be reported as
    # a cancelled write.
    reachable_from = {
        state
        for state, targets in ALLOWED_TRANSITIONS.items()
        if "cancelled_pre_submit" in targets
    }
    assert reachable_from == set(PRE_SUBMIT_STATES)
    for state in PRE_SUBMIT_STATES:
        assert can_cancel_pre_submit(state)
    for state in POST_SUBMIT_STATES:
        assert not can_cancel_pre_submit(state)


def test_post_submit_states_can_never_cancel() -> None:
    for state in POST_SUBMIT_STATES:
        assert not can_transition(state, "cancelled_pre_submit")
        assert may_have_reached_source(state)
    # needs_manual_review is only reachable once a submit could have happened.
    assert may_have_reached_source("needs_manual_review")
    for state in ("accepted", "interpreting", "dispatching"):
        assert not may_have_reached_source(state)


def test_pre_and_post_submit_partition_covers_the_flow() -> None:
    # Every non-terminal working state is classified as either pre- or
    # post-submit, and the two sets never overlap.
    assert PRE_SUBMIT_STATES.isdisjoint(POST_SUBMIT_STATES)
    working = PRE_SUBMIT_STATES | POST_SUBMIT_STATES
    assert working == set(OPERATION_STATES) - TERMINAL_OPERATION_STATES


def test_success_requires_passing_through_the_write_states_or_a_read() -> None:
    reachable_from = {
        state
        for state, targets in ALLOWED_TRANSITIONS.items()
        if "succeeded" in targets
    }
    # A verified write (`verifying`), a completed read (`dispatching`), or a
    # no-side-effect answer (`interpreting`) are the only ways to succeed.
    assert reachable_from == {"verifying", "dispatching", "interpreting"}


def test_a_write_in_progress_cannot_jump_straight_to_success() -> None:
    # source_in_progress must go through verifying; it cannot self-declare done.
    assert not can_transition("source_in_progress", "succeeded")
    assert can_transition("source_in_progress", "verifying")


def test_waiting_states_are_parked_and_only_cancel() -> None:
    # A clarification or duplicate decision is answered by a new operation, so
    # these states never resume into work.
    assert ALLOWED_TRANSITIONS["waiting_for_clarification"] == {
        "cancelled_pre_submit"
    }
    assert ALLOWED_TRANSITIONS["waiting_for_duplicate_decision"] == {
        "cancelled_pre_submit"
    }


def test_only_the_in_flight_states_are_recoverable() -> None:
    recoverable = {
        state for state in OPERATION_STATES if is_recoverable(state)
    }
    assert recoverable == set(RECOVERABLE_OPERATION_STATES)
    # The parked waiting states are deliberately not recovered on restart.
    assert not is_recoverable("waiting_for_clarification")
    assert not is_recoverable("waiting_for_duplicate_decision")


def test_recovery_keeps_a_recoverable_state_for_reconcile() -> None:
    # The Agent never promotes an accounting state locally; it reconciles against
    # Finance MCP and leaves the state until that resolves it.
    for state in RECOVERABLE_OPERATION_STATES:
        assert recovery_target(state) == state
    for state in TERMINAL_OPERATION_STATES:
        assert recovery_target(state) is None
    # Parked and early states are not driven by the recovery scan.
    assert recovery_target("accepted") is None
    assert recovery_target("waiting_for_clarification") is None


def test_illegal_transitions_explain_the_rule() -> None:
    with pytest.raises(IllegalOperationTransitionError) as excinfo:
        assert_transition("source_in_progress", "cancelled_pre_submit")
    assert "cancelled" in str(excinfo.value)

    with pytest.raises(IllegalOperationTransitionError):
        assert_transition("accepted", "not_a_state")
    with pytest.raises(IllegalOperationTransitionError):
        assert_transition("not_a_state", "accepted")
