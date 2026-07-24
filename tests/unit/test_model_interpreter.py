"""DEV-027 (offline): the model gateway seam and its interpreter adapter.

No GLM and no key. A fake gateway returns canned proposals, and the test proves
the adapter maps them faithfully to the orchestrator's interpretation type and
never corrects a bad tool name (policy owns that).
"""

from __future__ import annotations

from personal_agent.api.orchestrator import (
    Clarification,
    DirectAnswer,
    FailSafeInterpretation,
    ToolCall,
)
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.interpreter import ModelInterpreter
from personal_agent.runtime.model_gateway import (
    ClarificationContext,
    ProposedAnswer,
    ProposedClarification,
    ProposedFailure,
    ProposedToolCall,
    tool_declarations,
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

    def propose(self, *, system, user_text, tools, clarification=None):
        self.seen = {
            "system": system,
            "user_text": user_text,
            "tools": tools,
            "clarification": clarification,
        }
        return self.proposal


def _interpreter(proposal, *, tools=(_EXPENSE,), system="SYS") -> ModelInterpreter:
    return ModelInterpreter(
        FakeGateway(proposal), tools=list(tools), system=system
    )


def test_a_proposed_answer_becomes_a_direct_answer() -> None:
    interp = _interpreter(ProposedAnswer("你好"))
    result = interp.interpret(text="午饭 45", conversation_id="c1")
    assert isinstance(result, DirectAnswer)
    assert result.text == "你好"


def test_structured_non_write_proposals_keep_their_state_meaning() -> None:
    clarification = _interpreter(ProposedClarification("个人还是家庭支出？"))
    assert clarification.interpret(
        text="午饭 45", conversation_id="c1"
    ) == Clarification("个人还是家庭支出？")

    failure = _interpreter(ProposedFailure("BATCH_ATOMICITY_UNAVAILABLE"))
    assert failure.interpret(
        text="午饭 45，晚饭 60", conversation_id="c1"
    ) == FailSafeInterpretation("BATCH_ATOMICITY_UNAVAILABLE")


def test_a_proposed_tool_call_becomes_a_tool_call_with_copied_args() -> None:
    args = {"name": "午饭", "input_amount": "45"}
    interp = _interpreter(ProposedToolCall("finance.log_expense", args))
    result = interp.interpret(text="午饭 45 个人", conversation_id="c1")
    assert isinstance(result, ToolCall)
    assert result.tool == "finance.log_expense"
    assert result.model_args == args
    # The args are copied, not aliased to the model's dict.
    assert result.model_args is not args


def test_an_off_catalog_tool_is_passed_through_for_policy_to_reject() -> None:
    # The interpreter does not repair or drop a tool the device cannot see; the
    # orchestrator's authorize step turns this into a safe policy denial.
    interp = _interpreter(ProposedToolCall("finance.delete_everything", {}))
    result = interp.interpret(text="删掉所有记录", conversation_id="c1")
    assert isinstance(result, ToolCall)
    assert result.tool == "finance.delete_everything"


def test_the_gateway_receives_the_bound_system_and_tools() -> None:
    gateway = FakeGateway(ProposedAnswer("ok"))
    interp = ModelInterpreter(gateway, tools=[_EXPENSE], system="RULES")
    interp.interpret(text="hi", conversation_id="c1")
    assert gateway.seen["system"] == "RULES"
    assert gateway.seen["user_text"] == "hi"
    assert gateway.seen["tools"] == [_EXPENSE]


def test_the_gateway_receives_only_an_explicit_clarification_context() -> None:
    gateway = FakeGateway(ProposedAnswer("ok"))
    interp = ModelInterpreter(gateway, tools=[_EXPENSE], system="RULES")
    context = ClarificationContext("午饭 45", "个人还是家庭？")
    interp.interpret(
        text="个人支出",
        conversation_id="c1",
        clarification_context=context,
    )
    assert gateway.seen["clarification"] == context


def test_tool_declarations_use_the_trusted_manifest_shape() -> None:
    decls = tool_declarations([_EXPENSE])
    assert decls == [
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
