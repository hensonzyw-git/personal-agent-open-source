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

from personal_agent.policy.bridge import VisibleTool


class ModelGatewayError(RuntimeError):
    """The model turn could not be obtained or understood.

    Part of the gateway contract: `propose` raises this for a transport failure,
    a malformed response, or a tool call whose arguments are not a JSON object.
    The interpreter translates it into the orchestrator's `InterpreterError`, so a
    bad model turn becomes a safe failure and never a write.
    """


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


@dataclass(frozen=True)
class ClarificationContext:
    """Only the previous unresolved turn, never the conversation archive."""

    original_user_text: str
    question: str


ModelProposal = (
    ProposedAnswer | ProposedToolCall | ProposedClarification | ProposedFailure
)


class ModelGateway(Protocol):
    """One model turn: a system instruction, the user text, and the visible tools.

    The implementation must return exactly one proposal and must not execute a
    tool. A response containing multiple tool calls is malformed and fails
    closed; it is never truncated to the first call.
    """

    def propose(
        self,
        *,
        system: str,
        user_text: str,
        tools: list[VisibleTool],
        clarification: ClarificationContext | None = None,
    ) -> ModelProposal: ...


def tool_declarations(tools: list[VisibleTool]) -> list[dict[str, Any]]:
    """Translate the governed catalog into OpenAI-style function declarations.

    The description and schema come from the trusted manifest (via `VisibleTool`),
    never from a connector's own metadata, so a server cannot smuggle instructions
    to the model through a tool description.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.alias,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]
