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
    assert "绝不能为“个人还是家庭收入”或日期缺失发起澄清" in prompt
    assert "商户名还是付款时间" in prompt
    assert "不执行银行转账" in prompt
    assert "不要自行读取旧余额" in prompt
    assert "全量分页" in prompt
    assert "午饭45" in prompt
    assert "绝不能猜 false 或 true" in prompt


def test_prompt_requires_structured_fail_safe_for_out_of_scope_requests() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    assert 'agent.fail_safely(reason="TOOL_NOT_ALLOWLISTED")' in prompt
    assert 'agent.fail_safely(reason="UNSUPPORTED_OPERATION")' in prompt
    assert "不用自由文本拒绝" in prompt


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

    The gateway still fails closed on prose-as-a-question and multiple calls.
    One valid call plus text has an explicit suppressed-untrusted disposition;
    these lines reduce that preventable provider shape without making the prompt
    the safety boundary.
    """

    prompt = build_system_prompt(today="2026-07-24")
    assert "任何需要用户回答的问句都必须" in prompt
    assert "只输出一个工具调用，不附带解释文字" in prompt
    assert "标记为不可信并抑制" in prompt
    assert "它绝不会成为用户可见回答、工具参数" in prompt
    assert "或成功/写入证据" in prompt
    assert "日期缺失永远不是澄清理由" in prompt
    assert "前两天" in prompt
    assert "不能唯一确定日期" in prompt


def test_prompt_explains_how_a_clarification_answer_resumes_the_original_request() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    assert "clarification_context" in prompt
    assert "original_user_text" in prompt
    assert "completed_exchanges" in prompt
    assert "pending_question" in prompt
    assert "不能把当前短回答当成新的普通对话" in prompt
    assert "也不能重复" in prompt
    assert "已被当前回答解决的问题" in prompt


def test_prompt_closes_the_expense_clarification_set() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    assert "本工具在以下情形澄清" in prompt
    assert "没有写明充电宝是买的还是借/租的" in prompt
    assert "不要另外追问" in prompt
    assert "金额缺失" in prompt
    assert "区间/近似值" in prompt
    assert "不透明商户名" in prompt


def test_prompt_contains_hensons_robustness_decisions() -> None:
    prompt = build_system_prompt(today="2026-07-24")
    for rule in (
        "晚饭示例餐馆",
        "示例餐馆",
        "自称认识商户",
        "还了某人",
        "TOOL_NOT_ALLOWLISTED",
        "四五十",
        "六百多",
    ):
        assert rule in prompt


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


def test_prompt_pins_query_aggregation_and_full_calendar_ranges() -> None:
    prompt = build_system_prompt(today="2026-08-12")
    assert "当年 1 月 1 日至 12 月 31 日" in prompt
    assert "绝不能截断到今天" in prompt
    assert "“花了多少钱”“一共多少”或“总额”时用 total" in prompt
    assert "分类统计或分类聚合时用\n  by_category" in prompt
    assert "明确要求“哪些”“列出”“明细”或“账单列表”时用 records" in prompt


def test_prompt_distinguishes_travel_from_local_transport_and_preserves_names() -> None:
    prompt = build_system_prompt(today="2026-08-12")
    for rule in (
        "不得写成“出行”",
        "打车、地铁、停车",
        "把目的地（如东京、美国、瑞士）放入 trip_tag",
        "“买水”“买衣服”“买洗水果篮”“买网球”不能删成",
        "“东京机票”“瑞士酒店”则分别以“机票”“酒店”为 name",
    ):
        assert rule in prompt


def test_prompt_sends_destination_transport_in_trip_context_to_trip_tag() -> None:
    """fictional trip-context case: `示例城打车 27.3 家庭支出` was extracted as
    `category=出行` with no trip_tag after the provider switch. The prompt
    placed "旅行目的地不是本地交通" and "出行只用于打车地铁停车" side by side,
    so the trip-context taxi fell into the gap between the two sentences:
    GLM read the ambiguity the intended way, DeepSeek read it literally and
    reasoned that the trip_tag rule "is specifically for travel items". The
    server already forces tag⇒旅行 and reuses the ledger's trip root, so the
    fix is a prompt rule pinning this exact case, not an MCP change."""
    prompt = build_system_prompt(today="2025-03-12")
    for rule in (
        "行程中",
        "示例城打车",
        "trip_tag",
        "仍是旅行消费",
    ):
        assert rule in prompt
    # The new rule must sit inside the travel/transport paragraph it repairs,
    # so "出行" remains pinned to no-trip-context transport only.
    assert "不属行程、没有目的地" in prompt
