"""The real Agent-to-Finance dispatcher, over the MCP protocol.

Henson chose the real protocol over an in-process call (2026-07-24), so the
governed path `Agent -> policy -> MCP client -> connector` and Finance MCP's own
second authorisation gate stay real instead of being bypassed by a Python import.

That choice fixes the shape of this module. `finance.log_expense` is a *single*
MCP call that resolves, duplicate-checks, writes and verifies, so the two-phase
`resolve` / `commit` seam the orchestrator needs cannot be two MCP calls:

- `resolve` therefore performs **no** write-tool call at all. It packages the
  authorised arguments into a `WriteIntent` and returns immediately, which is
  exactly the honest thing to report: nothing has been sent, so a cancel here is
  genuinely pre-submit. A *read* tool is different -- it has no commit phase, so
  it is executed here and returns `ReadCompleted`.
- `commit` makes the one call, and maps its outcome onto the orchestrator's
  vocabulary. Two of those outcomes are parking edges backed by evidence rather
  than assumption: Finance performs a duplicate check and a resolver
  clarification with **zero writes and no execution row**, so the operation may
  park even though it had already entered `source_in_progress`.

Where the contract has gaps, this module fails closed rather than inventing:

- a `POSSIBLE_DUPLICATE` whose pending check cannot be read from the control
  plane becomes a *safe failure*, not a silent success and not a decision the
  user cannot answer. The write did not happen either way;
- a timeout and a transport error are `CommitUnknown`: a write whose response was
  lost is never reported as failed;
- for every other failure, whether anything was written is **read from Finance**
  (`_classify_by_execution`) rather than inferred from the error code. Claiming
  "no external record exists" is the strongest thing this module can say, and it
  is said only on the evidence of Finance's own execution row -- which is
  committed before any network call, so its absence is proof. An unreachable
  control plane or an unrecognised state is `CommitUnknown`;
- a clarification may carry one Finance-selected value from the closed
  `ClarificationQuestion` enum. The exact question persists with the pending
  operation; arbitrary resolver detail, ledger candidate names and connector
  diagnostics remain outside the MCP error envelope. An older or malformed
  connector response keeps the generic stable-code message.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from personal_agent.api.calendar_query_projection import (
    CalendarQueryProjectionError,
    canonical_calendar_projection_json,
    decode_calendar_query_projection,
    summarise_calendar_projection,
)
from personal_agent.api.control_client import (
    ControlPlaneError,
    FinanceControlClient,
)
from personal_agent.api.finance_query_projection import (
    QUERY_RESULT_UNREADABLE,
    FinanceQueryProjectionError,
    canonical_projection_json,
    decode_finance_query_projection,
    summarise_query_projection,
)
from personal_agent.api.finance_record_projection import (
    FinanceExpenseRecord,
    FinanceRecordProjectionError,
    decode_finance_expense_record,
)
from personal_agent.api.intent import WriteIntent
from personal_agent.api.orchestrator import (
    CommitClarificationZeroWrite,
    CommitDuplicateZeroWrite,
    CommitFailedSafe,
    CommitOutcome,
    CommitUnknown,
    DeviceActionIssued,
    ReadCompleted,
    Resolved,
    ResolveFailedSafe,
    ResolveOutcome,
    Written,
)
from personal_agent.api.recovery import (
    FinanceExecutionStatus,
    proves_zero_write,
)
from personal_agent.mcp_client.core import McpTimeoutError, McpTransportError
from personal_agent.policy.bridge import (
    BridgeCallContext,
    DeviceAuthorization,
    GovernedToolBridge,
)
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.finance_tools import FINANCE_READ_TOOLS
from personal_agent_core.host_context import HostContext, ServiceKeyRing
from personal_agent_core.manifest import canonical_json
from personal_agent_core.tool_ir import TOOL_CONTRACTS


_log = logging.getLogger(__name__)


#: Tools with no side effect, which `resolve` may therefore execute outright.
#: Derived from the IR's effect field, never hand-listed (review R4): the
#: hand-listed set held exactly the Finance reads and `meta.capabilities`, so
#: `calendar.query_events` — a governed read in the IR — fell through to the
#: write branch and parked in the commit flow awaiting a record id that a read
#: will never produce.
READ_TOOLS: frozenset[str] = frozenset(
    contract.name
    for contract in TOOL_CONTRACTS
    if contract.effect == "read" and contract.enabled
)

#: The governed read tools whose `trusted_result` is the *expense* query
#: projection rather than a prose answer. Derived from the IR so a second
#: governed read tool cannot silently bypass a strict decoder and have its
#: canonical JSON echoed as `answer` -- the exact bug this set exists to fix.
#: Narrowed to the tools whose output contract is the expense query projection
#: (identified by its `metric` const): `meta.capabilities` is a read but not a
#: query, and must keep the plain `answer` path.
_QUERY_RESULT_TOOLS: frozenset[str] = frozenset(
    contract.name
    for contract in TOOL_CONTRACTS
    if contract.effect == "read"
    and contract.enabled
    and contract.output_schema.get("properties", {}).get("metric", {}).get("const")
    == "personal_spend_total_cny"
)

#: The same idea for the calendar mirror read (review R4): a governed read
#: whose result is a structured projection — here keyed on the output
#: contract's `source_system` const, exactly the way the Finance set keys on
#: `metric`. A read in neither set keeps the plain `answer` path.
_CALENDAR_QUERY_RESULT_TOOLS: frozenset[str] = frozenset(
    contract.name
    for contract in TOOL_CONTRACTS
    if contract.effect == "read"
    and contract.enabled
    and contract.output_schema.get("properties", {}).get("source_system", {}).get(
        "const"
    )
    == "apple_calendar_mirror"
)


#: Whether a tool's executor is the user's device, derived from the IR. A
#: device-executed write never crosses the MCP bridge; the dispatcher
#: authorises it and issues a device action instead. Derived, never
#: hand-listed: a second device tool ships into the fork automatically, and
#: flipping `calendar.create_event` back to `mcp` leaves the fork empty —
#: which the orchestrator's exhaustiveness assertion then surfaces.
def _is_device_executed(remote_name: str) -> bool:
    return any(
        contract.name == remote_name and contract.executor == "device"
        for contract in TOOL_CONTRACTS
    )



#: Codes whose contract meaning already is "this may have reached the ledger".
#: They are Finance's own assertion, so they resolve to `CommitUnknown` without
#: a control-plane read -- both because the read cannot make the answer more
#: certain, and because it could only ever weaken it.
_ASSERTS_MAY_HAVE_WRITTEN: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.SOURCE_COMMIT_UNKNOWN,
        ErrorCode.SOURCE_COMMITTED_MISMATCH,
        ErrorCode.SOURCE_TIMEOUT_UNKNOWN,
        ErrorCode.BATCH_COMMIT_UNKNOWN,
    }
)


def tool_call_fingerprint(tool: str, model_args: dict[str, Any]) -> str:
    """Bind an idempotency key to this exact tool call.

    Computed from the model-facing arguments only, so a replay of the same
    request produces the same fingerprint even if the ledger moved underneath
    it. A fingerprint derived from the *resolved* entry would make a legitimate
    retry look like a different request and be refused as a conflict.
    """
    return hashlib.sha256(
        canonical_json({"tool": tool, "model_args": model_args}).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class DispatcherContext:
    """The per-operation identity a dispatch is made under."""

    device: DeviceAuthorization
    user_id: str
    agent_id: str
    conversation_trace_id: str
    timezone: str = "Asia/Shanghai"


class McpFinanceDispatcher:
    """The orchestrator's `Dispatcher`, backed by real MCP calls."""

    def __init__(
        self,
        *,
        bridge: GovernedToolBridge,
        control: FinanceControlClient,
        signing_ring: ServiceKeyRing,
        context: DispatcherContext,
        run: Callable[[Any], Any] = asyncio.run,
        new_request_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._bridge = bridge
        self._control = control
        self._ring = signing_ring
        self._context = context
        # The orchestrator runs on a worker thread with no event loop, so each
        # dispatch drives its own. Injected so tests can supply their own runner.
        self._run = run
        self._new_request_id = new_request_id

    # --- phase 1: resolve ----------------------------------------------------

    def _remote_name(self, alias: str) -> str:
        """The connector's own tool name behind a model-visible alias.

        The Host Context binds the *remote* name -- the bridge refuses a context
        whose tool is the alias -- so the two must not be conflated even while
        they happen to be equal for the single local Finance connector.
        """
        try:
            return self._bridge.registry.resolve(alias).remote_name
        except (KeyError, RuntimeError) as exc:
            raise AppError(
                ErrorCode.TOOL_NOT_ALLOWLISTED,
                internal_detail=f"{alias} resolves to no single connector tool",
            ) from exc

    def resolve(
        self,
        *,
        tool: str,
        model_args: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> ResolveOutcome:
        try:
            remote = self._remote_name(tool)
        except AppError as error:
            return ResolveFailedSafe(reason=_reason(error))
        if _is_device_executed(remote):
            # Device-executed write: authorise exactly like any governed
            # write (scope, allowlist, write switch, schema — the bridge
            # refuses before anything can be issued), then stop. No MCP call
            # exists for this tool; the executor is the phone, reached by the
            # chat response itself. The action id *is* the operation's
            # idempotency key, so one message can produce at most one device
            # side effect.
            try:
                # The cleaned arguments are the *attested* ones: host-only
                # fields the model tried to smuggle in are gone, and a field
                # this contract declares survives. The action is the
                # authorisation record, so what it carries must be what was
                # attested, never the model's raw output.
                _, attested = self._bridge.authorize(
                    tool, model_args, self._context.device
                )
            except AppError as error:
                # Nothing was issued, so this is provably zero-write.
                return ResolveFailedSafe(reason=_reason(error))
            if idempotency_key is None:
                return ResolveFailedSafe(
                    reason="device action requires the operation idempotency key"
                )
            return DeviceActionIssued(
                action_id=idempotency_key,
                tool=tool,
                event_fields=attested,
            )
        if remote not in READ_TOOLS:
            # A write tool has exactly one MCP call and it belongs to `commit`.
            # Returning here means nothing has been sent yet, which is what lets
            # the operation stay honestly cancellable until the commit step.
            return Resolved(intent=WriteIntent(tool=tool, model_args=model_args))

        idempotency_key = str(uuid.uuid4())
        try:
            result = self._call(
                tool=tool,
                model_args=model_args,
                idempotency_key=idempotency_key,
                duplicate_override=None,
            )
        except (AppError, McpTimeoutError, McpTransportError) as error:
            # A read has no side effect, so every failure is a safe failure.
            return ResolveFailedSafe(reason=_reason(error))
        if tool in _QUERY_RESULT_TOOLS:
            # A query result is not a string to echo back. It is projected
            # through the strict whitelist; a result the projection cannot
            # read is a safe failure, never a JSON dump shown as an answer.
            try:
                projection = decode_finance_query_projection(result)
            except FinanceQueryProjectionError:
                return ResolveFailedSafe(reason=QUERY_RESULT_UNREADABLE)
            return ReadCompleted(
                result=canonical_projection_json(projection),
                projection=projection,
                answer=summarise_query_projection(projection),
            )
        if tool in _CALENDAR_QUERY_RESULT_TOOLS:
            # Same discipline, calendar shape: the mirror read's result is a
            # structured projection the calendar decoder whitelists, never a
            # string the model may restate as its own answer.
            try:
                calendar_projection = decode_calendar_query_projection(result)
            except CalendarQueryProjectionError:
                return ResolveFailedSafe(reason=QUERY_RESULT_UNREADABLE)
            return ReadCompleted(
                result=canonical_calendar_projection_json(calendar_projection),
                projection=calendar_projection,
                answer=summarise_calendar_projection(calendar_projection),
            )
        return ReadCompleted(result=canonical_json(result))

    # --- phase 2: commit -----------------------------------------------------

    def commit(
        self,
        *,
        intent: WriteIntent,
        idempotency_key: str,
        duplicate_override: str | None,
    ) -> CommitOutcome:
        if _is_device_executed(intent.tool):
            # The two-phase protocol has no phase 2 for a device tool: the
            # write was issued in `resolve` and the phone reports back through
            # its own endpoint. Reaching `commit` means the dispatch fork
            # failed to intercept, and failing open here would fabricate an
            # MCP call the contract says does not exist.
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=(
                    f"{intent.tool} is device-executed; commit must never run"
                ),
            )
        dispatched: list[bool] = []
        try:
            result = self._call(
                tool=intent.tool,
                model_args=intent.model_args,
                idempotency_key=idempotency_key,
                duplicate_override=duplicate_override,
                dispatched=dispatched,
            )
        except AppError as error:
            if not dispatched:
                # Refused on this side -- scope, allowlist, an unresolvable
                # alias -- before anything could be sent. Nothing was written,
                # and that is knowable without Finance. Asking anyway would make
                # a permission error unreportable exactly when Finance is down,
                # turning "you do not have permission" into "unknown, needs
                # manual review".
                return CommitFailedSafe(reason=error.code.value)
            return self._commit_error(error, idempotency_key=idempotency_key)
        except McpTimeoutError:
            # The request left. Whether it landed is unknown by definition.
            return CommitUnknown(reason="source_commit_unknown")
        except McpTransportError:
            return CommitUnknown(reason="source_commit_unknown")

        record_id = result.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            # The output schema requires one, so this is a contract violation
            # rather than a business outcome -- and it may still have written.
            return CommitUnknown(reason="missing_verified_record_id")
        return Written(
            record_id=record_id, record=self._projected_record(result)
        )

    @staticmethod
    def _projected_record(result: dict[str, Any]) -> FinanceExpenseRecord | None:
        """The written row for the `G1` receipt card, or nothing.

        This is where the fields used to be thrown away. They are let through
        the strict projection only, for the same reason a query result is: what
        the connector may return and what a card may render are two different
        contracts, and the narrower one has to fail closed.

        The failure is deliberately asymmetric with the query path. An
        unprojectable *query* result is a safe failure, because there the result
        **is** the answer -- there is nothing else to show. Here the write is
        already proven by its `record_id`, so an unreadable record costs the
        card its rows and nothing more. Turning a committed ledger write into a
        failure because its presentation payload was malformed would be strictly
        worse than a plain receipt, and it would do it *after* the money moved.
        """
        try:
            return decode_finance_expense_record(result.get("record"))
        except FinanceRecordProjectionError:
            _log.warning(
                "write receipt record failed projection; "
                "the card falls back to the status row"
            )
            return None

    def _commit_error(
        self, error: AppError, *, idempotency_key: str
    ) -> CommitOutcome:
        if error.code is ErrorCode.POSSIBLE_DUPLICATE:
            # Proven zero writes: Finance raises this before any execution row
            # exists. The id itself is not on this channel; it is read from the
            # Host-to-Host control plane, keyed by the idempotency key.
            try:
                pending = self._run(
                    self._control.get_pending_duplicate_check(idempotency_key)
                )
            except ControlPlaneError:
                return CommitFailedSafe(reason="duplicate_check_unavailable")
            if pending is None:
                # Refused as a duplicate, but nothing is pending a decision --
                # most likely it expired. There is no decision to offer, and
                # still nothing was written.
                return CommitFailedSafe(reason="duplicate_check_unavailable")
            return CommitDuplicateZeroWrite(
                duplicate_check_id=pending.duplicate_check_id,
                existing_summary=pending.existing_summary,
            )
        if error.code is ErrorCode.CLARIFICATION_REQUIRED:
            return CommitClarificationZeroWrite(
                question=(
                    error.clarification_question.value
                    if error.clarification_question is not None
                    else error.to_envelope().message
                )
            )
        if error.code in _ASSERTS_MAY_HAVE_WRITTEN:
            # Finance has already stated the outcome may have reached the source.
            # Its own claim is authoritative and stronger than anything the
            # execution row could add, so this must not be re-derived: a row that
            # had been cleaned up would otherwise downgrade an explicit "unknown"
            # into a proven zero write.
            return CommitUnknown(reason=error.code.value)
        return self._classify_by_execution(
            reason=error.code.value, idempotency_key=idempotency_key
        )

    def _classify_by_execution(
        self, *, reason: str, idempotency_key: str
    ) -> CommitOutcome:
        """Ask Finance whether anything was written; never infer it from a code.

        An error code is a *category*; whether the one create left the process is
        a *fact*, and Finance holds it in the execution row. Inferring the fact
        from the category made the strongest claim this system can make -- "no
        external record exists" -- depend on a distant module never letting
        certain codes escape from after the create. `INTERNAL_ERROR` is the code
        Finance emits for any failure it could not name, so it is reachable from
        exactly there: storage exhaustion at the receipt commit leaves the record
        in Feishu and puts `INTERNAL_ERROR` on the wire. Reporting that as a safe
        failure told the user nothing was written *and released the idempotency
        slot*, so the obvious retry could add a second ledger row.

        The read is the same control endpoint recovery already uses, keyed by the
        same idempotency key. It fails closed twice over: an unreachable control
        plane and an unrecognised execution state are both `CommitUnknown`, which
        parks the operation for reconciliation under the same client token rather
        than claiming anything.
        """
        try:
            execution = self._run(self._control.get_execution(idempotency_key))
        except ControlPlaneError:
            return CommitUnknown(reason=reason)

        if execution is None:
            status = None
        else:
            state = execution.get("state")
            if not isinstance(state, str):
                return CommitUnknown(reason=reason)
            status = FinanceExecutionStatus(
                state=state, record_id=None, receipt_verified=False
            )

        if proves_zero_write(status):
            return CommitFailedSafe(reason=reason)
        return CommitUnknown(reason=reason)

    # --- the one governed call ----------------------------------------------

    def _call(
        self,
        *,
        tool: str,
        model_args: dict[str, Any],
        idempotency_key: str,
        duplicate_override: str | None,
        dispatched: list[bool] | None = None,
    ) -> dict[str, Any]:
        """Make the one governed call.

        `dispatched` records whether anything was handed to the bridge for
        execution. `commit` needs that distinction: a refusal decided on this
        side is already proof of zero writes, and must not be routed through
        Finance for an opinion it does not need and may not be able to give.
        """
        context = self._context
        # Resolved and authorised here, before anything can leave. The bridge
        # authorises again inside `execute` -- deliberately, since authorisation
        # is recomputed rather than cached -- so this is an earlier check whose
        # *failure* is what carries the information, not a replacement for it.
        remote_name = self._remote_name(tool)
        self._bridge.authorize(tool, model_args, context.device)
        host = HostContext(
            agent_id=context.agent_id,
            device_id=context.device.device_id,
            user_id=context.user_id,
            scopes=tuple(sorted(context.device.scopes)),
            tool=remote_name,
            request_id=self._new_request_id(),
            trace_id=context.conversation_trace_id,
            idempotency_key=idempotency_key,
            request_fingerprint=tool_call_fingerprint(tool, model_args),
            allowed_tools_version=context.device.allowed_tools_version,
            timezone=context.timezone,
            duplicate_override=duplicate_override,
        )
        if dispatched is not None:
            dispatched.append(True)
        execution = self._run(
            self._bridge.execute(
                tool,
                model_args,
                context.device,
                call_context=BridgeCallContext(
                    host=host, signing_keys=self._ring
                ),
            )
        )
        return execution.trusted_result


def _reason(error: Exception) -> str:
    if isinstance(error, AppError):
        return error.code.value
    return "source_unavailable"
