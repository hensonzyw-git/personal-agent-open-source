from personal_agent_spike.policy import decide_tool_call


VALID_EXPENSE = {
    "name": "午饭",
    "amount": "45.00",
    "category": "餐饮",
    "occurred_on": "2026-07-23",
    "is_family_expense": False,
}


def test_r2_expense_is_direct_write() -> None:
    decision = decide_tool_call(
        "finance.log_expense",
        VALID_EXPENSE,
        {"finance.expense.write"},
    )
    assert decision.outcome == "allow"
    assert decision.reason_code == "R2_DIRECT_WRITE"


def test_missing_scope_is_denied() -> None:
    decision = decide_tool_call("finance.log_expense", VALID_EXPENSE, set())
    assert decision.outcome == "deny"
    assert decision.reason_code == "SCOPE_DENIED"


def test_unknown_category_requires_clarification() -> None:
    invalid = {**VALID_EXPENSE, "category": "医疗"}
    decision = decide_tool_call(
        "finance.log_expense",
        invalid,
        {"finance.expense.write"},
    )
    assert decision.outcome == "ask"
    assert decision.reason_code == "CATEGORY_NOT_ALLOWED"


def test_unknown_tool_is_denied() -> None:
    decision = decide_tool_call("shell.run", {}, {"*"})
    assert decision.outcome == "deny"
    assert decision.reason_code == "TOOL_NOT_ALLOWLISTED"


def test_amount_must_match_ir_decimal_string() -> None:
    for invalid_amount in ("1e3", "NaN", "Infinity", "-1", "1.234", 45):
        decision = decide_tool_call(
            "finance.log_expense",
            {**VALID_EXPENSE, "amount": invalid_amount},
            {"finance.expense.write"},
        )
        assert decision.outcome == "ask"
        assert decision.reason_code == "INVALID_AMOUNT"
