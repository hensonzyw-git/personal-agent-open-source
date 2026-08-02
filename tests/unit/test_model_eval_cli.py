"""DEV-037: the v0.2 dataset has a production-boundary evaluator."""

from __future__ import annotations

from personal_agent.api.orchestrator import (
    Clarification,
    DirectAnswer,
    FailSafeInterpretation,
    ToolCall,
)
from personal_agent.eval_cli import envelope_factory, score_interpretation, visible_tools
from personal_agent_core.evalset import domain_of, load_cases


CASES = {case.id: case for case in load_cases()}


def test_enabled_manifest_tools_are_the_only_eval_catalog() -> None:
    names = {tool.alias for tool in visible_tools()}
    assert "finance.log_expense" in names
    assert "meta.capabilities" in names
    assert "finance.log_expense_batch" not in names


def test_model_scoring_uses_real_outcomes_not_case_counts() -> None:
    case = CASES["EXP-019"]
    exact = ToolCall(tool=case.expected.tool or "", model_args=case.expected.arguments)
    wrong = ToolCall(
        tool=case.expected.tool or "",
        model_args={**case.expected.arguments, "category": "购物"},
    )

    passed = score_interpretation(case, exact, evaluator="test")
    failed = score_interpretation(case, wrong, evaluator="test")

    assert passed.passed and passed.safety_pass
    assert not failed.passed and not failed.safety_pass
    assert any("category" in problem for problem in failed.problems)


def test_clarification_and_structured_rejection_are_mechanically_scored() -> None:
    clarification = score_interpretation(
        CASES["ASK-001"], Clarification("个人还是家庭？"), evaluator="test"
    )
    batch = score_interpretation(
        CASES["BAT-001"],
        FailSafeInterpretation("BATCH_ATOMICITY_UNAVAILABLE"),
        evaluator="test",
    )

    assert clarification.passed and clarification.safety_pass
    assert batch.passed and batch.safety_pass


def test_free_prose_is_never_counted_as_a_verified_rejection() -> None:
    outcome = score_interpretation(
        CASES["GRD-006"], DirectAnswer("我不能访问资产。"), evaluator="test"
    )

    assert not outcome.passed
    assert outcome.safety_pass
    assert outcome.observed_action == "direct_answer"


def test_mcp_domain_is_populated_and_builds_a_real_envelope() -> None:
    case = CASES["MCP-001"]
    tools = visible_tools()

    assert domain_of(case) == "mcp"
    with envelope_factory(tools) as assemble:
        envelope = assemble(case)

    assert envelope.user_text == case.input
    assert "meta.capabilities" in "\n".join(
        component.text for component in envelope.components
    )
