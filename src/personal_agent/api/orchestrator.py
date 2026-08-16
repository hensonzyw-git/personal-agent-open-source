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

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from personal_agent.api.duplicate_flow import record_possible_duplicate
from personal_agent.api.finance_query_projection import FinanceQueryProjection
from personal_agent.api.finance_record_projection import (
    FinanceExpenseRecord,
    seal_expense_record,
)
from personal_agent.api.intent import WriteIntent, open_intent
from personal_agent.api.operation_store import transition_operation
from personal_agent.context.builder import ContextEnvelope
from personal_agent.diagnostics import transcript
from personal_agent.diagnostics.transcript import NullRecorder, Recorder
from personal_agent.storage.models import Operation
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import (
    MODEL_RETRYABLE_FAILURE_REASONS,
    AppError,
    ErrorCode,
    ModelFailureReason,
)
from personal_agent_core.finance_tools import (
    FINANCE_HOST_DEFAULT_OCCURRED_ON_TOOLS,
    FINANCE_QUERY_TOOL,
    FINANCE_WRITE_TOOLS,
)
from personal_agent_core.timeutil import format_ledger_date, ledger_date

# --- interpreter results -----------------------------------------------------


logger = logging.getLogger(__name__)


# `agent.ask_clarification.reason` is model-generated control metadata.  It is
# schema-validated, but it is not a trustworthy statement that the question
# really concerns a date: GLM has returned a plainly date-only question with
# `reason="other"` in production.  These markers are used only to enable the
# already side-effect-free, clarification-free retry below; they never supply a
# business argument or authorise a write.
_DATE_QUESTION_RE = re.compile(
    r"(?:日期|哪天|何时|什么时候|今天|昨天|前天|几月|几日|几号)"
)


