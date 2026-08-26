"""DEV-040: the bookkeeping-intent guard that refuses prose-only "I recorded it".

Fail-closed by design: any plausible reading must return True, because a false
positive is a safe refusal and a false negative keeps a fake success. The cases
below lock the boundary -- what the guard must catch (the production regression)
and what it must leave alone (queries, plain chat, no amount).
"""

from __future__ import annotations

import pytest

from personal_agent.runtime.bookkeeping_intent import (
    is_finance_intent_candidate,
    is_finance_query_request,
    is_finance_retry_request,
    is_bookkeeping_write_request,
    is_expense_write_request,
    is_income_write_request,
)


@pytest.mark.parametrize(
    "text",
    [
        # The exact production regression: clean write shapes that the model
        # answered with "我来帮你记录…" and no tool call for a whole day.
        "咖啡 18 个人支出",
        "午饭 3000 个人支出",
        "午饭 9496 个人支出",
        "午饭演练1 68 个人",
        "午饭演练3 5 个人",
        # Family-fund and income shapes.
        "充 100 家庭基金",
        "把家庭基金补到 5000",
        "发工资 12000",
        # Shorthand with a currency word, no verb.
        "咖啡 18 块",
        "午饭 25 元",
        # Natural shorthand: no explicit "记/元/个人" token.
        "午饭 38",
        "晚饭30",
        "理发 80",
        "便利店 26.5",
        # Amount + a booking verb.
        "我昨天买了个包 300",
        "打车花了 45",
        "报销 120 打车费",
        # An accepted over-catch: "个人" is also the人数 reading, and the guard
        # deliberately refuses rather than risk a fake success. A refused prose
        # answer costs a retry; a false negative would silently record nothing.
        "我们 18 个人一起去",
    ],
)
def test_bookkeeping_write_requests_are_refused(text: str) -> None:
    assert is_bookkeeping_write_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "记收入 公积金 4000",
        "记收入",
        "发工资 12000",
        "公积金入账 4000",
    ],
)
def test_explicit_income_write_requests_are_identified(text: str) -> None:
    assert is_income_write_request(text) is True
    assert is_finance_intent_candidate(text) is True


def test_an_explicit_family_expense_selects_the_expense_tool() -> None:
    text = "昨天晚饭很久以前 283.99 家庭支出"
    assert is_expense_write_request(text) is True
    assert is_finance_intent_candidate(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "公积金 4000",
        "查一下今年收入多少",
        "工资是多少",
        "怎么记收入",
    ],
)
def test_ambiguous_or_question_income_phrases_do_not_select_the_income_tool(
    text: str,
) -> None:
    assert is_income_write_request(text) is False


@pytest.mark.parametrize(
    "text",
    [
        # No amount: a query or a request for help, not a claim of a write.
        "今天花了多少",
        "帮我查一下上个月的交通费",
        "你好",
        "",
        "   ",
        # An amount with no ledger signal at all.
        "这本书定价 45 我在考虑",
        "我的电话号码是 138",
        "电影评分 8.5",
        "80",
        # Date-bearing prose that only looks numeric.
        "8 月 5 号见面",
        # A general "help me record" with no amount is a request for guidance,
        # not a claim of a write -- the single 一 in 记一下 is not an amount.
        "帮我记一下这个月的支出",
        "记一下午饭",
    ],
)
def test_non_bookkeeping_messages_are_left_alone(text: str) -> None:
    assert is_bookkeeping_write_request(text) is False


@pytest.mark.parametrize(
    "text",
    ["重新记", "重新记一下。", "重试刚才那笔", "再记一次"],
)
def test_only_explicit_retry_phrases_require_finance_routing(text: str) -> None:
    assert is_finance_retry_request(text) is True
    assert is_finance_intent_candidate(text) is True


@pytest.mark.parametrize(
    "text",
    ["再试试模型好了吗", "重新回答", "就是今天", "继续"],
)
def test_generic_follow_ups_never_replay_finance(text: str) -> None:
    assert is_finance_retry_request(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "查一下我今年打网球花了多少钱",
        "今天花了多少",
        "帮我查一下上个月的交通费",
        "家庭基金余额多少",
        "今年网球一共花了几块",
    ],
)
def test_finance_queries_cannot_be_answered_from_model_memory(text: str) -> None:
    assert is_finance_query_request(text) is True
    assert is_finance_intent_candidate(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "查一下天气",
        "今年网球打了多少场",
        "这束花多少钱",
        "今天花了多少时间",
        "你好",
    ],
)
def test_non_finance_queries_remain_normal_conversation(text: str) -> None:
    assert is_finance_query_request(text) is False
