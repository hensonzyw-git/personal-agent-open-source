"""Adapt a model proposal to the orchestrator's interpretation seam.

`DEV-027`. The orchestrator's `Interpreter` protocol preserves direct answers,
one tool call, structured clarification and fixed fail-closed outcomes. This
binds a `ModelGateway` into that protocol.

Since `CAP-001` it carries no per-request state at all: the system instruction
and the device's effective tool declarations arrive inside the turn's
`ContextEnvelope`, already measured against the budget. An interpreter that also
held its own catalog would be a second, unmeasured source of the same facts.

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
from personal_agent.context.builder import ContextEnvelope
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
    """A `ModelGateway`-backed interpreter over one assembled turn."""

    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    def interpret(
        self,
        *,
        envelope: ContextEnvelope,
        clarification_context: ClarificationContext | None = None,
    ) -> Interpretation:
        try:
            kwargs: dict[str, object] = {"envelope": envelope}
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