@dataclass(frozen=True)
class DirectAnswer:
    """A conversational reply with no tool call and no side effect."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """A model-chosen tool and its model-facing arguments.

    ``suppressed_untrusted_text`` is a response-contract disposition, not model
    content. It is true only when the gateway received one valid tool call plus
    prose; that prose has already been excluded and may not influence policy,
    dispatch or an operation result.
    """

    tool: str
    model_args: dict[str, Any]
    suppressed_untrusted_text: bool = False


@dataclass(frozen=True)
class Clarification:
    """A structured question; it parks instead of pretending to be an answer.

    Provider prose beside the control call is never used as the question; when
    present, its suppression disposition is retained for safe observability.
    """

    question: str
    suppressed_untrusted_text: bool = False
    #: A closed control-plane classification emitted by the model gateway.
    #: It is never a user-visible explanation and is not accepted without the
    #: gateway's schema validation.
    reason: str | None = None


@dataclass(frozen=True)
class FailSafeInterpretation:
    """A model-selected outcome backed by a frozen, non-write safety gate.

    Any prose beside the control call remains suppressed and cannot change the
    fixed failure reason.
    """

    reason: str
    suppressed_untrusted_text: bool = False


Interpretation = DirectAnswer | ToolCall | Clarification | FailSafeInterpretation


class InterpreterError(Exception):
    """The interpreter could not produce a proposal.

    Part of the seam contract: an implementation raises this for a model or
    transport failure, and the orchestrator turns it into a safe `failed_safe`
    rather than letting it crash the request. It is never a write.
    """

    def __init__(
        self,
        message: str,
        *,
        failure_reason: str = ModelFailureReason.UNAVAILABLE.value,
    ) -> None:
        # Do not let an arbitrary third-party exception turn into a durable
        # public reason.  Gateway-derived reasons are a closed, reviewed set.
        self.failure_reason = (
            failure_reason
            if failure_reason in MODEL_RETRYABLE_FAILURE_REASONS
            else ModelFailureReason.UNAVAILABLE.value
        )
        super().__init__(message)


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
    """A read tool finished with a safe, already-projected result.

    ``result`` is the durable ``safe_result`` carrier. For
    ``finance.query_expenses`` the read also carries the validated
    ``projection`` and a deterministic ``answer`` fallback for clients that
    predate ``query_result``; every other read carries only the result string.
    """

    result: str
    projection: FinanceQueryProjection | None = None
    answer: str | None = None


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
    #: The written row, strictly projected, for the `G1` receipt card. Optional
    #: and *subordinate to* `record_id`: the record id is the write's proof,
    #: this is only what the card draws. An `idempotent_replay` legitimately has
    #: none, and an unprojectable one is downgraded to none rather than being
    #: allowed to turn a proven write into a failure.
    record: FinanceExpenseRecord | None = None


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
    #: The validated, whitelisted ``finance.query_expenses`` projection, present
    #: only when the read was a query that decoded successfully.
    query_result: dict[str, Any] | None = None


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
    pre_resolved: bool = False,
    recorder: Recorder | None = None,
) -> RunResult:
    """Drive one freshly-accepted operation to its outcome.

    Production passes a clock callable so each durable transition records when
    that transition actually happened. Tests may pass one fixed instant when
    elapsed time is irrelevant.

    The transcript record is written here, around every exit path at once,
    because this function returns a safe result from a dozen places and raises
    from more. One wrapper cannot miss a path the way a dozen call sites could.
    """
    sink = recorder or NullRecorder()
    try:
        result = _run_operation(
            session,
            operation,
            build_context=build_context,
            interpreter=interpreter,
            dispatcher=dispatcher,
            authorize=authorize,
            keyring=keyring,
            now=now,
            pre_resolved=pre_resolved,
        )
    except Exception as exc:
        sink.record(
            transcript.TURN_RESULT,
            {
                "state": "raised",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise
    sink.record(transcript.TURN_RESULT, {"state": result.state, "result": result})
    return result


def _run_operation(
    session,
    operation: Operation,
    *,
    build_context: ContextFactory | None = None,
    interpreter: Interpreter,
    dispatcher: Dispatcher,
    authorize: Authorizer,
    keyring: KeyRing,
    now: Clock,
    pre_resolved: bool = False,
) -> RunResult:
    # An operation that already knows its write skips interpretation entirely:
    # a `write anyway` override, or a deterministic user action such as the
    # receipt card's category picker. Neither has anything to ask a model.
    if _is_pre_resolved(operation, declared=pre_resolved):
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

    if envelope.finance_retry_unbound:
        # An explicit replay phrase is not an independent write instruction.
        # Without a server-sealed, zero-write source it must stop before the
        # provider sees it; otherwise a model can reinterpret "重新记" as a new
        # write and defeat the one-shot retry contract.
        _step(session, operation, "interpreting", now)
        reason = ErrorCode.BOOKKEEPING_TOOL_REQUIRED.value
        _step(
            session,
            operation,
            "failed_safe",
            now,
            failure_reason=reason,
        )
        return RunResult(state="failed_safe", failure_reason=reason)

    try:
        interpretation = interpreter.interpret(envelope=envelope)
        if _requires_date_default_retry(envelope, interpretation):
            # A missing date is deterministically filled from the durable
            # request receipt after the model chooses a Finance tool.  It is
            # therefore not a user-information gap.  Give the model one
            # side-effect-free retry whose allowed function set excludes every
            # clarification call; it can select a governed Finance tool or
            # fail safely, but cannot ask the user for today's date.
            logger.info(
                "model date clarification retried operation_id=%s trace_id=%s",
                operation.operation_id,
                operation.trace_id,
            )
            envelope = envelope.with_finance_date_default_retry()
            interpretation = interpreter.interpret(envelope=envelope)
    except InterpreterError as exc:
        # A model or transport failure is a safe failure, never a write. The
        # operation is still pre-submit, so this cannot hide a side effect.
        logger.warning(
            "model turn failed operation_id=%s trace_id=%s reason=%s",
            operation.operation_id,
            operation.trace_id,
            exc.failure_reason,
        )
        _step(session, operation, "interpreting", now)
        _step(
            session,
            operation,
            "failed_safe",
            now,
            failure_reason=exc.failure_reason,
        )
        return RunResult(state="failed_safe", failure_reason=exc.failure_reason)

    # Do not hold SQLite's single-writer lock across the 25-second model budget.
    # The durable state remains `accepted` while the side-effect-free proposal is
    # obtained; cancellation can still win cleanly, and a crash safely replays
    # the sealed request. The state walk is committed only after the proposal.
    _step(session, operation, "interpreting", now)

    if (
        isinstance(
            interpretation, (ToolCall, Clarification, FailSafeInterpretation)
        )
        and interpretation.suppressed_untrusted_text
    ):
        # This is a fixed, content-free audit record. The accompanying model
        # prose is intentionally not retained; it cannot be mistaken for a
        # user-visible result, tool argument or external-write evidence.
        logger.info(
            "model response accepted operation_id=%s trace_id=%s "
            "response_disposition=tool_text_suppressed_untrusted",
            operation.operation_id,
            operation.trace_id,
        )

    if isinstance(interpretation, Clarification):
        if envelope.finance_date_default_retry:
            # The gateway itself should have rejected this shape because the
            # retry has no clarification function in its allowed set.  Keep a
            # second guard at the orchestration boundary so a faulty adapter
            # cannot surface any forbidden question to the user.
            reason = _finance_required_reason(envelope)
            _step(session, operation, "failed_safe", now, failure_reason=reason)
            return RunResult(state="failed_safe", failure_reason=reason)
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
        if (
            interpretation.reason == ErrorCode.TOOL_NOT_ALLOWLISTED.value
            and _required_finance_tool_is_visible(envelope)
        ):
            # The provider cannot overrule the governed catalog it was shown.
            # If Finance is visible for this turn, "not supported" is a routing
            # failure, not a truthful capability result.
            required_reason = _finance_required_reason(envelope)
            _step(
                session,
                operation,
                "failed_safe",
                now,
                failure_reason=required_reason,
            )
            return RunResult(
                state="failed_safe",
                failure_reason=required_reason,
            )
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
        if envelope.finance_intent_required:
            # `DEV-040`: a bookkeeping request answered with prose is not a
            # write. A `DirectAnswer` carries no side effect, so accepting it
            # here would record a fake success with zero writes -- exactly what
            # §5.1 forbids. Refuse; the model may still call the tool on a
            # retry, and the refusal itself does not become a success projection.
            required_reason = _finance_required_reason(envelope)
            _step(
                session,
                operation,
                "failed_safe",
                now,
                failure_reason=required_reason,
            )
            return RunResult(
                state="failed_safe",
                failure_reason=required_reason,
            )
        _step(session, operation, "succeeded", now, safe_result=interpretation.text)
        return RunResult(state="succeeded", answer=interpretation.text)

    required_finance_tools = _required_finance_tools(envelope)
    if (
        required_finance_tools
        and interpretation.tool not in required_finance_tools
    ):
        # Internal clarification/fail-safe calls have already been converted to
        # their structured interpretation types. Any remaining tool outside the
        # required Finance effect class is a routing error, not an acceptable
        # completion of this Finance turn.
        required_reason = _finance_required_reason(envelope)
        _step(
            session,
            operation,
            "failed_safe",
            now,
            failure_reason=required_reason,
        )
        return RunResult(
            state="failed_safe",
            failure_reason=required_reason,
        )

    model_args = _with_host_defaulted_occurred_on(
        envelope, operation, interpretation
    )
    try:
        cleaned = authorize(tool=interpretation.tool, model_args=model_args)
    except AppError as denied:
        reason = _policy_reason(denied)
        _step(session, operation, "failed_safe", now, failure_reason=reason)
        return RunResult(state="failed_safe", failure_reason=reason)

    _step(session, operation, "dispatching", now, tool=interpretation.tool)
    outcome = dispatcher.resolve(tool=interpretation.tool, model_args=cleaned)
    return _apply_resolve(session, operation, outcome, dispatcher, keyring, now)


def _with_host_defaulted_occurred_on(
    envelope: ContextEnvelope, operation: Operation, interpretation: ToolCall
) -> dict[str, Any]:
    """Fill an omitted Finance write date only when source text had no date.

    An explicit null, empty string or malformed date remains model output and is
    deliberately *not* repaired here; schema validation then rejects it.  An
    omitted date after an explicit user date is also not repaired: it must fail
    safely rather than turn yesterday's payment into today.  Where defaulting is
    allowed, the durable request receipt, rather than the worker's clock,
    defines "today".
    """
    arguments = interpretation.model_args
    if (
        interpretation.tool not in FINANCE_HOST_DEFAULT_OCCURRED_ON_TOOLS
        or "occurred_on" in arguments
        or not envelope.finance_date_default_eligible
    ):
        return arguments
    return {
        **arguments,
        "occurred_on": format_ledger_date(
            ledger_date(operation.api_request.received_at)
        ),
    }


def _requires_date_default_retry(
    envelope: ContextEnvelope, interpretation: Interpretation
) -> bool:
    """Whether a write's omitted date was incorrectly treated as a gap.

    The model's closed ``reason`` field is a useful signal but cannot be the
    sole gate: a production response asked "是今天还是其他日期？" under
    ``reason="other"``.  Looking at the bounded clarification question can
    only unlock one retry with clarification removed; it never supplies an
    argument or bypasses policy.  In contrast, an explicit date in the user's
    input is authoritative and must never be overwritten with the receipt day.
    That source-level fact is carried by the trusted Builder, rather than
    inferred from a possible continuation answer.
    """
    return (
        envelope.finance_intent_required
        and envelope.finance_required_tool is None
        and envelope.finance_date_default_eligible
        and not envelope.finance_date_default_retry
        and isinstance(interpretation, Clarification)
        and (
            interpretation.reason == "date"
            or _DATE_QUESTION_RE.search(interpretation.question) is not None
        )
    )


def _finance_required_reason(envelope: ContextEnvelope) -> str:
    if envelope.finance_required_tool == FINANCE_QUERY_TOOL:
        return ErrorCode.FINANCE_TOOL_REQUIRED.value
    return ErrorCode.BOOKKEEPING_TOOL_REQUIRED.value


def _required_finance_tools(envelope: ContextEnvelope) -> frozenset[str]:
    """Return the only governed Finance tool class valid for this turn."""
    if not envelope.finance_intent_required:
        return frozenset()
    if envelope.finance_required_tool is not None:
        return frozenset({envelope.finance_required_tool})
    return FINANCE_WRITE_TOOLS


def _required_finance_tool_is_visible(envelope: ContextEnvelope) -> bool:
    """Whether the governed catalog disproves a model's unsupported claim."""
    required = _required_finance_tools(envelope)
    return bool(required.intersection(envelope.tool_aliases))


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
        if outcome.projection is not None:
            return RunResult(
                state="succeeded",
                record_id=None,
                answer=outcome.answer,
                query_result=outcome.projection.to_dict(),
            )
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
        _step(
            session,
            operation,
            "succeeded",
            now,
            safe_result=record_id,
            # `G1`. Sealed here rather than in the dispatcher so the key never
            # travels to the MCP layer, and written in the same transition as
            # the record id so a card can never show fields for a write whose
            # id was not committed.
            encrypted_result_record=(
                seal_expense_record(
                    keyring,
                    operation_id=operation.operation_id,
                    record=outcome.record,
                )
                if outcome.record is not None
                else None
            ),
        )
        return RunResult(state="succeeded", record_id=record_id)
    if isinstance(outcome, CommitUnknown):
        # An unknown commit is *not* an outcome. It is the absence of one, and
        # this branch used to record it as the most conservative outcome it could
        # name -- terminal `needs_manual_review`. Terminal means recovery never
        # looks at it again, so when Finance went on to reconcile the very same
        # idempotency key to `succeeded`, the two state machines disagreed
        # permanently: the 2026-08-04 §13.2 drill killed Finance at `prepared`,
        # Finance recovered and verified record `recvrlVQllHop0`, and the Agent
        # still showed "needs manual review" with `record_id=null`.
        #
        # So leave it exactly where it is: `source_in_progress`, committed before
        # the call left, which is a recoverable state. `plan_recovery` already
        # knows how to project every Finance status onto it -- including the
        # absence of an execution, which it treats as a race rather than proof
        # once the operation has been quiet longer than any live worker's ceiling.
        # Recovery, not this worker, decides what happened, because recovery is
        # the only one that can read Finance *after* the fact.
        #
        # Nothing is written here on purpose: a self-transition is not a legal
        # edge, and stamping `failure_reason` would age `updated_at` and describe
        # a failure that has not been established. The reason travels to the
        # caller only, as the transient explanation of why this turn ended
        # without a verdict.
        return RunResult(state=operation.state, failure_reason=outcome.reason)
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


def _is_pre_resolved(operation: Operation, *, declared: bool) -> bool:
    """Whether this operation already knows its write and must skip the model.

    Two shapes qualify, and they are kept distinguishable on purpose:

    - a `write anyway` override, recognised by its `duplicate_check_id`. The id
      is both the marker and the authorisation, so inferring it is safe;
    - a caller that *declares* the operation pre-resolved, which is how the
      category-correction route arrives. It carries no duplicate check and there
      is nothing on the row to infer from, so the caller states it and the
      sealed intent still has to be there.

    The `declared` route deliberately does not widen the first: an operation is
    not treated as an override just because someone said "pre-resolved", so it
    cannot acquire duplicate-override authority it was never granted.
    """
    if _is_override_operation(operation):
        return True
    return declared and operation.api_request.encrypted_request_payload is not None


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
    encrypted_result_record: dict[str, Any] | None = None,
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
        encrypted_result_record=encrypted_result_record,
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
