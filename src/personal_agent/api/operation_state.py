"""The Agent API operation state machine, per technical design 5.2.

This is the client-facing twin of the Finance execution state machine, and it
guards the same invariant from the other side: a client giving up must never be
reported as the *write* giving up. The two are deliberately separate states --
`cancel_requested` / `client_detached` are flags, and the accounting outcome is
the `state` -- so "the app disconnected" can never be projected as a rollback.

Three rules are structure here, not caller discipline:

- **`cancelled_pre_submit` is reachable only before a source submit could have
  happened.** Once an operation reaches `source_in_progress`, the Finance write
  may already be in flight, so cancelling or detaching can no longer produce a
  cancelled outcome; the operation can only finish `succeeded`, `failed_safe`, or
  `needs_manual_review` once Finance MCP proves what happened.
- **The waiting states are parked, not in-flight.** A clarification or a
  duplicate decision is answered by a *new* operation (design 5.2.1), so
  `waiting_for_clarification` and `waiting_for_duplicate_decision` do not resume
  into work; their only non-cancel exit is nothing. They are intentionally absent
  from the recovery set so a restart never re-drives them.
- **Terminal states have no outgoing edges at all.**

The authoritative side-effect state always lives in Finance SQLite (design
5.2.2). This machine records only the safe projection of it, and never guesses an
accounting result by comparing timestamps across the two databases.
"""

from __future__ import annotations

from typing import Final

from personal_agent.storage.models import (
    OPERATION_STATES,
    RECOVERABLE_OPERATION_STATES,
    TERMINAL_OPERATION_STATES,
)


class IllegalOperationTransitionError(RuntimeError):
    """A transition the operation-safety design does not permit."""


class StaleOperationVersionError(RuntimeError):
    """Another worker advanced this operation first."""


#: States before any source submit could have started. Cancelling from one of
#: these is safe, because no external write can yet exist.
PRE_SUBMIT_STATES: Final[frozenset[str]] = frozenset(
    {
        "accepted",
        "interpreting",
        "waiting_for_clarification",
        "dispatching",
        "waiting_for_duplicate_decision",
    }
)

#: States from which the Finance write may already have been submitted, so a
#: cancel can no longer roll back to `cancelled_pre_submit`.
POST_SUBMIT_STATES: Final[frozenset[str]] = frozenset(
    {"source_in_progress", "verifying"}
)


#: The complete transition table. Anything absent is illegal by construction.
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "accepted": frozenset(
        {"interpreting", "failed_safe", "cancelled_pre_submit"}
    ),
    # Interpreting concludes in one of four ways: a question, a tool dispatch, a
    # no-side-effect answer that is already done, or a safe failure.
    "interpreting": frozenset(
        {
            "waiting_for_clarification",
            "dispatching",
            "succeeded",
            "failed_safe",
            "cancelled_pre_submit",
        }
    ),
    # A parked question. The answer arrives as a new operation, so the only move
    # here is the user abandoning it while nothing has been submitted.
    "waiting_for_clarification": frozenset({"cancelled_pre_submit"}),
    # Dispatching a tool. A read completes straight to `succeeded`; a write goes
    # on to `source_in_progress`. The server-side resolvers can still ask a
    # question (an ambiguous trip, a non-unique refund) or find a pre-write
    # duplicate; both park the operation, and nothing has been submitted yet, so
    # cancel is still safe here.
    "dispatching": frozenset(
        {
            "waiting_for_clarification",
            "waiting_for_duplicate_decision",
            "source_in_progress",
            "succeeded",
            "failed_safe",
            "cancelled_pre_submit",
        }
    ),
    "waiting_for_duplicate_decision": frozenset({"cancelled_pre_submit"}),
    # From here the write may exist. No path returns to a cancellable state.
    "source_in_progress": frozenset(
        {"verifying", "failed_safe", "needs_manual_review"}
    ),
    "verifying": frozenset({"succeeded", "failed_safe", "needs_manual_review"}),
    "succeeded": frozenset(),
    "failed_safe": frozenset(),
    "needs_manual_review": frozenset(),
    "cancelled_pre_submit": frozenset(),
}


def is_terminal(state: str) -> bool:
    return state in TERMINAL_OPERATION_STATES


def is_recoverable(state: str) -> bool:
    """Whether a restart must reconcile this operation against Finance MCP."""
    return state in RECOVERABLE_OPERATION_STATES


def may_have_reached_source(state: str) -> bool:
    """Whether the Finance write might already have been submitted.

    Callers use this to decide whether a cancel or a detach can still finish as
    `cancelled_pre_submit`, or must instead wait for Finance MCP to prove the
    outcome. `needs_manual_review` is included because it is only reachable once
    a submit could have happened.
    """
    return state in POST_SUBMIT_STATES or state == "needs_manual_review"


def can_cancel_pre_submit(state: str) -> bool:
    """Whether cancelling now can still produce a clean pre-submit cancellation."""
    return "cancelled_pre_submit" in ALLOWED_TRANSITIONS.get(state, frozenset())


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str) -> None:
    """Raise unless the move is permitted, with a reason worth reading."""
    if current not in ALLOWED_TRANSITIONS:
        raise IllegalOperationTransitionError(f"unknown operation state {current!r}")
    if target not in OPERATION_STATES:
        raise IllegalOperationTransitionError(f"unknown operation state {target!r}")
    if is_terminal(current):
        raise IllegalOperationTransitionError(
            f"{current} is terminal; it has no outgoing transitions"
        )
    if not can_transition(current, target):
        raise IllegalOperationTransitionError(
            f"{current} -> {target} is not permitted. "
            "An operation whose write may already have been submitted cannot be "
            "reported as cancelled; it must be resolved from Finance MCP."
        )


def recovery_target(state: str) -> str | None:
    """What a restart should do with a non-terminal operation.

    Design 5.2.2: the Agent never guesses. A recoverable operation
    (`dispatching`, `source_in_progress`, `verifying`) is reconciled against the
    authoritative Finance execution and keeps its state until that reconcile
    resolves it, so this returns the same state rather than promoting it locally.
    Terminal and parked states need no recovery and return ``None``.
    """
    if is_terminal(state):
        return None
    if is_recoverable(state):
        return state
    return None
