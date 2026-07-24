"""The closed, deterministic Income Policy: salary or other, name and category.

Income is not the expense tool. It carries no family scope, no trip, no refund
or AA semantics, and the model does not supply a category -- the server decides
it from a two-way rule (design 4.5):

- text with explicit salary meaning -> category `工资`, name `工资`;
- any other clear income -> category `其他`, name the extracted subject, with
  the generic income verbs/status words removed (`公积金入账` -> `公积金`).

This is the income tool's one, narrow exception to the "preserve the user's
name verbatim" rule, and it is deliberately small: only a fixed list of trailing
status words is stripped, nothing is reworded, and if nothing but status words
remains there is no honest subject to store, so the policy asks rather than
inventing one.

`log_income` rejects a negative amount elsewhere; a refund or AA receipt is an
expense reduction, not income.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from personal_data_mcp.finance.ledger_config import EXPECTED_INCOME_CATEGORIES


SALARY_CATEGORY: Final[str] = "工资"
OTHER_CATEGORY: Final[str] = "其他"

#: Explicit salary wording. Matched as substrings because Chinese has no word
#: boundaries; every entry here is unambiguously salary, so a substring hit is
#: safe. This list is confirmed product vocabulary, not a place to guess.
_SALARY_MARKERS: Final[tuple[str, ...]] = (
    "工资",
    "发工资",
    "薪资",
    "发薪",
    "月薪",
    "薪水",
)

#: Generic income verbs/status words stripped from an `其他` subject. Only these
#: fixed forms are removed, and only as a cleanup of the subject -- the tool
#: never rewords or normalises what remains.
_STATUS_WORDS: Final[tuple[str, ...]] = (
    "入账",
    "到账",
    "收到",
    "进账",
    "已到",
    "到了",
)

#: Trailing modal particles that can survive status-word removal ("到账了" ->
#: "了"). An income subject is a substantive noun, so a residue of only these is
#: not a subject. Stripped only from the ends, so a real subject is untouched.
_TRAILING_PARTICLES: Final[str] = "了啦呢吧的"


class IncomeClarification(StrEnum):
    NO_SUBJECT = "income_no_subject"


@dataclass(frozen=True)
class ResolvedIncome:
    name: str
    category: str


def _is_salary(text: str) -> bool:
    return any(marker in text for marker in _SALARY_MARKERS)


def _strip_status_words(text: str) -> str:
    subject = text.strip()
    # Remove status words wherever they sit, then tidy whitespace. They are
    # generic ("到账", "收到"), so removing them anywhere is safe; the subject is
    # what a human would call the income.
    for word in _STATUS_WORDS:
        subject = subject.replace(word, "")
    return subject.strip().strip(_TRAILING_PARTICLES).strip()


def resolve_income(description: str) -> ResolvedIncome | IncomeClarification:
    """Classify one income description into its ledger name and category."""
    text = description.strip()
    if not text:
        return IncomeClarification.NO_SUBJECT
    if _is_salary(text):
        return ResolvedIncome(name=SALARY_CATEGORY, category=SALARY_CATEGORY)

    subject = _strip_status_words(text)
    if not subject:
        # Nothing but status words was given (e.g. "到账了"): there is no subject
        # to store, so ask rather than write a blank or a generic name.
        return IncomeClarification.NO_SUBJECT
    return ResolvedIncome(name=subject, category=OTHER_CATEGORY)


# A guard so the category constants never drift from the ledger contract.
assert set((SALARY_CATEGORY, OTHER_CATEGORY)) == set(EXPECTED_INCOME_CATEGORIES)
