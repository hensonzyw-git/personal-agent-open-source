"""The Agent operation orchestrator, per technical design 5.2.

One operation is driven from `accepted` to a terminal or parked state:

    interpret -> policy -> resolve -> (park on duplicate/clarification) -> commit -> verify

Two seams are injected so this whole flow is exercised offline, and so the model
and the fact source can be swapped without touching the safety logic:

- the **interpreter** turns user text into either a direct answer or a tool call;
  the real one is the GLM runtime (`DEV-027`), tests use a fake.
- the **dispatcher** is deliberately two-phase. `resolve` runs the read-only
  Finance resolvers and the pre-write duplicate check and *never writes*; `commit`
  performs the create-and-verify. This split is what lets the orchestrator commit
  `source_in_progress` in the narrow window around the actual write only -- a
  duplicate or a clarification is surfaced while the operation is still
  `dispatching`, where a cancel is honestly pre-submit, and the moment a write may
  occur the operation is already `source_in_progress`, where a cancel can no
  longer be reported as a rollback.

`write anyway` re-enters here: an operation carrying a `duplicate_check_id` and a
sealed intent skips interpretation and re-issues the exact resolved write, passing
that id as the Host-bound `duplicate_override`. The override is never a model
argument.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from personal_agent.api.duplicate_flow import record_possible_duplicate
from personal_agent.api.intent import WriteIntent, open_intent
from personal_agent.api.operation_store import transition_operation
from personal_agent.context.builder import ContextEnvelope
from personal_agent.storage.models import Operation
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode


# --- interpreter results -----------------------------------------------------


@dataclass(frozen=True)
class DirectAnswer:
    """A conversational reply with no tool call and no side effect."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """A model-chosen tool and its model-facing arguments."""

    tool: str
    model_args: dict[str, Any]


@dataclass(frozen=True)
class Clarification:
    """A structured question; it parks instead of pretending to be an answer."""

    question: str


@dataclass(frozen=True)
class FailSafeInterpretation:
    """A model-selected outcome backed by a frozen, non-write safety gate."""

    reason: str


Interpretation = DirectAnswer | ToolCall | Clarification | FailSafeInterpretation


class InterpreterError(Exception):
    """The interpreter could not produce a proposal.

    Part of the seam contract: an implementation raises this for a model or
    transport failure, and the orchestrator turns it into a safe `failed_safe`
    rather than letting it crash the request. It is never a write.
    """


class Interpreter(Protocol):
    def interpret(
        self,
        *,
        envelope: ContextEnvelope,
    ) -> Interpretation: ...


#: Assembles this turn's context, called at the moment the model is about to be
#: consulted. It is a callable rather than a value so the override path -- which
#: never asks a model -- builds nothing, and so the assembly cost is paid inside
#: the orchestrator's own failure handling.
ContextFactory = Callable[[], ContextEnvelope]


# --- dispatcher results ------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """Finance resolved the entry and no duplicate blocks it; ready to commit."""

    intent: WriteIntent


@dataclass(frozen=True)
class ReadCompleted:
    """A read tool finished with a safe, already-projected result."""

    result: str


@dataclass(frozen=True)
class PossibleDuplicate:
    duplicate_check_id: str
    intent: WriteIntent
    existing_summary: str


@dataclass(frozen=True)
class NeedsClarification:
    reason: str


@dataclass(frozen=True)
class ResolveFailedSafe:
    reason: str


ResolveOutcome = (
    Resolved | ReadCompleted | PossibleDuplicate | NeedsClarification
    | ResolveFailedSafe
)


@dataclass(frozen=True)
class Written:
    record_id: str


@dataclass(frozen=True)
class CommitUnknown:
    reason: str


@dataclass(frozen=True)
class CommitFailedSafe:
    reason: str


@dataclass(frozen=True)
class CommitDuplicateZeroWrite:
    """Finance found a duplicate and reported writing nothing.

    `finance.log_expense` is a single MCP call that resolves, duplicate-checks
    and writes, so a duplicate can only be learned once the call is under way.
    Finance reports it with zero writes and no execution row, which is the
    evidence that lets the operation park instead of resolving.
    """

    duplicate_check_id: str
    existing_summary: str


@dataclass(frozen=True)
class CommitClarificationZeroWrite:
    """A server-side resolver needs an answer, and Finance wrote nothing."""

    question: str


CommitOutcome = (
    Written
    | CommitUnknown
    | CommitFailedSafe
    | CommitDuplicateZeroWrite
    | CommitClarificationZeroWrite
)


class Dispatcher(Protocol):
    def resolve(
        self, *, tool: str, model_args: dict[str, Any]
    ) -> ResolveOutcome: ...

    def commit(
        self,
        *,
        intent: WriteIntent,
        idempotency_key: str,
        duplicate_override: str | None,
    ) -> CommitOutcome: ...


#: Authorises one tool call for the operation's device, returning cleaned
#: arguments (Host-injected fields stripped) or raising `AppError`.
class Authorizer(Protocol):
    def __call__(
        self, *, tool: str, model_args: dict[str, Any]
    ) -> dict[str, Any]: ...


# --- the outcome the API projects -------------------------------------------


@dataclass(frozen=True)
class RunResult:
    state: str
    record_id: str | None = None
    answer: str | None = None
    clarification: str | None = None
    duplicate_check_id: str | None = None
    duplicate_existing: str | None = None
    failure_reason: str | None = None


Clock = datetime | Callable[[], datetime]

#: Context failures that are the operation's own safe outcome rather than a bug:
#: the mandatory input does not fit the budget, or the Timeline/Session state
#: cannot be read consistently. Both mean zero model calls and zero writes.
_CONTEXT_FAILURES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.CONTEXT_BUDGET_EXCEEDED, ErrorCode.CONTEXT_UNAVAILABLE}
)


def run_operation(
    session,
    operation: Operation,
    *,
    build_context: ContextFactory | None = None,
    interpreter: Interpreter,
    dispatcher: Dispatcher,
    authorize: Authorizer,
    keyring: KeyRing,
    now: Clock,
) -> RunResult:
    """Drive one freshly-accepted operation to its outcome.

    Production passes a clock callable so each durable transition records when
    that transition actually happened. Tests may pass one fixed instant when
    elapsed time is irrelevant.
    """
    # A `write anyway` operation carries a resolved intent and an override; it
    # skips interpretation entirely.
    if _is_override_operation(operation):
        return _run_override(
            session, operation, dispatcher=dispatcher, keyring=keyring, now=now
        )

    if build_context is None:
        # There is no fallback shape for "ask the model with whatever we have".
        # A turn without an assembled, budget-validated envelope is a wiring
        # error, and guessing one here would put an unmeasured input in front of
        # the model exactly where `CAP-001` says one may never appear.
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="an interpreted turn needs a context envelope",
        )
    try:
        envelope = build_context()
        # End the transaction the assembly reads opened. A transaction that has
        # read cannot write once anyone else has committed (SQLite reports the
        # snapshot as unusable rather than waiting), and the model turn is the
        # longest window in the system for someone else to commit. This also
        # persists any Checkpoint the builder had to invalidate.
        session.commit()
    except AppError as refused:
        if refused.code not in _CONTEXT_FAILURES:
            raise
        # The context could not be assembled safely: no model was called and
        # nothing was written, so this is a clean pre-submit failure with the
        # reason the builder gave.
        _step(session, operation, "interpreting", now)
        _step(
            session,
            operation,
            "failed_safe",
            now,
            failure_reason=refused.code.value,
        )
        return RunResult(state="failed_safe", failure_reason=refused.code.value)

    try:
        interpretation = interpreter.interpret(envelope=envelope)
    except InterpreterError:
        # A model or transport failure is a safe failure, never a write. The
        # operation is still pre-submit, so this cannot hide a side effect.
        _step(session, operation, "interpreting", now)
        _step(session, operation, "failed_safe", now, failure_reason="model_unavailable")
        return RunResult(state="failed_safe", failure_reason="model_unavailable")

    # Do not hold SQLite's single-writer lock across the 25-second model budget.
    # The durable state remains `accepted` while the side-effect-free proposal is
    # obtained; cancellation can still win cleanly, and a crash safely replays
    # the sealed request. The state walk is committed only after the proposal.
    _step(session, operation, "interpreting", now)

    if isinstance(interpretation, Clarification):
        _step(
            session,
            operation,
            "waiting_for_clarification",
            now,
            safe_result=interpretation.question,
        )
        return RunResult(
            state="waiting_for_clarification",
            clarification=interpretation.question,
        )

    if isinstance(interpretation, FailSafeInterpretation):
        _step(
            session,
            operation,
            "failed_safe",
            now,
            failure_reason=interpretation.reason,
        )
        return RunResult(
            state="failed_safe", failure_reason=interpretation.reason
        )

    if isinstance(interpretation, DirectAnswer):
        _step(session, operation, "succeeded", now, safe_result=interpretation.text)
        return RunResult(state="succeeded", answer=interpretation.text)

    try:
        cleaned = authorize(
            tool=interpretation.tool, model_args=interpretation.model_args
        )
    except AppError as denied:
        reason = _policy_reason(denied)
        _step(session, operation, "failed_safe", now, failure_reason=reason)
        return RunResult(state="failed_safe", failure_reason=reason)

    _step(session, operation, "dispatching", now, tool=interpretation.tool)
    outcome = dispatcher.resolve(tool=interpretation.tool, model_args=cleaned)
    return _apply_resolve(session, operation, outcome, dispatcher, keyring, now)


def _apply_resolve(
    session,
    operation: Operation,
    outcome: ResolveOutcome,
    dispatcher: Dispatcher,
    keyring: KeyRing,
    now: Clock,
) -> RunResult:
    if isinstance(outcome, ReadCompleted):
        _step(session, operation, "succeeded", now, safe_result=outcome.result)
        return RunResult(state="succeeded", record_id=None, answer=outcome.result)

    if isinstance(outcome, NeedsClarification):
        _step(
            session,
            operation,
            "waiting_for_clarification",
            now,
            safe_result=outcome.reason,
        )
        return RunResult(
            state="waiting_for_clarification", clarification=outcome.reason
        )

    if isinstance(outcome, ResolveFailedSafe):
        _step(session, operation, "failed_safe", now, failure_reason=outcome.reason)
        return RunResult(state="failed_safe", failure_reason=outcome.reason)

    if isinstance(outcome, PossibleDuplicate):
        record_possible_duplicate(
            session,
            keyring,
            operation=operation,
            write_intent=outcome.intent,
            duplicate_check_id=outcome.duplicate_check_id,
            existing_summary=outcome.existing_summary,
            now=_moment(now),
        )
        return RunResult(
            state="waiting_for_duplicate_decision",
            duplicate_check_id=outcome.duplicate_check_id,
            duplicate_existing=outcome.existing_summary,
        )

    if isinstance(outcome, Resolved):
        return _commit(
            session,
            operation,
            intent=outcome.intent,
            dispatcher=dispatcher,
            duplicate_override=None,
            keyring=keyring,
            now=now,
        )

    raise AppError(  # pragma: no cover - the union is exhaustive above
        ErrorCode.INTERNAL_ERROR,
        internal_detail=f"unhandled resolve outcome {type(outcome).__name__}",
    )


def _run_override(
    session,
    operation: Operation,
    *,
    dispatcher: Dispatcher,
    keyring: KeyRing,
    now: Clock,
) -> RunResult:
    intent = open_intent(
        keyring,
        request_id=operation.request_id,
        envelope=operation.api_request.encrypted_request_payload,
    )
    _step(session, operation, "interpreting", now)
    _step(session, operation, "dispatching", now, tool=intent.tool)
    return _commit(
        session,
        operation,
        intent=intent,
        dispatcher=dispatcher,
        duplicate_override=operation.duplicate_check_id,
        keyring=keyring,
        now=now,
    )


def _commit(
    session,
    operation: Operation,
    *,
    intent: WriteIntent,
    dispatcher: Dispatcher,
    duplicate_override: str | None,
    keyring: KeyRing,
    now: Clock,
) -> RunResult:
    # Commit `source_in_progress` before the write can occur: from here a cancel
    # can no longer be reported as a clean pre-submit cancellation.
    _step(session, operation, "source_in_progress", now, tool=intent.tool)
    # This is deliberately a real transaction boundary, not merely a flush.
    # If the process dies after Finance accepts the idempotency key, startup
    # recovery must see a post-submit Agent operation and project Finance truth.
    session.commit()
    session.refresh(operation)
    outcome = dispatcher.commit(
        intent=intent,
        idempotency_key=operation.idempotency_key,
        duplicate_override=duplicate_override,
    )
    if isinstance(outcome, Written):
        record_id = outcome.record_id.strip()
        if not record_id:
            reason = "missing_verified_record_id"
            _step(
                session,
                operation,
                "needs_manual_review",
                now,
                failure_reason=reason,
            )
            return RunResult(
                state="needs_manual_review", failure_reason=reason
            )
        _step(session, operation, "verifying", now)
        _step(session, operation, "succeeded", now, safe_result=record_id)
        return RunResult(state="succeeded", record_id=record_id)
    if isinstance(outcome, CommitUnknown):
        _step(
            session, operation, "needs_manual_review", now,
            failure_reason=outcome.reason,
        )
        return RunResult(state="needs_manual_review", failure_reason=outcome.reason)
    if isinstance(outcome, CommitFailedSafe):
        _step(session, operation, "failed_safe", now, failure_reason=outcome.reason)
        return RunResult(state="failed_safe", failure_reason=outcome.reason)
    if isinstance(outcome, CommitDuplicateZeroWrite):
        # Proven zero writes, so parking is a projection of Finance's own report,
        # not an assumption. The evidence is stated explicitly at the call site.
        record_possible_duplicate(
            session,
            keyring,
            operation=operation,
            # The dispatcher may report facts about this attempt, but it cannot
            # replace the already-authorized and resolved write intent.
            write_intent=intent,
            duplicate_check_id=outcome.duplicate_check_id,
            existing_summary=outcome.existing_summary,
            now=_moment(now),
            zero_write_proven=True,
        )
        return RunResult(
            state="waiting_for_duplicate_decision",
            duplicate_check_id=outcome.duplicate_check_id,
            duplicate_existing=outcome.existing_summary,
        )
    if isinstance(outcome, CommitClarificationZeroWrite):
        _step(
            session,
            operation,
            "waiting_for_clarification",
            now,
            safe_result=outcome.question,
            zero_write_proven=True,
        )
        return RunResult(
            state="waiting_for_clarification", clarification=outcome.question
        )
    raise AppError(  # pragma: no cover - the union is exhaustive above
        ErrorCode.INTERNAL_ERROR,
        internal_detail=f"unhandled commit outcome {type(outcome).__name__}",
    )


def _is_override_operation(operation: Operation) -> bool:
    return (
        operation.duplicate_check_id is not None
        and operation.api_request.encrypted_request_payload is not None
    )


def _policy_reason(error: AppError) -> str:
    # design 5.2: policy_denied is a stable reason on failed_safe, not a state.
    if error.code in {ErrorCode.SCOPE_DENIED, ErrorCode.TOOL_NOT_ALLOWLISTED}:
        return "policy_denied"
    return error.code.value


def _step(
    session,
    operation: Operation,
    target: str,
    now: Clock,
    *,
    tool: str | None = None,
    safe_result: str | None = None,
    failure_reason: str | None = None,
    zero_write_proven: bool = False,
) -> None:
    session.refresh(operation)
    transition_operation(
        session,
        operation_id=operation.operation_id,
        current_state=operation.state,
        current_version=operation.state_version,
        target_state=target,
        now=_moment(now),
        tool=tool,
        safe_result=safe_result,
        failure_reason=failure_reason,
        zero_write_proven=zero_write_proven,
    )
    # Each transition is durable on its own, and the transaction closes here
    # rather than staying open across the next model or MCP call. The state
    # machine is forward-only, so there is nothing a later failure would want
    # to take back -- and a half-hour-old read snapshot is exactly what makes
    # a later write fail.
    session.commit()
    session.refresh(operation)


def _moment(clock: Clock) -> datetime:
    """Resolve a fresh transition instant without breaking fixed-time tests."""
    return clock() if callable(clock) else clock
