"""The production prompt must not contradict frozen Finance contracts."""

from personal_agent.runtime.prompt import build_system_prompt


def test_prompt_covers_every_current_runtime_tool() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    for tool in (
        "finance.log_expense",
        "finance.log_income",
        "finance.update_family_fund",
        "finance.query_expenses",
        "meta.capabilities",
    ):
        assert tool in prompt


def test_prompt_refuses_disabled_batch_without_selecting_one_entry() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    assert "agent.fail_batch_unavailable" in prompt
    assert "不得挑一笔" in prompt
    assert "拆分" in prompt
    assert "顺序写入" in prompt


def test_prompt_keeps_original_finance_boundaries() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    assert "没有默认值" in prompt
    assert "不要提供个人/家庭属性或收入分类" in prompt
    assert "不执行银行转账" in prompt
    assert "不要自行读取旧余额" in prompt
    assert "全量分页" in prompt
