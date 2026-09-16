"""Conservative "is this a bookkeeping write request?" guard (`DEV-040`).

The model sometimes answers a bookkeeping request with a prose sentence like
"我来帮你记录这笔支出。现在为你提交记录。" and no tool call at all. The
orchestrator turns a `DirectAnswer` into `succeeded`, so that sentence is
recorded as success with **zero writes** -- the exact "model prose is not
evidence" failure §5.1 names, now observed live for an entire day.

This module supplies the guard: when the user's message is judged to be a
bookkeeping write request, the operation must fail closed rather than succeed
on model prose. The asymmetry is the argument -- a false positive costs one
safe refusal ("no tool was called, nothing was written"), a false negative
silently keeps a fake success -- so the bar is deliberately low: **one amount
token plus one ledger signal**.
"""

from __future__ import annotations

import re

#: An amount: an Arabic number, a Chinese numeral followed by a currency word
#: ("一块", "三百块"), or a run of at least two Chinese numerals ("十八",
#: "三千五百"). A single Chinese digit in prose is deliberately NOT an amount:
#: "记一下" and "第一个" both contain 一, and neither is a bookkeeping request.
_AMOUNT_RE = re.compile(
    r"\d+(?:\.\d+)?"
    r"|[一二两三四五六七八九十百千万零]+[点块元]"
    r"|[一二两三四五六七八九十百千万零]{2,}"
)

#: Ledger semantics. An ownership word, a verb that books money, or a currency
#: word -- "咖啡 18 块" is as much a bookkeeping request as "咖啡 18 个人支出".
#: "个人" also catches the人数 reading of "18 个人"; that misreads as a request
#: to record, which is the fail-closed direction.
_LEDGER_TOKENS = (
    "个人",
    "家庭",
    "家里",
    "记",
    "花",
    "买",
    "付",
    "充",
    "存",
    "支出",
    "消费",
    "充值",
    "转账",
    "报销",
    "退款",
    "工资",
    "元",
    "块",
    "钱",
    "AA",
    "aa",
)

#: Natural shorthand is open-ended (``理发 80``, a merchant name plus an
#: amount, and so on), so a subject allowlist can only ever encode yesterday's
#: vocabulary. Match the narrow *shape* instead: non-empty text followed by one
#: terminal exact amount. Contexts that clearly describe a score or measurement
#: are excluded below. This is only a prose-success guard; it never authorises a
#: write or supplies a missing Finance field.
_NATURAL_TRANSACTION_RE = re.compile(
    r"^\s*(?=.{1,60}$)"
    r"(?=.*[^0-9一二两三四五六七八九十百千万零点¥￥.\s元块人民币])"
    r".+?\s*[¥￥]?"
    r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百千万零]+)"
    r"(?:\s*(?:元|块|人民币))?\s*$"
)

_NON_FINANCE_MEASUREMENT_TOKENS = (
    "比分",
    "评分",
    "打分",
    "排名",
    "第几",
    "几号",
    "号线",
    "点入住",
    "点开始",
    "公里",
    "分钟",
    "小时",
    "个人",
    "电话",
    "手机",
    "号码",
    "编号",
    "验证码",
)

_FINANCE_RETRY_PHRASES = frozenset(
    {
        "重新记",
        "重新记一下",
        "重新记账",
        "重记",
        "重试刚才那笔",
        "重试这笔",
        "再记一次",
        "再试一次刚才那笔",
    }
)

_MONEY_QUESTION = r"(?:多少钱|多少(?!时间|小时|分钟|天)|几(?:块(?:钱)?|元|笔|条))"
_FINANCE_QUERY_RE = re.compile(
    rf"(?:花了|花费了?){_MONEY_QUESTION}"
    rf"|(?:查|查询|看看).{{0,20}}(?:支出|消费|费用|账本|账单|交通费|餐饮|收入|家庭基金|花销)"
    rf"|(?:支出|消费|费用|收入|家庭基金|花销).{{0,12}}(?:{_MONEY_QUESTION}|合计|总共|明细|记录)"
)

# An explicit request to record income is materially different from a generic
# bookkeeping write: the model must never be offered the expense tool for it.
# Keep this deliberately narrow.  An opaque "公积金 4000" could still mean a
# contribution expense, while "记收入 公积金 4000" and "公积金入账" are clear.
_INCOME_WRITE_RE = re.compile(
    r"(?:记(?:一笔)?|登记|录入|添加)\s*(?:收入|工资|薪资|薪水)"
    r"|(?:发工资|发薪|领工资|领薪)"
    r"|(?:工资|薪资|薪水|公积金|利息|稿费|报销款).{0,24}?(?:入账|到账|进账|收到|已到|到了)"
)


def is_income_write_request(text: str) -> bool:
    """Whether the source unambiguously asks to record income.

    This is a routing constraint rather than income extraction.  It does not
    infer a subject, amount, category, or date; those remain the income tool's
    and policy's responsibility.  A question stays out of this classifier so
    query semantics retain their existing, separately governed path.
    """
    if (
        not text
        or not text.strip()
        or is_finance_query_request(text)
        or any(token in text for token in ("怎么", "如何", "怎样", "能否", "可以", "？", "?"))
    ):
        return False
    return _INCOME_WRITE_RE.search(text) is not None


def is_expense_write_request(text: str) -> bool:
    """Whether the source unambiguously selects the expense write tool.

    This is intentionally narrower than the general bookkeeping guard.  Only
    a concrete amount together with the frozen `个人支出` / `家庭支出` wording
    binds the model to the expense tool.  Other write shapes can still need a
    resolver or clarification and therefore retain the wider write set.
    """
    return (
        bool(text and _AMOUNT_RE.search(text))
        and not is_finance_query_request(text)
        and ("个人支出" in text or "家庭支出" in text)
    )


def is_bookkeeping_write_request(text: str) -> bool:
    """Whether `text` must be handled only by a bookkeeping tool.

    Fail-closed by design: any plausible reading returns True, so the
    orchestrator refuses a prose answer instead of recording a fake success.
    A bare amount with no ledger signal ("我们 18 个人") returns False; a query
    with no amount ("今天花了多少") returns False, because neither can be
    confused with a write the model is claiming to have made.
    """
    if not text or not text.strip():
        return False
    if _AMOUNT_RE.search(text) is None:
        return False
    if any(token in text for token in _LEDGER_TOKENS):
        return True
    if any(token in text for token in _NON_FINANCE_MEASUREMENT_TOKENS):
        return False
    return _NATURAL_TRANSACTION_RE.fullmatch(text) is not None


def is_finance_retry_request(text: str) -> bool:
    """Whether `text` is an explicit request to retry the last safe failure.

    This intentionally accepts only a small closed phrase set. Generic model
    troubleshooting such as "再试试模型好了吗" must never replay a financial
    action merely because it contains the word "试".
    """
    if not text or not text.strip():
        return False
    normalized = re.sub(r"[\s，。！？、,.!?]+", "", text)
    return normalized in _FINANCE_RETRY_PHRASES


def is_finance_query_request(text: str) -> bool:
    """Whether a read question needs Finance rather than model recall."""
    if not text or not text.strip():
        return False
    return _FINANCE_QUERY_RE.search(text) is not None


def is_finance_intent_candidate(text: str) -> bool:
    """Whether this turn must not complete as an ordinary direct answer."""
    return (
        is_bookkeeping_write_request(text)
        or is_expense_write_request(text)
        or is_income_write_request(text)
        or is_finance_query_request(text)
        or is_finance_retry_request(text)
    )
