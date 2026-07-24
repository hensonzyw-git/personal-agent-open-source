"""DEV-024: the closed Income Policy — salary or subject, or ask.

The rule is deterministic and binary by design, so the cases are the contract's
own examples plus the boundary the design calls out: a description that is
nothing but status words has no subject to store and must ask.
"""

from __future__ import annotations

import pytest

from personal_data_mcp.finance.income_policy import (
    IncomeClarification,
    ResolvedIncome,
    resolve_income,
)


@pytest.mark.parametrize(
    "text",
    ["工资", "今天发工资", "这个月的薪资", "发薪了", "月薪到账", "薪水"],
)
def test_salary_wording_maps_to_the_salary_category_and_name(text) -> None:
    resolved = resolve_income(text)
    assert isinstance(resolved, ResolvedIncome)
    assert resolved.category == "工资"
    assert resolved.name == "工资"


@pytest.mark.parametrize(
    "text,subject",
    [
        ("公积金入账", "公积金"),
        ("公积金到账", "公积金"),
        ("收到公积金", "公积金"),
        ("利息到账", "利息"),
        ("报销款到账", "报销款"),
    ],
)
def test_other_income_keeps_its_subject_without_status_words(text, subject) -> None:
    resolved = resolve_income(text)
    assert isinstance(resolved, ResolvedIncome)
    assert resolved.category == "其他"
    assert resolved.name == subject


def test_salary_takes_precedence_when_both_could_apply() -> None:
    # "工资到账" is salary; the status word is irrelevant once salary matches.
    resolved = resolve_income("工资到账")
    assert isinstance(resolved, ResolvedIncome)
    assert resolved.category == "工资"
    assert resolved.name == "工资"


@pytest.mark.parametrize("text", ["", "   ", "到账", "收到", "到账了"])
def test_a_description_with_no_subject_asks(text) -> None:
    assert resolve_income(text) is IncomeClarification.NO_SUBJECT


def test_a_plain_other_subject_is_preserved_verbatim() -> None:
    # No status word to strip: the subject stands as given.
    resolved = resolve_income("稿费")
    assert isinstance(resolved, ResolvedIncome)
    assert resolved.name == "稿费"
    assert resolved.category == "其他"
