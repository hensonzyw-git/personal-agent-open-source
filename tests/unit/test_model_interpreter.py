"""DEV-027 (offline): the model gateway seam and its interpreter adapter.

No GLM and no key. A fake gateway returns canned proposals, and the test proves
the adapter maps them faithfully to the orchestrator's interpretation type and
never corrects a bad tool name (policy owns that).
"""

from __future__ import annotations

import json

import pytest

from context_envelopes import envelope_for
from personal_agent.api.orchestrator import (
    Clarification,
    DirectAnswer,
    FailSafeInterpretation,
    ToolCall,
)
from personal_agent.context.budget import ComponentKind
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.interpreter import ModelInterpreter
from personal_agent.runtime.model_gateway import (
    ProposedAnswer,
    ProposedClarification,
    ProposedFailure,
    ProposedToolCall,
)


_EXPENSE = VisibleTool(
    alias="finance.log_expense",
    description="记一笔支出",
    input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
    risk_level="R2",
    required_scopes=("finance.write",),
)


class FakeGateway:
    def __init__(self, proposal) -> None:
        self.proposal = proposal
        self.seen: dict | None = None

    def propose(self, *, envelope):
        self.seen = {"envelope": envelope}
        return self.proposal


@pytest.fixture()
def envelope(tmp_path):
    return envelope_for(tmp_path, system="SYS", tools=[_EXPENSE])


def _interpreter(proposal) -> ModelInterpreter:
    return ModelInterpreter(FakeGateway(proposal))


def test_a_proposed_answer_becomes_a_direct_answer(envelope) -> None:
    interp = _interpreter(ProposedAnswer("你好"))
    result = interp.interpret(envelope=envelope)
    assert isinstance(result, DirectAnswer)
    assert result.text == "你好"


def test_structured_non_write_proposals_keep_their_state_meaning(
    envelope,
) -> None:
    clarification = _interpreter(ProposedClarification("个人还是家庭支出？"))
    assert clarification.interpret(envelope=envelope) == Clarification(
        "个人还是家庭支出？"
    )

    failure = _interpreter(ProposedFailure("BATCH_ATOMICITY_UNAVAILABLE"))
    assert failure.interpret(envelope=envelope) == FailSafeInterpretation(
        "BATCH_ATOMICITY_UNAVAILABLE"
    )


def test_a_proposed_tool_call_becomes_a_tool_call_with_copied_args(
    envelope,
) -> None:
    args = {"name": "午饭", "input_amount": "45"}
    interp = _interpreter(ProposedToolCall("finance.log_expense", args))
    result = interp.interpret(envelope=envelope)
    assert isinstance(result, ToolCall)
    assert result.tool == "finance.log_expense"
    assert result.model_args == args
    # The args are copied, not aliased to the model's dict.
    assert result.model_args is not args


def test_an_off_catalog_tool_is_passed_through_for_policy_to_reject(
    envelope,
) -> None:
    # The interpreter does not repair or drop a tool the device cannot see; the
    # orchestrator's authorize step turns this into a safe policy denial.
    interp = _interpreter(ProposedToolCall("finance.delete_everything", {}))
    result = interp.interpret(envelope=envelope)
    assert isinstance(result, ToolCall)
    assert result.tool == "finance.delete_everything"


def test_the_gateway_receives_the_assembled_envelope(tmp_path) -> None:
    """The instruction and the catalog reach the model only through the envelope.

    The interpreter holds neither any more: a second copy of the system prompt
    or of the tool list would be an input nobody measured against the budget.
    """
    gateway = FakeGateway(ProposedAnswer("ok"))
    built = envelope_for(tmp_path, system="RULES", tools=[_EXPENSE], user_text="hi")
    ModelInterpreter(gateway).interpret(envelope=built)
    assert gateway.seen["envelope"] is built
    assert built.system_instruction == "RULES"
    assert built.user_text == "hi"
    assert built.tool_aliases == ("finance.log_expense",)


def test_tool_declarations_use_the_trusted_manifest_shape(tmp_path) -> None:
    """One place shapes a declaration, and it is the measured one.

    The description and schema come from the trusted manifest through
    `VisibleTool`, so a connector cannot smuggle instructions to the model
    through its own metadata.
    """
    built = envelope_for(tmp_path, tools=[_EXPENSE])
    assert [json.loads(text) for text in built.texts_of(
        ComponentKind.TOOL_DECLARATION
    )] == [
        {
            "type": "function",
            "function": {
                "name": "finance.log_expense",
                "description": "记一笔支出",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
            },
        }
    ]
