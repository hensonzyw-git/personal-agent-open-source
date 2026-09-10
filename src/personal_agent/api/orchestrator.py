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
from personal_agent.api.device_action_projection import seal_device_action
from personal_agent.api.finance_query_projection import FinanceQueryProjection
from personal_agent.api.finance_record_projection import (
    FinanceExpenseRecord,
    seal_expense_record,
)
from personal_agent.api.intent import WriteIntent, open_intent
from personal_agent.api.operation_request import (
    OperationRequestError,
    open_operation_request,
    seal_operation_request,
)
from personal_agent.api.operation_store import (
    join_action_plan,
    open_plan_item,
    plan_item_fingerprint,
    plan_item_key,
    plan_operations,
    transition_operation,
)
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
from personal_agent_core.tool_ir import DEVICE_EXECUTED_TOOL_NAMES

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
class ToolCalls:
    """Several tool calls from one model turn, in the order it made them.

    One message can ask for several things, and the natural shape for the answer
    is one call per thing. The order is carried because it becomes a position in
    the frozen plan, not a transport detail.

    This is a shape, not an authorisation: only a list where every call is
    executed by the device is run, and only after the whole list has been
    written down (design 4.1/4.2, Henson 2026-09-10). Anything else is refused
    exactly as a multi-call response always was.
    """

    calls: tuple[ToolCall, ...]
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


Interpretation = (
    DirectAnswer | ToolCall | ToolCalls | Clarification | FailSafeInterpretation
)


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
class DeviceActionIssued:
    """A device-executed write was authorised and issued to the phone.

    `calendar.create_event` never crosses the MCP bridge: the iPhone's
    EventKit is the executor. `resolve` authorises exactly like any governed
    write and then stops — nothing has been *sent* anywhere, because the
    "send" is the chat response itself carrying the action to the device that
    asked for it.

    `action_id` is the operation's own idempotency key, so one message can
    produce at most one device side effect and the device's report PATCHes the
    same operation the response came from. `event_fields` is the
    schema-validated model input, carried alongside the routing decision the
    server made: the phone builds the EKEvent from exactly what was authorised,
    not from anything re-derived.

    `wire_version` travels with the action so the phone and the Host agree on
    what its fields mean, rather than each assuming the other's reading. A
    client that does not implement it is never handed the action at all
    (design 2.5).
    """

    action_id: str
    tool: str
    wire_version: int
    event_fields: dict[str, Any]

    def response_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "tool": self.tool,
            "wire_version": self.wire_version,
            "event": self.event_fields,
        }


