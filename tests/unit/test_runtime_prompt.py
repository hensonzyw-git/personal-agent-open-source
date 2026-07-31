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


def test_prompt_forbids_model_side_duplicate_judgement() -> None:
    """The 2026-08-01 production incident: on a resent message, GLM saw the
    prior success in context and refused in prose, quoting the record_id --
    the server-side duplicate gate never fired and no decision card existed.
    The prompt must send resends through the tool instead.
    """

    prompt = build_system_prompt(today="2026-07-24")
    assert "不得据此自行判重" in prompt
    assert "仍照常调用对应工具" in prompt
    assert "是否重复由服务端判定" in prompt


def test_prompt_states_the_output_discipline_the_adapter_enforces() -> None:
    """The 2026-07-26 live smoke found all three of these shapes on real GLM.

    The gateway already fails closed on prose-as-a-question and on a tool call
    mixed with text, so these lines are not the safety property; they exist so a
    correct outcome is not lost to a preventable malformed turn.
    """

    prompt = build_system_prompt(today="2026-07-24")
    assert "任何需要用户回答的问句都必须" in prompt
    assert "不带任何解释文字" in prompt
    assert "日期缺失永远不是澄清理由" in prompt


def test_prompt_closes_the_expense_clarification_set() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    assert "本工具只在四种情形澄清" in prompt
    assert "没有写明充电宝是买的还是借/租的" in prompt
    assert "不要另外追问" in prompt


def test_prompt_contains_the_frozen_expense_classification_contract() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    for rule in (
        "用户明确写出的合法分类优先",
        "旅行标签时 category 必须是旅行",
        "活动语境优先于其中的餐饮词",
        "搓澡",
        "网球场地",
        "网球拍穿线",
        "买充电宝",
        "借用或租用充电宝",
        "非活动语境的饮料",
        "不能判断充电宝是购买还是借用",
    ):
        assert rule in prompt
