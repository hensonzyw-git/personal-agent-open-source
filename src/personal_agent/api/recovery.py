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
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from personal_agent.api.operation_store import transition_operation
from personal_agent.mcp_client.core import TRANSPORT_READ_TIMEOUT
from personal_agent.storage.models import (
    RECOVERABLE_OPERATION_STATES,
    Operation,
)


#: How long an operation must have sat at the same state before recovery may
#: adopt it.
#:
#: Derived, not chosen. `updated_at` moves on every transition, so the question
#: is: what is the longest a *live* worker can legitimately go without one? Every
#: gap on a recoverable operation is bounded by a call budget -- the model turn
#: (25s) happens at `dispatching`, and the longest of all, the governed MCP write,
#: is bounded by `TRANSPORT_READ_TIMEOUT`. A worker cannot exceed its own ceiling
#: without failing, so anything quiet for longer than that ceiling really has
#: been abandoned. The margin on top is so the two are never merely equal.
#:
#: Importing the ceiling rather than restating it keeps the derivation true if
#: the timeout ever changes; a test pins the inequality so it cannot silently
#: invert.
RECOVERY_QUIET_PERIOD: Final[timedelta] = TRANSPORT_READ_TIMEOUT + timedelta(
    seconds=30
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

#: The Finance execution states that prove the one create never left the
#: process. `prepared` commits *before* any network call and `cancelled_pre_submit`
#: is reachable only from `prepared`, so both are provably zero-write;
#: `failed_safe` is Finance's own terminal claim that nothing reached the source.
#: Everything else -- including a state this Agent does not recognise -- may have
#: written.
#:
#: This mirrors `may_have_reached_source` in Finance's state machine, inverted.
#: The two services cannot share the module, so the duplication is deliberate and
#: kept here, in the one Agent module that already owns Finance's state
#: vocabulary, rather than spread across callers.
_FINANCE_ZERO_WRITE_STATES: Final[frozenset[str]] = frozenset(
    {"prepared", "failed_safe", "cancelled_pre_submit"}
)


def proves_zero_write(status: FinanceExecutionStatus | None) -> bool:
    """Whether Finance's own record proves this key wrote nothing externally.

    `None` means Finance has no execution for the key at all. Because the
    `prepared` row is committed before any network call, no execution row means
    no create was ever attempted -- which is the strongest evidence available,
    and the only thing that may be reported as a proven zero write.

    Fails closed: an unrecognised state is not a proof.
    """
    if status is None:
        return True
    return status.state in _FINANCE_ZERO_WRITE_STATES

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
    agent_state: str,
    finance_status: FinanceExecutionStatus | None,
    *,
    quiet: bool,
) -> RecoveryPlan:
    """Decide how to project a Finance status onto a recoverable operation.

    Pure: no database, no clock. The applier turns a plan into legal transitions.

    `quiet` says the operation has not changed state for longer than any live
    worker can legitimately hold it (see `RECOVERY_QUIET_PERIOD`). It is a
    required parameter rather than a filter applied by the caller, because the
    branch it protects is the one that read an *absence* as evidence: on
    2026-08-03 this function resolved a live operation to `needs_manual_review`
    with "finance has no execution" **3.2 seconds before Finance committed that
    execution**, and the write then succeeded in full. §5.2 and DEV-038's slice B
    already state the rule -- an absent row is a race, not proof -- and it had
    been applied to the dispatcher and never here. Making it a parameter means a
    future caller cannot reach the branch without answering the question.
    """
    if agent_state not in RECOVERABLE_OPERATION_STATES:
        raise ValueError(f"{agent_state} is not a recoverable operation state")
    agent_pre_submit = agent_state == _PRE_SUBMIT_RECOVERABLE

    if not quiet:
        # Someone else is holding this one. Recovery exists for what a crash left
        # behind, and a still-running turn is indistinguishable from that by
        # state alone -- so the state is not what decides it.
        return RecoveryPlan(
            RecoveryAction.LEAVE,
            reason="a live worker may still own this operation",
        )

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
        return _manual_review(
            "finance needs manual review",
            safe_result=(
                finance_status.record_id
                if _nonempty(finance_status.record_id)
                else None
            ),
        )
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


def is_quiet(updated_at: datetime, *, now: datetime) -> bool:
    """Whether an operation has sat still long enough for recovery to adopt it.

    A time-based proxy for ownership, not a lease. It is sound here for one
    specific reason: every gap between two state changes on a recoverable
    operation is bounded by a call budget shorter than `RECOVERY_QUIET_PERIOD`,
    so a worker cannot stay silent past it and still be running. If a future
    change introduces an unbounded wait inside a recoverable state, this stops
    being true and the proxy has to become a real lease -- the shape Finance's
    own reconciler already uses with `recovery_lease_owner`.
    """
    return now - updated_at >= RECOVERY_QUIET_PERIOD


def apply_recovery(
    session,
    operation: Operation,
    finance_status: FinanceExecutionStatus | None,
    *,
    now: datetime,
    quiet: bool,
) -> RecoveryPlan:
    """Project a Finance status onto one operation, walking legal transitions.

    Returns the plan that was applied. `LEAVE` changes no state.
    """
    plan = plan_recovery(operation.state, finance_status, quiet=quiet)

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
    """Reconcile every recoverable operation against Finance in one scan.

    Returns `(operation_id, plan)` for each so the composition root can log the
    projection without having to re-read the affected rows.

    An operation that is still quiet-period-young is left alone *and* is not
    even read from the control plane: it belongs to a live worker, so asking
    Finance about it can only produce an answer that is already out of date, and
    the write that would follow is the one that collided with the live worker's
    own transition and surfaced as `database is locked`.
    """
    pending = list(
        session.query(Operation)
        .filter(Operation.state.in_(sorted(RECOVERABLE_OPERATION_STATES)))
        .all()
    )
    # Snapshotted before the commit below, because reading it afterwards would
    # re-fetch each row and the quietness of an operation must be judged at one
    # instant, not at whatever moment each attribute happened to be refreshed.
    quiet_by_id = {
        operation.operation_id: is_quiet(operation.updated_at, now=now)
        for operation in pending
    }
    # Every control-plane read happens first, with no transaction open. Holding
    # one across these calls would mean the first write afterwards fails if
    # anything else committed in between, and reading them all before touching
    # any row is also what keeps this projection all-or-nothing: an unreadable
    # control plane aborts before a single operation has moved.
    adoptable = [
        operation
        for operation in pending
        if quiet_by_id[operation.operation_id]
    ]
    keys = [operation.idempotency_key for operation in adoptable]
    session.commit()
    statuses = [read_status(key) for key in keys]
    status_by_id = {
        operation.operation_id: status
        for operation, status in zip(adoptable, statuses, strict=True)
    }

    results: list[tuple[str, RecoveryPlan]] = []
    for operation in pending:
        quiet = quiet_by_id[operation.operation_id]
        plan = apply_recovery(
            session,
            operation,
            status_by_id.get(operation.operation_id),
            now=now,
            quiet=quiet,
        )
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


def _manual_review(
    reason: str, *, safe_result: str | None = None
) -> RecoveryPlan:
    return RecoveryPlan(
        RecoveryAction.RESOLVE,
        target_state="needs_manual_review",
        safe_result=safe_result,
        reason=reason,
    )


def _nonempty(value: str | None) -> bool:
    return bool(value and value.strip())
