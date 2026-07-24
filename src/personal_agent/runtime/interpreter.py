"""Adapt a model proposal to the orchestrator's interpretation seam.

`DEV-027`. The orchestrator's `Interpreter` protocol preserves direct answers,
one tool call, structured clarification and fixed fail-closed outcomes. This
binds a `ModelGateway`, the device's visible tools, and a system instruction into
that protocol. It is deliberately per-request: the tool catalog is the
*device's* effective tools (design 5.3), so a fresh interpreter is built for
each request with that device's catalog.

The mapping is intentionally faithful, not corrective. A proposed tool call is
handed on as-is -- even a tool the device cannot see -- because rejecting it is
policy's job (`GovernedToolBridge.authorize`), and doing it here as well would
split enforcement across two places and let one drift from the other.
"""

from __future__ import annotations

from personal_agent.api.orchestrator import (
    Clarification,
    DirectAnswer,
    FailSafeInterpretation,
    Interpretation,
    InterpreterError,
    ToolCall,
)
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.model_gateway import (
    ClarificationContext,
    ModelGateway,
    ModelGatewayError,
    ProposedAnswer,
    ProposedClarification,
    ProposedFailure,
    ProposedToolCall,
)


class ModelInterpreter:
    """A `ModelGateway`-backed interpreter for one device's request."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        tools: list[VisibleTool],
        system: str,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._system = system

    def interpret(
        self,
        *,
        text: str,
        conversation_id: str,
        clarification_context: ClarificationContext | None = None,
    ) -> Interpretation:
        try:
            kwargs = {
                "system": self._system,
                "user_text": text,
                "tools": self._tools,
            }
            if clarification_context is not None:
                kwargs["clarification"] = clarification_context
            proposal = self._gateway.propose(**kwargs)
        except ModelGatewayError as exc:
            # Translate the gateway's failure into the seam's neutral error, so
            # the orchestrator stays decoupled from any model SDK.
            raise InterpreterError(str(exc)) from exc
        if isinstance(proposal, ProposedToolCall):
            # Passed through unchanged; policy decides whether it may run.
            return ToolCall(tool=proposal.tool, model_args=dict(proposal.arguments))
        if isinstance(proposal, ProposedClarification):
            return Clarification(question=proposal.question)
        if isinstance(proposal, ProposedFailure):
            return FailSafeInterpretation(reason=proposal.reason)
        if isinstance(proposal, ProposedAnswer):
            return DirectAnswer(text=proposal.text)
        raise TypeError(  # pragma: no cover - the union is exhaustive above
            f"unknown model proposal {type(proposal).__name__}"
        )
