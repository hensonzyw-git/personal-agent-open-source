"""The execution state machine, per technical design 7.6.

Everything here exists to answer one question after a failure: could this
request already have reached Feishu? If the answer is "maybe", the only safe
moves are to query or replay under the *same* client token. Minting a new key is
how duplicate ledger entries happen, so the transition table simply has no edge
that allows it.

Three rules are encoded as structure rather than left to the caller:

- `submitting` is committed before the HTTP request leaves, and there is no edge
  back to `prepared`. A crash mid-submit therefore resolves to `commit_unknown`,
  never to "never sent";
- `cancelled_pre_submit` is reachable only from `prepared`. Once a request may
  have been submitted, a user cancelling can no longer produce a cancelled
  outcome, because that would report a possibly-committed write as rolled back;
- terminal states have no outgoing edges at all.
"""

from __future__ import annotations

from typing import Final

from personal_data_mcp.storage.models import (
    EXECUTION_STATES,
    POST_SUBMIT_STATES,
    TERMINAL_EXECUTION_STATES,
)


class IllegalTransitionError(RuntimeError):
    """A transition the safety design does not permit."""


class StaleStateVersionError(RuntimeError):
    """Another worker advanced this execution first."""


#: The complete transition table. Anything absent is illegal by construction.
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "prepared": frozenset(
        {"submitting", "failed_safe", "cancelled_pre_submit"}
    ),
    # A definitive 4xx that proves no side effect is the only way out of
    # `submitting` without going through verification or reconciliation.
    "submitting": frozenset(
        {"committed_unverified", "commit_unknown", "failed_safe"}
    ),
    "commit_unknown": frozenset(
        {"reconciling_same_client_token", "needs_manual_review"}
    ),
    "reconciling_same_client_token": frozenset(
        {"committed_unverified", "failed_safe", "needs_manual_review"}
    ),
    "committed_unverified": frozenset({"succeeded", "needs_manual_review"}),
    "succeeded": frozenset(),
    "failed_safe": frozenset(),
    "needs_manual_review": frozenset(),
    "cancelled_pre_submit": frozenset(),
}


def is_terminal(state: str) -> bool:
    return state in TERMINAL_EXECUTION_STATES


def may_have_reached_source(state: str) -> bool:
    """Whether the request might already exist in the fact source.

    Callers use this to decide between "safe to stop" and "must reconcile".
    """
    return state in POST_SUBMIT_STATES or state in {
        "succeeded",
        "needs_manual_review",
    }


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str) -> None:
    """Raise unless the move is permitted, with a reason worth reading."""
    if current not in ALLOWED_TRANSITIONS:
        raise IllegalTransitionError(f"unknown execution state {current!r}")
    if target not in EXECUTION_STATES:
        raise IllegalTransitionError(f"unknown execution state {target!r}")
    if is_terminal(current):
        raise IllegalTransitionError(
            f"{current} is terminal; it has no outgoing transitions"
        )
    if not can_transition(current, target):
        raise IllegalTransitionError(
            f"{current} -> {target} is not permitted. "
            "A request that may have reached the fact source can only be "
            "queried or replayed under the same client token."
        )


def recovery_target(state: str) -> str | None:
    """Where a non-terminal execution resumes after a process restart.

    Technical design 7.6.2. `prepared` may still submit for the first time under
    its saved token. `submitting` is promoted to `commit_unknown`, because the
    request may have been in flight when the process died. The rest continue the
    bounded recovery they were already in, so they need no state change.
    """
    if is_terminal(state):
        return None
    if state == "submitting":
        return "commit_unknown"
    return state
