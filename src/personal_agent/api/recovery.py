"""Cross-database recovery projection, per technical design 5.2.2 and 7.6.

After a restart, an operation left in a recoverable state (`dispatching`,
`source_in_progress`, `verifying`) has an authoritative twin somewhere: the
Finance execution keyed by the same idempotency key. The Agent never guesses the
accounting outcome by comparing timestamps -- it asks Finance MCP's read-only
control endpoint for that execution's state and *projects* it onto its own
operation, walking only legal transitions so the projection is as auditable as a
live write.

The one property this protects is the same one the state machine protects: a
client detaching or a process dying must never be read as a rollback. So:

- Finance is authoritative. A Finance terminal state resolves the operation; a
  verified success carries the record id into `safe_result`.
- If the Agent thinks it is still pre-submit (`dispatching`) but Finance shows a
  possible submit, the operation is advanced to `source_in_progress` first, so a
  pending cancel can no longer produce a false `cancelled_pre_submit`.
- Any genuine contradiction -- Finance has no execution for a post-submit
  operation, or a success without a verified receipt -- is escalated to
  `needs_manual_review` with the row kept, never silently resolved.

The control-plane read itself is injected (`FinanceStatusReader`), so this module
is exercised offline against fakes; the HTTP client that calls the real control
endpoint is wired in at composition time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from personal_agent.api.operation_store import transition_operation
from personal_agent.storage.models import (
    RECOVERABLE_OPERATION_STATES,
    Operation,
)


@dataclass(frozen=True)
class FinanceExecutionStatus:
    """The subset of a Finance execution the Agent projects from.

    Mirrors the control endpoint's `get_execution_status` body: the verbatim
    execution state, the external record id if one exists, and whether its
    receipt was verified. No encrypted payload or result is carried.
    """

    state: str
    record_id: str | None
    receipt_verified: bool


#: Reads one Finance execution by idempotency key, or None if Finance never saw
#: it. In production this calls the read-only control endpoint; tests inject a
#: fake.
FinanceStatusReader = Callable[[str], FinanceExecutionStatus | None]


class RecoveryAction(StrEnum):
    RESOLVE = "resolve"  # walk to a projected terminal state
    ADVANCE_IN_PROGRESS = "advance_in_progress"  # move dispatching -> source_in_progress
    LEAVE = "leave"  # still consistently in flight; re-poll later


@dataclass(frozen=True)
class RecoveryPlan:
    action: RecoveryAction
    target_state: str | None = None
    safe_result: str | None = None
    reason: str | None = None


#: The only pre-submit recoverable state. A `dispatching` operation may not have
#: reached Finance at all; `source_in_progress` and `verifying` always have.
_PRE_SUBMIT_RECOVERABLE: Final[str] = "dispatching"

_FINANCE_TERMINALS: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed_safe", "needs_manual_review", "cancelled_pre_submit"}
)

_FINANCE_IN_PROGRESS: Final[frozenset[str]] = frozenset(
    {
        "submitting",
        "commit_unknown",
        "reconciling_same_client_token",
        "committed_unverified",
    }
)

#: Explicit legal walks from a recoverable state to a projected terminal. Paths
#: are spelled out rather than shortest-path searched so recovery never takes the
#: read-only `dispatching -> succeeded` shortcut for a write, and every hop stays
#: auditable.
_RECOVERY_PATHS: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    ("dispatching", "succeeded"): ("source_in_progress", "verifying", "succeeded"),
    ("dispatching", "failed_safe"): ("failed_safe",),
    ("dispatching", "needs_manual_review"): (
        "source_in_progress",
        "needs_manual_review",
    ),
    ("dispatching", "cancelled_pre_submit"): ("cancelled_pre_submit",),
    ("source_in_progress", "succeeded"): ("verifying", "succeeded"),
    ("source_in_progress", "failed_safe"): ("failed_safe",),
    ("source_in_progress", "needs_manual_review"): ("needs_manual_review",),
    ("verifying", "succeeded"): ("succeeded",),
    ("verifying", "failed_safe"): ("failed_safe",),
    ("verifying", "needs_manual_review"): ("needs_manual_review",),
}


def plan_recovery(
    agent_state: str, finance_status: FinanceExecutionStatus | None
) -> RecoveryPlan:
    """Decide how to project a Finance status onto a recoverable operation.

    Pure: no database, no clock. The applier turns a plan into legal transitions.
    """
    if agent_state not in RECOVERABLE_OPERATION_STATES:
        raise ValueError(f"{agent_state} is not a recoverable operation state")
    agent_pre_submit = agent_state == _PRE_SUBMIT_RECOVERABLE

    if finance_status is None:
        if agent_pre_submit:
            # The current Agent schema retains the sealed user request, but not
            # the complete authorised tool intent at `dispatching`. Re-running
            # the model could silently produce different arguments, so design
            # 7.6.1 requires a safe failure and a fresh user request instead.
            return RecoveryPlan(
                RecoveryAction.RESOLVE,
                target_state="failed_safe",
                reason=(
                    "finance has no execution and no complete replayable tool "
                    "intent was persisted"
                ),
            )
        return _manual_review(
            "the operation is past submit but finance has no execution"
        )

    fs = finance_status.state

    if fs == "succeeded":
        if finance_status.receipt_verified and _nonempty(finance_status.record_id):
            return RecoveryPlan(
                RecoveryAction.RESOLVE,
                target_state="succeeded",
                safe_result=finance_status.record_id,
            )
        return _manual_review("finance succeeded without a verified receipt id")
    if fs == "failed_safe":
        return RecoveryPlan(
            RecoveryAction.RESOLVE,
            target_state="failed_safe",
            reason="finance failed safe",
        )
    if fs == "needs_manual_review":
        return _manual_review("finance needs manual review")
    if fs == "cancelled_pre_submit":
        if agent_pre_submit:
            return RecoveryPlan(
                RecoveryAction.RESOLVE, target_state="cancelled_pre_submit"
            )
        return _manual_review(
            "finance cancelled pre-submit but the operation is past submit"
        )

    # --- finance is still non-terminal --------------------------------------
    if fs == "prepared":
        # Finance prepared but has not submitted. Consistent only if the Agent is
        # also still pre-submit; otherwise the two disagree about submission.
        if agent_pre_submit:
            return RecoveryPlan(
                RecoveryAction.LEAVE, reason="finance is still preparing"
            )
        return _manual_review(
            "the operation is past submit but finance is still prepared"
        )
    if fs not in _FINANCE_IN_PROGRESS:
        return _manual_review(f"finance returned unknown execution state {fs!r}")

    # A known in-progress state means the write may exist.
    if agent_pre_submit:
        return RecoveryPlan(
            RecoveryAction.ADVANCE_IN_PROGRESS,
            reason="finance may have submitted; advance so a cancel cannot lie",
        )
    return RecoveryPlan(
        RecoveryAction.LEAVE, reason="finance is still resolving the write"
    )


def apply_recovery(
    session,
    operation: Operation,
    finance_status: FinanceExecutionStatus | None,
    *,
    now: datetime,
) -> RecoveryPlan:
    """Project a Finance status onto one operation, walking legal transitions.

    Returns the plan that was applied. `LEAVE` changes no state.
    """
    plan = plan_recovery(operation.state, finance_status)

    if plan.action is RecoveryAction.ADVANCE_IN_PROGRESS:
        _walk(session, operation, ("source_in_progress",), now, plan)
    elif plan.action is RecoveryAction.RESOLVE:
        assert plan.target_state is not None
        path = _RECOVERY_PATHS[(operation.state, plan.target_state)]
        _walk(session, operation, path, now, plan)
    # LEAVE intentionally touches no state.
    return plan


def recover_pending(
    session, read_status: FinanceStatusReader, *, now: datetime
) -> list[tuple[str, RecoveryPlan]]:
    """Reconcile every recoverable operation against Finance, on startup.

    Returns `(operation_id, plan)` for each so the composition root can log the
    projection without having to re-read the affected rows.
    """
    pending = list(
        session.query(Operation)
        .filter(Operation.state.in_(sorted(RECOVERABLE_OPERATION_STATES)))
        .all()
    )
    results: list[tuple[str, RecoveryPlan]] = []
    for operation in pending:
        status = read_status(operation.idempotency_key)
        plan = apply_recovery(session, operation, status, now=now)
        results.append((operation.operation_id, plan))
    return results


def _walk(
    session,
    operation: Operation,
    path: tuple[str, ...],
    now: datetime,
    plan: RecoveryPlan,
) -> None:
    """Apply each hop of a legal path, attaching result/reason on the last hop."""
    for index, target in enumerate(path):
        last = index == len(path) - 1
        session.refresh(operation)
        transition_operation(
            session,
            operation_id=operation.operation_id,
            current_state=operation.state,
            current_version=operation.state_version,
            target_state=target,
            now=now,
            failure_reason=plan.reason if last else None,
            safe_result=plan.safe_result if last else None,
        )
    session.refresh(operation)


def _manual_review(reason: str) -> RecoveryPlan:
    return RecoveryPlan(
        RecoveryAction.RESOLVE, target_state="needs_manual_review", reason=reason
    )


def _nonempty(value: str | None) -> bool:
    return bool(value and value.strip())
