"""The model gateway: a framework-neutral seam for one model turn.

`DEV-027`. Per `CLAUDE.md` §5 the flow is `Agent -> tool intent -> policy -> MCP`:
the model proposes, it never executes. This module is the thin, replaceable
adapter that turns one user message into exactly one proposal: a direct answer,
one tool call, a structured clarification, or a fixed fail-closed outcome. The
GLM/ADK implementation lives behind this protocol so the orchestrator's safety
machinery never depends on the model SDK, and a proposal is only ever advisory:
policy, idempotency, and verification are enforced downstream regardless of what
the model returns.

Deliberately absent here: any execution, any Host-injected field, any authority
over success. A hallucinated tool name is passed through unchanged for policy to
reject, rather than being silently repaired, so enforcement stays in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from personal_agent.context.builder import ContextEnvelope
from personal_agent_core.errors import ModelFailureReason


class ModelGatewayError(RuntimeError):
    """The model turn could not be obtained or understood.

    Part of the gateway contract: `propose` raises this for a transport failure,
    a malformed response, or a tool call whose arguments are not a JSON object.
    The interpreter translates it into the orchestrator's `InterpreterError`, so a
    bad model turn becomes a safe failure and never a write.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: ModelFailureReason = ModelFailureReason.UNAVAILABLE,
        provider_status: int | None = None,
        provider_code: str | None = None,
        provider_request_id: str | None = None,
        exception_type: str | None = None,
        response_shape: str | None = None,
    ) -> None:
        self.reason = reason
        self.provider_status = provider_status
        self.provider_code = provider_code
        self.provider_request_id = provider_request_id
        self.exception_type = exception_type
        self.response_shape = response_shape
        super().__init__(message)


@dataclass(frozen=True)
class ProposedAnswer:
    """The model chose to reply in words, with no tool call."""

    text: str


@dataclass(frozen=True)
class ProposedToolCall:
    """The model proposed exactly one tool call with raw model arguments.

    `arguments` are whatever the model produced; they are validated by the tool's
    JSON Schema and stripped of any Host-only fields at the policy boundary, never
    here.
    """

    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ProposedClarification:
    """A structured, side-effect-free question that parks the operation."""

    question: str


@dataclass(frozen=True)
class ProposedFailure:
    """A structured fail-closed outcome selected for a frozen safety gate."""

    reason: str


ModelProposal = (
    ProposedAnswer | ProposedToolCall | ProposedClarification | ProposedFailure
)


class ModelGateway(Protocol):
    """One model turn, built from exactly one budget-validated `ContextEnvelope`.

    `CAP-001` design §9 makes the Context Builder the only assembly point: the
    system instruction, the history, the exact pending state and the tool
    declarations all arrive already rendered and already measured, and the
    gateway may not add context of its own. Anything it appends unmeasured (the
    two internal, side-effect-free declarations below) lives inside
    `CONTEXT_RESERVED_TOOL_TOKENS`, which is reserved outside the hard limit.

    The implementation must return exactly one proposal and must not execute a
    tool. A response containing multiple tool calls is malformed and fails
    closed; it is never truncated to the first call.
    """

    def propose(
        self,
        *,
        envelope: ContextEnvelope,
    ) -> ModelProposal: ...
