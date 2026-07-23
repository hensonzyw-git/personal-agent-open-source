"""DEV-006: the transition table refuses every unsafe move by construction."""

from __future__ import annotations

import pytest

from personal_data_mcp.storage.models import (
    EXECUTION_STATES,
    POST_SUBMIT_STATES,
    TERMINAL_EXECUTION_STATES,
)
from personal_data_mcp.storage.state_machine import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    assert_transition,
    can_transition,
    is_terminal,
    may_have_reached_source,
    recovery_target,
)


def test_the_table_covers_every_declared_state() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(EXECUTION_STATES)
    for targets in ALLOWED_TRANSITIONS.values():
        assert targets.issubset(set(EXECUTION_STATES))


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_EXECUTION_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
        assert is_terminal(state)
        with pytest.raises(IllegalTransitionError):
            assert_transition(state, "succeeded")


def test_nothing_can_go_back_to_prepared() -> None:
    # Returning to `prepared` would licence minting a second client token for a
    # request that may already exist in the ledger.
    for state, targets in ALLOWED_TRANSITIONS.items():
        if state != "prepared":
            assert "prepared" not in targets, f"{state} -> prepared is unsafe"


def test_submitting_cannot_be_undone_only_resolved() -> None:
    assert ALLOWED_TRANSITIONS["submitting"] == {
        "committed_unverified",
        "commit_unknown",
        "failed_safe",
    }
    assert not can_transition("submitting", "cancelled_pre_submit")
    assert not can_transition("submitting", "succeeded")


def test_cancellation_is_reachable_only_before_submission() -> None:
    # Otherwise a user who gives up would be told a possibly-committed write was
    # cancelled.
    reachable_from = {
        state
        for state, targets in ALLOWED_TRANSITIONS.items()
        if "cancelled_pre_submit" in targets
    }
    assert reachable_from == {"prepared"}


def test_success_requires_passing_through_verification() -> None:
    reachable_from = {
        state
        for state, targets in ALLOWED_TRANSITIONS.items()
        if "succeeded" in targets
    }
    assert reachable_from == {"committed_unverified"}


def test_an_unknown_commit_must_reconcile_or_escalate() -> None:
    assert ALLOWED_TRANSITIONS["commit_unknown"] == {
        "reconciling_same_client_token",
        "needs_manual_review",
    }
    assert not can_transition("commit_unknown", "succeeded")
    assert not can_transition("commit_unknown", "failed_safe")


def test_post_submit_states_are_flagged_as_possibly_committed() -> None:
    assert POST_SUBMIT_STATES == {
        "submitting",
        "commit_unknown",
        "reconciling_same_client_token",
        "committed_unverified",
    }
    for state in POST_SUBMIT_STATES:
        assert may_have_reached_source(state)
    for state in ("prepared", "failed_safe", "cancelled_pre_submit"):
        assert not may_have_reached_source(state)


def test_recovery_promotes_an_interrupted_submit_to_unknown() -> None:
    # A process that died during `submitting` cannot prove the request never
    # left, so recovery treats it as possibly committed.
    assert recovery_target("submitting") == "commit_unknown"
    assert recovery_target("prepared") == "prepared"
    assert recovery_target("commit_unknown") == "commit_unknown"
    assert recovery_target("committed_unverified") == "committed_unverified"
    for state in TERMINAL_EXECUTION_STATES:
        assert recovery_target(state) is None


def test_illegal_transitions_explain_the_rule(
    ) -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        assert_transition("submitting", "prepared")
    assert "same client token" in str(excinfo.value)

    with pytest.raises(IllegalTransitionError):
        assert_transition("prepared", "not_a_state")
    with pytest.raises(IllegalTransitionError):
        assert_transition("not_a_state", "prepared")
