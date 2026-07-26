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
- a timeout, a transport error or `SOURCE_COMMIT_UNKNOWN` is `CommitUnknown`.
  A write whose response was lost is never reported as failed;
- a clarification carries only the stable code's catalogue text, because
  `ErrorEnvelope` has nowhere to put the resolver's reason. The question is
  therefore generic today; making it specific needs a contract or control-plane
  change, and guessing the reason here would put words in Finance's mouth.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from personal_agent.api.control_client import (
    ControlPlaneError,
    FinanceControlClient,
)
from personal_agent.api.intent import WriteIntent
from personal_agent.api.orchestrator import (
    CommitClarificationZeroWrite,
    CommitDuplicateZeroWrite,
    CommitFailedSafe,
    CommitOutcome,
    CommitUnknown,
    ReadCompleted,
    Resolved,
    ResolveFailedSafe,
    ResolveOutcome,
    Written,
)
from personal_agent.mcp_client.core import McpTimeoutError, McpTransportError
from personal_agent.policy.bridge import (
    BridgeCallContext,
    DeviceAuthorization,
    GovernedToolBridge,
)
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import HostContext, ServiceKeyRing
from personal_agent_core.manifest import canonical_json


#: Tools with no side effect, which `resolve` may therefore execute outright.
READ_TOOLS: frozenset[str] = frozenset(
    {"finance.query_expenses", "meta.capabilities"}
)

#: Every commit failure that means "the write may have landed". A create is
#: never retried on the strength of one of these; the Finance reconciler
#: resolves the truth from the same client token.
_UNKNOWN_COMMIT_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.SOURCE_COMMIT_UNKNOWN, ErrorCode.SOURCE_COMMITTED_MISMATCH}
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
        self, *, tool: str, model_args: dict[str, Any]
    ) -> ResolveOutcome:
        try:
            remote = self._remote_name(tool)
        except AppError as error:
            return ResolveFailedSafe(reason=_reason(error))
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
        return ReadCompleted(result=canonical_json(result))

    # --- phase 2: commit -----------------------------------------------------

    def commit(
        self,
        *,
        intent: WriteIntent,
        idempotency_key: str,
        duplicate_override: str | None,
    ) -> CommitOutcome:
        try:
            result = self._call(
                tool=intent.tool,
                model_args=intent.model_args,
                idempotency_key=idempotency_key,
                duplicate_override=duplicate_override,
            )
        except AppError as error:
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
        return Written(record_id=record_id)

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
                question=error.to_envelope().message
            )
        if error.code in _UNKNOWN_COMMIT_CODES:
            return CommitUnknown(reason=error.code.value)
        return CommitFailedSafe(reason=error.code.value)

    # --- the one governed call ----------------------------------------------

    def _call(
        self,
        *,
        tool: str,
        model_args: dict[str, Any],
        idempotency_key: str,
        duplicate_override: str | None,
    ) -> dict[str, Any]:
        context = self._context
        host = HostContext(
            agent_id=context.agent_id,
            device_id=context.device.device_id,
            user_id=context.user_id,
            scopes=tuple(sorted(context.device.scopes)),
            tool=self._remote_name(tool),
            request_id=self._new_request_id(),
            trace_id=context.conversation_trace_id,
            idempotency_key=idempotency_key,
            request_fingerprint=tool_call_fingerprint(tool, model_args),
            allowed_tools_version=context.device.allowed_tools_version,
            timezone=context.timezone,
            duplicate_override=duplicate_override,
        )
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
