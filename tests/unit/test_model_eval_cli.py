"""DEV-037: the v0.2 dataset has a production-boundary evaluator."""

from __future__ import annotations

from personal_agent.api.orchestrator import (
    Clarification,
    DirectAnswer,
    FailSafeInterpretation,
    ToolCall,
)
from personal_agent.context.builder import ComponentKind
from personal_agent.eval_cli import (
    envelope_factory,
    score_interpretation,
    visible_tools,
)
from personal_agent_core.evalset import (
    EvalCase,
    EvalPriorTurn,
    ExpectedBehavior,
    domain_of,
    load_cases,
)

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


def test_model_scoring_applies_the_same_receipt_date_default_as_the_host() -> None:
    case = CASES["EXP-019"]
    omitted_date = ToolCall(
        tool=case.expected.tool or "",
        model_args={
            name: value
            for name, value in case.expected.arguments.items()
            if name != "occurred_on"
        },
    )

    outcome = score_interpretation(case, omitted_date, evaluator="test")

    assert outcome.passed and outcome.safety_pass


def test_model_scoring_never_defaults_an_omitted_explicit_date() -> None:
    case = CASES["EXP-004"]
    omitted_date = ToolCall(
        tool=case.expected.tool or "",
        model_args={
            name: value
            for name, value in case.expected.arguments.items()
            if name != "occurred_on"
        },
    )

    outcome = score_interpretation(case, omitted_date, evaluator="test")

    assert not outcome.passed
    assert "occurred_on: expected '2026-07-22', got None" in outcome.problems


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


def test_eval_replays_exact_clarification_context() -> None:
    case = EvalCase(
        id="ROB-TEST-CLARIFICATION",
        source_type="synthetic",
        reference_time="2026-08-07T15:00:00+08:00",
        input="家庭",
        prior_turns=(
            EvalPriorTurn(
                event_type="user_message",
                content={"text": "晚饭示例餐馆620"},
            ),
            EvalPriorTurn(
                event_type="operation_result",
                content={
                    "state": "waiting_for_clarification",
                    "clarification": "这笔是个人支出还是家庭支出？",
                },
            ),
        ),
        expected=ExpectedBehavior(
            action="call_tool",
            tool="finance.log_expense",
            arguments={},
        ),
    )

    with envelope_factory(visible_tools()) as assemble:
        envelope = assemble(case)

    exact = "\n".join(envelope.texts_of(ComponentKind.CLARIFICATION_CONTEXT))
    raw = "\n".join(envelope.texts_of(ComponentKind.RAW_EVENT))
    assert "晚饭示例餐馆620" in exact
    assert "这笔是个人支出还是家庭支出？" in exact
    assert "晚饭示例餐馆620" not in raw
    assert envelope.texts_of(ComponentKind.PENDING_STATE) == ()


def test_eval_replays_evidenced_operation_history() -> None:
    case = EvalCase(
        id="ROB-TEST-HISTORY",
        source_type="synthetic",
        reference_time="2026-08-07T15:00:00+08:00",
        input="宽带229家庭",
        prior_turns=(
            EvalPriorTurn(
                event_type="user_message",
                content={"text": "午饭38个人"},
            ),
            EvalPriorTurn(
                event_type="operation_result",
                tool="finance.log_expense",
                content={"state": "succeeded", "record_id": "rec_eval_history"},
            ),
        ),
        expected=ExpectedBehavior(
            action="call_tool",
            tool="finance.log_expense",
            arguments={},
        ),
    )

    with envelope_factory(visible_tools()) as assemble:
        envelope = assemble(case)

    raw = "\n".join(envelope.texts_of(ComponentKind.RAW_EVENT))
    assert "午饭38个人" in raw
    assert "rec_eval_history" in raw
    assert '"state":"succeeded"' in raw
    assert envelope.texts_of(ComponentKind.PENDING_STATE) == ()
