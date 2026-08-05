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
    "收",
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
    return any(token in text for token in _LEDGER_TOKENS)