@dataclass(frozen=True)
class ReadCompleted:
    """A read tool finished with a safe, already-projected result.

    ``result`` is the durable ``safe_result`` carrier. For
    ``finance.query_expenses`` the read also carries the validated
    ``projection`` and a deterministic ``answer`` fallback for clients that
    predate ``query_result``; every other read carries only the result string.
    ``projection`` is either a ``FinanceQueryProjection`` (whose ``to_dict()``
    the API serves as ``query_result``) or an already-decoded calendar
    projection dict (whose strict decoder returned exactly the display
    shape) — the two query domains share the same client field, so this
    carries whichever validated projection the read produced.
    """

    result: str
    projection: FinanceQueryProjection | dict[str, Any] | None = None
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
    | ResolveFailedSafe | DeviceActionIssued
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
        self,
        *,
        tool: str,
        model_args: dict[str, Any],
        idempotency_key: str | None = None,
        skip_local_dedup: bool = False,
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
    #: The device-executed actions (`calendar.create_event`) this turn issued,
    #: in the order the model asked for them. Always a tuple, even for the one
    #: action a single-call turn issues, so a caller cannot come to depend on a
    #: length that only one of the two shapes has. Each action's operation is
    #: parked at `source_in_progress`; settlement arrives later, per action, via
    #: the device-action result endpoint.
    device_actions: tuple[dict[str, Any], ...] = ()


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
    prior_clarification_question: str | None = None,
    recorder: Recorder | None = None,
    action_keyring: KeyRing | None = None,
) -> RunResult:
    """Drive one freshly-accepted operation to its outcome.

    Production passes a clock callable so each durable transition records when
    that transition actually happened. Tests may pass one fixed instant when
    elapsed time is irrelevant.

    `action_keyring` seals an issued device action onto its operation (review
    R6). It is a separate keyring because the data keyring's callers must not
    silently gain the ability to write the device-action column, and `None`
    (tests of non-device paths) makes a device issue fail loudly instead of
    producing an operation nobody can deliver.

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
            prior_clarification_question=prior_clarification_question,
            action_keyring=action_keyring,
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
    prior_clarification_question: str | None = None,
    action_keyring: KeyRing | None = None,
) -> RunResult:
    # An operation that already knows its write skips interpretation entirely:
    # a `write anyway` override, or a deterministic user action such as the
    # receipt card's category picker. Neither has anything to ask a model.
    if _is_pre_resolved(operation, declared=pre_resolved):
        if _is_calendar_override(operation):
            return _run_calendar_override(
                session,
                operation,
                dispatcher=dispatcher,
                authorize=authorize,
                keyring=keyring,
                action_keyring=action_keyring,
                now=now,
            )
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
            interpretation,
            (ToolCall, ToolCalls, Clarification, FailSafeInterpretation),
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

    if isinstance(interpretation, ToolCalls):
        # A list is a turn of its own, and it is decided before the single-call
        # tail: several calls are not one call with extra steps, and every check
        # below (`required_finance_tools`, the date default) reads a single
        # `interpretation.tool`. A list that is not entirely device-executed
        # keeps the refusal a multi-call response always got.
        return _run_action_plan(
            session,
            operation,
            interpretation,
            dispatcher=dispatcher,
            authorize=authorize,
            keyring=keyring,
            action_keyring=action_keyring,
            now=now,
        )

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
    outcome = dispatcher.resolve(
        tool=interpretation.tool,
        model_args=cleaned,
        idempotency_key=operation.idempotency_key,
    )
    return _apply_resolve(
        session,
        operation,
        outcome,
        dispatcher,
        keyring,
        now,
        prior_clarification_question=prior_clarification_question,
        action_keyring=action_keyring,
        intent=WriteIntent(tool=interpretation.tool, model_args=cleaned),
    )


def _run_action_plan(
    session,
    operation: Operation,
    interpretation: ToolCalls,
    *,
    dispatcher: Dispatcher,
    authorize: Authorizer,
    keyring: KeyRing,
    action_keyring: KeyRing | None,
    now: Clock,
) -> RunResult:
    """Issue one message's several calendar events as one frozen plan.

    Issuance is all-or-nothing (Henson, 2026-09-10; design 4.2). A row carries
    one state, and "an action may already be with the phone" cannot sit beside
    "waiting for the user to answer" -- but the deeper reason is that a
    half-issued list has no honest repair: keeping the parked items freezes
    arguments that go stale, and re-running the turn would re-issue events the
    phone already wrote.

    So there are two passes, and the order between them is the whole design.
    The first authorises and resolves every item without writing anything down.
    If any item cannot be issued, that outcome is applied to the message's own
    operation and the turn ends there: no rows, no freeze, nothing parked -- so
    the user's answer can re-run the whole turn from scratch, and nothing has
    been written twice. Only when every item is issuable is the list written
    down (design 4.1) and then parked item by item.

    A crash inside the second pass is what the freeze exists for: the rows carry
    their attested arguments, so `resume_action_plan` continues from the list
    instead of asking a model again.
    """
    calls = interpretation.calls
    if any(call.tool not in DEVICE_EXECUTED_TOOL_NAMES for call in calls):
        # Several calls are only ever a plan when the phone executes all of
        # them. Anything else -- two Finance writes, a read beside a write, a
        # tool the catalog does not have -- keeps the refusal a multi-call
        # response has always got: there is no ordering of those that is safe.
        reason = ModelFailureReason.RESPONSE_AMBIGUOUS.value
        _step(session, operation, "failed_safe", now, failure_reason=reason)
        return RunResult(state="failed_safe", failure_reason=reason)

    plan_key = operation.idempotency_key
    intents: list[WriteIntent] = []
    outcomes: list[ResolveOutcome] = []
    for index, call in enumerate(calls):
        try:
            cleaned = authorize(tool=call.tool, model_args=call.model_args)
        except AppError as denied:
            reason = _policy_reason(denied)
            _step(session, operation, "failed_safe", now, failure_reason=reason)
            return RunResult(state="failed_safe", failure_reason=reason)
        intents.append(WriteIntent(tool=call.tool, model_args=cleaned))
        outcomes.append(
            dispatcher.resolve(
                tool=call.tool,
                model_args=cleaned,
                idempotency_key=_plan_action_key(plan_key, index),
            )
        )

    unmet = next(
        (
            outcome
            for outcome in outcomes
            if not isinstance(outcome, DeviceActionIssued)
        ),
        None,
    )
    if unmet is not None:
        # The first item that cannot be issued decides the turn. Which one it is
        # does not change the outcome -- all of them are withheld either way --
        # but reporting the first keeps the message the user sees the one about
        # the thing they said first.
        return _apply_resolve(
            session,
            operation,
            unmet,
            dispatcher,
            keyring,
            now,
            action_keyring=action_keyring,
        )

    if action_keyring is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="action plan issued with no action keyring",
        )

    # Item 0 is this message's own operation: it is the request the user is
    # talking to, so it is the plan's first item rather than a second row.
    _step(session, operation, "dispatching", now, tool=calls[0].tool)
    rows = _freeze_action_plan(
        session,
        operation,
        plan_key=plan_key,
        intents=intents,
        action_keyring=action_keyring,
        now=now,
    )

    issued: list[dict[str, Any]] = []
    for row, intent, outcome in zip(rows, intents, outcomes):
        assert isinstance(outcome, DeviceActionIssued)
        single = _apply_resolve(
            session,
            row,
            outcome,
            dispatcher,
            keyring,
            now,
            action_keyring=action_keyring,
            intent=intent,
        )
        issued.extend(single.device_actions)
    return RunResult(state="source_in_progress", device_actions=tuple(issued))


def _plan_action_key(plan_key: str, index: int) -> str:
    """The one idempotency key this item's action may ever carry.

    Item 0's key is the message's own, because item 0 *is* the message's
    operation; every later item's is derived from the plan. See `plan_item_key`.
    """
    return plan_key if index == 0 else plan_item_key(plan_key, index)


def _freeze_action_plan(
    session,
    operation: Operation,
    *,
    plan_key: str,
    intents: list[WriteIntent],
    action_keyring: KeyRing,
    now: Clock,
) -> list[Operation]:
    """Write the whole turn's list down before any of it is issued (design 4.1).

    One transaction, so the plan is either entirely on disk or not at all: a
    list half-written is a list whose remaining items nobody can name. Each row
    is created already at `dispatching` with its attested arguments sealed --
    the seal is bound to the row's own `operation_id`, which is why it is
    written immediately after the row is created rather than in the same INSERT
    -- and the transaction commits once, after the last one.

    Item 0 is sealed here too, even though parking it seals its request again.
    The two seals say the same thing, and the one written here is what makes the
    *whole* list resumable: a crash between this commit and item 0's park would
    otherwise leave a row at `dispatching` inside a frozen plan with no retained
    arguments, and `resume_action_plan` -- whose only input is the rows -- would
    have nothing to continue from on the item the user is actually talking to.

    Re-running this is safe and is what a resumed turn does: the derived key
    names the same position of the same message, so an item that already exists
    is read back instead of duplicated.

    `trace_id` is the message's own, because this is one turn: the items are
    steps of the same request, and a reader following the trace should find all
    of them. Only the message operation's own trace is a fresh traceparent.
    """
    moment = _moment(now)
    join_action_plan(
        session, operation_id=operation.operation_id, plan_key=plan_key, now=moment
    )
    if operation.encrypted_request is None:
        operation.encrypted_request = seal_operation_request(
            action_keyring,
            operation_id=operation.operation_id,
            intent=intents[0],
        )
    rows: list[Operation] = [operation]
    for index, intent in enumerate(intents[1:], start=1):
        opened = open_plan_item(
            session,
            device_id=operation.api_request.device_id,
            plan_key=plan_key,
            plan_index=index,
            request_fingerprint=plan_item_fingerprint(
                plan_key=plan_key,
                index=index,
                tool=intent.tool,
                args=intent.model_args,
            ),
            now=moment,
            trace_id=operation.trace_id,
        )
        if opened.created:
            # An existing row was written by a previous attempt in this same
            # transaction's shape and already carries its own seal; re-sealing
            # it would only replace one readable envelope with another.
            opened.operation.encrypted_request = seal_operation_request(
                action_keyring,
                operation_id=opened.operation.operation_id,
                intent=intent,
            )
        rows.append(opened.operation)
    session.commit()
    return rows


def resume_action_plan(
    session,
    operation: Operation,
    *,
    dispatcher: Dispatcher,
    authorize: Authorizer,
    keyring: KeyRing,
    action_keyring: KeyRing | None,
    now: Clock,
) -> RunResult | None:
    """Finish issuing the items of a frozen plan that never got an action.

    The freeze is what makes this possible: the item's authorised arguments are
    on its own row, so a crash between two items continues from the list that
    was written down rather than from a second model turn that could come back
    with a different list. Nothing here asks a model, and nothing re-derives an
    argument: the seal is opened, re-authorised (policy may have moved: the kill
    switch, the device's scopes, the calendar's writability) and re-resolved.

    A row already parked is left alone -- its action is sealed on it and the
    projection hands it over -- and a row that reached a terminal state is
    finished, however it got there. So the caller can call this on every replay:
    it returns `None` unless this operation is a frozen plan's first item with
    at least one item still waiting to be issued, and the caller's existing
    behaviour is untouched for every other shape.

    One item failing here does not undo its siblings: they may already be in the
    phone's calendar. The item takes its own outcome (a `failed_safe` for a
    refusal, a question it can no longer ask through the parked message
    otherwise) and the plan is left visibly partial.
    """
    if operation.plan_key is None or operation.plan_index != 0:
        return None
    rows = plan_operations(session, operation.plan_key)
    if not any(row.state == "dispatching" for row in rows):
        return None
    if action_keyring is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="a frozen action plan arrived with no action keyring",
        )

    issued: list[dict[str, Any]] = []
    for row in rows:
        if row.state != "dispatching":
            continue
        if row.encrypted_request is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=(
                    f"frozen plan item {row.operation_id} kept no request to resume"
                ),
            )
        try:
            intent = open_operation_request(
                action_keyring,
                operation_id=row.operation_id,
                envelope=row.encrypted_request,
            )
        except OperationRequestError as unreadable:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=str(unreadable),
            ) from unreadable

        try:
            cleaned = authorize(tool=intent.tool, model_args=intent.model_args)
        except AppError as denied:
            _step(
                session,
                row,
                "failed_safe",
                now,
                failure_reason=_policy_reason(denied),
            )
            continue
        outcome = dispatcher.resolve(
            tool=intent.tool,
            model_args=cleaned,
            idempotency_key=row.idempotency_key,
        )
        single = _apply_resolve(
            session,
            row,
            outcome,
            dispatcher,
            keyring,
            now,
            action_keyring=action_keyring,
            intent=WriteIntent(tool=intent.tool, model_args=cleaned),
        )
        issued.extend(single.device_actions)
    session.refresh(operation)
    return RunResult(state=operation.state, device_actions=tuple(issued))


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
        and (
            envelope.finance_required_tool is None
            or envelope.finance_required_tool
            in FINANCE_HOST_DEFAULT_OCCURRED_ON_TOOLS
        )
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
    *,
    prior_clarification_question: str | None = None,
    action_keyring: KeyRing | None = None,
    intent: WriteIntent | None = None,
) -> RunResult:
    if isinstance(outcome, ReadCompleted):
        _step(session, operation, "succeeded", now, safe_result=outcome.result)
        if outcome.projection is not None:
            # A Finance projection carries a to_dict(); a calendar projection
            # is already the decoded display dict. Both are strict decoders'
            # outputs, so what travels to the client is the validated shape.
            query_result = (
                outcome.projection.to_dict()
                if isinstance(outcome.projection, FinanceQueryProjection)
                else outcome.projection
            )
            return RunResult(
                state="succeeded",
                record_id=None,
                answer=outcome.answer,
                query_result=query_result,
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
            prior_clarification_question=prior_clarification_question,
        )

    if isinstance(outcome, DeviceActionIssued):
        # Post-submit semantics: `source_in_progress` commits before the
        # response leaves, so the phone may write the event while the message
        # is in flight, and a crash here cannot be read as a cancellation.
        # The device's report settles the operation later through its own
        # endpoint; a report that never arrives is handled by the timeout
        # sweep, not by this turn.
        #
        # The action is sealed onto the operation in this same transition
        # (review R6, 2026-09-08): the chat response is no longer the action's
        # only delivery channel, so a request that times out at 202 still
        # leaves an undelivered action the poll can hand over, and a settled
        # operation refuses to hand it over again. A missing keyring is a
        # wiring bug and fails loudly here rather than silently un-delivering
        # the action.
        if action_keyring is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="device action issued with no action keyring",
            )
        # The authorised request is sealed beside the action, in the same
        # committed transition (design 3.3). An issued action is also a
        # promise the user can act on: if the phone reports a duplicate, the
        # receipt offers 「仍要创建」, and that decision has to resume *these*
        # arguments rather than re-derive them. It is sealed here, where the
        # attested arguments are in hand, rather than at settlement, where
        # they no longer exist -- and unlike the action it is not cleared when
        # the operation settles, because settlement is when it is needed. A
        # missing intent is a wiring bug, like a missing keyring.
        if intent is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="device action issued with no request to retain",
            )
        _step(
            session,
            operation,
            "source_in_progress",
            now,
            tool=outcome.tool,
            encrypted_device_action=seal_device_action(
                action_keyring,
                operation_id=operation.operation_id,
                action=outcome.response_payload(),
            ),
            encrypted_request=seal_operation_request(
                action_keyring,
                operation_id=operation.operation_id,
                intent=intent,
            ),
        )
        return RunResult(
            state="source_in_progress",
            device_actions=(outcome.response_payload(),),
        )

    raise AppError(  # pragma: no cover - the union is exhaustive above
        ErrorCode.INTERNAL_ERROR,
        internal_detail=f"unhandled resolve outcome {type(outcome).__name__}",
    )


def _run_calendar_override(
    session,
    operation: Operation,
    *,
    dispatcher: Dispatcher,
    authorize: Authorizer,
    keyring: KeyRing,
    action_keyring: KeyRing | None,
    now: Clock,
) -> RunResult:
    """Re-issue a calendar write the user answered 「仍要创建」 to (design 3.3).

    The one thing an override must never do is ask a model. Between the
    duplicate report and the tap the world moves -- the directory syncs, the
    conversation continues -- and a re-derived request would be a *different*
    write wearing the original's receipt. So the arguments come from the seal
    the original turn wrote when it issued the action, and the only difference
    is the instruction the phone executes them under.

    Policy is deliberately re-checked, even though this request was authorised
    once already. The gap is real: the kill switch may have been turned off, the
    device may have been revoked, the calendar may have gone read-only or
    ambiguous. An override is a new write and is authorised like one. What is
    *not* re-done is the user's own decision -- it is what this endpoint exists
    to carry, and asking again would be the duplicate check they just overruled.
    """
    if action_keyring is None or operation.encrypted_request is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="a calendar override arrived with no retained request",
        )
    try:
        intent = open_operation_request(
            action_keyring,
            operation_id=operation.operation_id,
            envelope=operation.encrypted_request,
        )
    except OperationRequestError as unreadable:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=str(unreadable),
        ) from unreadable

    _step(session, operation, "interpreting", now)
    try:
        cleaned = authorize(tool=intent.tool, model_args=intent.model_args)
    except AppError as denied:
        reason = _policy_reason(denied)
        _step(session, operation, "failed_safe", now, failure_reason=reason)
        return RunResult(state="failed_safe", failure_reason=reason)

    _step(session, operation, "dispatching", now, tool=intent.tool)
    outcome = dispatcher.resolve(
        tool=intent.tool,
        model_args=cleaned,
        idempotency_key=operation.idempotency_key,
        # The device is told to skip its own lookup, which is the whole point of
        # the decision: the user has seen the duplicate and chosen to write
        # anyway. It travels as a Host-bound keyword, never as an argument --
        # a model-authored `skip_local_dedup` is refused by the input schema
        # before it can reach this line.
        skip_local_dedup=True,
    )
    return _apply_resolve(
        session,
        operation,
        outcome,
        dispatcher,
        keyring,
        now,
        action_keyring=action_keyring,
        intent=WriteIntent(tool=intent.tool, model_args=cleaned),
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
    prior_clarification_question: str | None = None,
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
        if _same_clarification_question(
            outcome.question, prior_clarification_question
        ):
            # Finance has proved zero writes, but parking the exact same
            # question after the user answered it creates an unbounded loop.
            # It is an honest safe failure, never a third parked operation.
            reason = ErrorCode.CLARIFICATION_REPEATED.value
            _step(session, operation, "failed_safe", now, failure_reason=reason)
            return RunResult(state="failed_safe", failure_reason=reason)
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


def _same_clarification_question(current: str, previous: str | None) -> bool:
    """Compare a Finance question without turning wording noise into a loop."""
    if previous is None:
        return False
    normalize = lambda value: re.sub(r"[\s，,。.!！？?、]", "", value).casefold()
    return bool(normalize(current)) and normalize(current) == normalize(previous)


def _is_override_operation(operation: Operation) -> bool:
    return (
        operation.duplicate_check_id is not None
        and operation.api_request.encrypted_request_payload is not None
    )


def _is_calendar_override(operation: Operation) -> bool:
    """Whether this operation re-issues a device write the user overrode.

    `parent_operation_id` is set only by the override endpoint, and the seal
    beside it only by that same write, so both halves are Host-written facts --
    nothing a model or a client can assert. Requiring both is what keeps this
    from being a generic "someone said so" flag: an operation with a parent but
    no retained request has nothing to resume and must not be run as one.
    """
    return (
        operation.parent_operation_id is not None
        and operation.encrypted_request is not None
    )


def _is_pre_resolved(operation: Operation, *, declared: bool) -> bool:
    """Whether this operation already knows its write and must skip the model.

    Three shapes qualify, and they are kept distinguishable on purpose:

    - a `write anyway` override, recognised by its `duplicate_check_id`. The id
      is both the marker and the authorisation, so inferring it is safe;
    - a calendar 「仍要创建」 override (design 3.3), recognised by its parent
      lineage and the request retained beside it -- both written by the
      endpoint, neither expressible by a model;
    - a caller that *declares* the operation pre-resolved, which is how the
      category-correction route arrives. It carries no duplicate check and there
      is nothing on the row to infer from, so the caller states it and the
      sealed intent still has to be there.

    The `declared` route deliberately does not widen the others: an operation is
    not treated as an override just because someone said "pre-resolved", so it
    cannot acquire override authority it was never granted.
    """
    if _is_override_operation(operation) or _is_calendar_override(operation):
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
    encrypted_device_action: dict[str, Any] | None = None,
    encrypted_request: dict[str, Any] | None = None,
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
        encrypted_device_action=encrypted_device_action,
        encrypted_request=encrypted_request,
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
