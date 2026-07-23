from personal_agent_spike.contracts import EvalCase
from personal_agent_spike.online_eval import Observation, score_case


def test_call_score_normalizes_amount_and_requires_evidence() -> None:
    case = EvalCase.model_validate(
        {
            "id": "TEST-001",
            "source_type": "synthetic",
            "synthetic": True,
            "reference_time": "2026-07-23T15:00:00+08:00",
            "input": "午饭45",
            "expected": {
                "action": "call_tool",
                "tool": "finance.log_expense",
                "arguments": {
                    "name": "午饭",
                    "amount": "45.00",
                    "category": "餐饮",
                    "occurred_on": "2026-07-23",
                    "is_family_expense": False,
                },
            },
        }
    )
    observation = Observation(
        framework="test",
        model="test",
        latency_ms=1,
        tool_calls=[
            {
                "name": "mcp__fixture__finance_log_expense",
                "arguments": {
                    "name": "午饭",
                    "amount": "45",
                    "category": "餐饮",
                    "occurred_on": "2026-07-23",
                    "is_family_expense": False,
                },
            }
        ],
        tool_results=[
            {
                "is_error": False,
                "record_id": "fixture_123456789abc",
            }
        ],
        final_text="",
        tool_name="mcp__fixture__finance_log_expense",
        tool_arguments={
            "name": "午饭",
            "amount": "45",
            "category": "餐饮",
            "occurred_on": "2026-07-23",
            "is_family_expense": False,
        },
        tool_result_error=False,
        record_id="fixture_123456789abc",
    )
    score = score_case(case, observation)
    assert score["action_correct"] is True
    assert score["arguments_correct"] is True
    assert score["critical_arguments_correct"] is True
    assert score["evidence_correct"] is True
    assert score["safety_pass"] is True
    assert score["passed"] is True


def test_control_score_requires_structured_reason() -> None:
    case = EvalCase.model_validate(
        {
            "id": "TEST-002",
            "source_type": "synthetic",
            "synthetic": True,
            "reference_time": "2026-07-23T15:00:00+08:00",
            "input": "午饭算餐饮",
            "expected": {
                "action": "ask_clarification",
                "missing_fields": ["amount"],
                "reason_code": "MISSING_AMOUNT",
            },
        }
    )
    observation = Observation(
        framework="test",
        model="test",
        latency_ms=1,
        tool_calls=[],
        tool_results=[],
        final_text=(
            '{"action":"ask_clarification","missing_fields":["amount"],'
            '"requires_confirmation":false,"reason_code":"MISSING_AMOUNT"}'
        ),
        tool_name=None,
        tool_arguments={},
        tool_result_error=False,
        record_id=None,
    )
    assert score_case(case, observation)["passed"] is True


def test_call_score_treats_name_wording_as_non_critical() -> None:
    case = EvalCase.model_validate(
        {
            "id": "TEST-003",
            "source_type": "synthetic",
            "synthetic": True,
            "reference_time": "2026-07-23T15:00:00+08:00",
            "input": "Steam 买游戏 68",
            "expected": {
                "action": "call_tool",
                "tool": "finance.log_expense",
                "arguments": {
                    "name": "Steam游戏",
                    "amount": "68.00",
                    "category": "游戏",
                    "occurred_on": "2026-07-23",
                    "is_family_expense": False,
                },
            },
        }
    )
    observation = Observation(
        framework="test",
        model="test",
        latency_ms=1,
        tool_calls=[
            {
                "name": "finance.log_expense",
                "arguments": {
                    "name": "Steam买游戏",
                    "amount": "68",
                    "category": "游戏",
                    "occurred_on": "2026-07-23",
                    "is_family_expense": False,
                },
            }
        ],
        tool_results=[
            {
                "is_error": False,
                "record_id": "fixture_123456789abc",
            }
        ],
        final_text="",
    )
    score = score_case(case, observation)
    assert score["arguments_correct"] is False
    assert score["critical_arguments_correct"] is True
    assert score["name_exact"] is False
    assert score["passed"] is True


def test_multiple_clear_expenses_require_two_evidenced_calls() -> None:
    case = EvalCase.model_validate(
        {
            "id": "TEST-004",
            "source_type": "synthetic",
            "synthetic": True,
            "reference_time": "2026-07-23T15:00:00+08:00",
            "input": "午饭45，咖啡18",
            "expected": {
                "action": "call_tools",
                "calls": [
                    {
                        "tool": "finance.log_expense",
                        "arguments": {
                            "name": "午饭",
                            "amount": "45.00",
                            "category": "餐饮",
                            "occurred_on": "2026-07-23",
                            "is_family_expense": False,
                        },
                    },
                    {
                        "tool": "finance.log_expense",
                        "arguments": {
                            "name": "咖啡",
                            "amount": "18.00",
                            "category": "餐饮",
                            "occurred_on": "2026-07-23",
                            "is_family_expense": False,
                        },
                    },
                ],
            },
        }
    )
    observation = Observation(
        framework="test",
        model="test",
        latency_ms=1,
        tool_calls=[
            {
                "name": "finance.log_expense",
                "arguments": call.arguments,
            }
            for call in case.expected.calls
        ],
        tool_results=[
            {"is_error": False, "record_id": "fixture_123456789abc"},
            {"is_error": False, "record_id": "fixture_abcdef123456"},
        ],
        final_text="",
    )
    score = score_case(case, observation)
    assert score["observed_action"] == "call_tools"
    assert score["critical_arguments_correct"] is True
    assert score["evidence_correct"] is True
    assert score["passed"] is True


def test_missing_field_aliases_are_canonicalized() -> None:
    case = EvalCase.model_validate(
        {
            "id": "TEST-005",
            "source_type": "synthetic",
            "synthetic": True,
            "reference_time": "2026-07-23T15:00:00+08:00",
            "input": "超市买菜和日用品一共180，分别记两笔。",
            "expected": {
                "action": "ask_clarification",
                "missing_fields": ["per_item_amounts"],
                "reason_code": "ALLOCATION_REQUIRED",
            },
        }
    )
    observation = Observation(
        framework="test",
        model="test",
        latency_ms=1,
        tool_calls=[],
        tool_results=[],
        final_text=(
            '{"action":"ask_clarification",'
            '"missing_fields":["amount_allocation"],'
            '"requires_confirmation":false,'
            '"reason_code":"ALLOCATION_REQUIRED"}'
        ),
    )
    assert score_case(case, observation)["passed"] is True
