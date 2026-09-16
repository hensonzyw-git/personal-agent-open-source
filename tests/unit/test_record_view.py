"""DEV-028: projecting a record's current values for a review card.

The card exists so Henson can check what the ledger holds *now*, which means the
dangerous failure here is not an error -- it is a plausible-looking wrong value.
These cases are chosen accordingly: every one of them is a cell shape that could
be silently coerced into something believable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.record_view import project_record


LEDGER_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads((LEDGER_FIXTURES / "config.synthetic.json").read_text("utf-8"))
)


def project(fields: dict, table_kind: str = "expense"):
    return project_record(
        fields, config=CONFIG, table_kind=table_kind, record_id="recX"
    )


def test_every_configured_field_is_read_by_meaning() -> None:
    view = project(
        {
            "原始金额": "20",
            "名称": [{"text": "午饭", "type": "text"}],
            # 2026-07-23 14:00 UTC is the 23rd, 22:00, in Asia/Shanghai.
            "日期": 1784815200000,
            "是否家庭支出": False,
            "分类": "餐饮",
            "个人支出": {"type": 2, "value": [20]},
        }
    )

    assert view.values == {
        "amount": "20.00",
        "name": "午饭",
        "occurred_on": "2026-07-23",
        "is_family_expense": False,
        "category": "餐饮",
        "personal_spend": "20.00",
    }
    assert view.unreadable_fields == ()


def test_a_money_value_never_crosses_as_a_float() -> None:
    """A JSON float is exactly how a cent goes missing three layers later."""
    view = project({"原始金额": 20.1})

    assert view.values["amount"] == "20.10"
    assert isinstance(view.values["amount"], str)


def test_an_unparseable_amount_is_unreadable_rather_than_zero() -> None:
    view = project({"原始金额": {"unexpected": "shape"}})

    assert "amount" not in view.values
    assert "amount" in view.unreadable_fields


def test_an_unparseable_date_is_unreadable_rather_than_today() -> None:
    view = project({"日期": "not-a-timestamp"})

    assert "occurred_on" not in view.values
    assert "occurred_on" in view.unreadable_fields


def test_a_non_boolean_family_flag_is_unreadable_rather_than_truthy() -> None:
    """`"true"`, `1` and `"是"` must not all quietly become a family expense."""
    for hostile in ("true", 1, "是", [], {}):
        view = project({"是否家庭支出": hostile})
        assert "is_family_expense" in view.unreadable_fields, hostile


def test_an_absent_checkbox_is_false_but_an_absent_text_is_empty() -> None:
    view = project({})

    assert view.values["is_family_expense"] is False
    assert view.values["name"] is None
    assert view.values["category"] is None
    assert view.unreadable_fields == ()


def test_a_formula_uses_the_only_shape_with_live_provider_evidence() -> None:
    view = project({"个人支出": {"type": 2, "value": [20]}})

    assert view.values["personal_spend"] == "20.00"


def test_a_bare_number_formula_result_from_get_record_is_readable() -> None:
    """`get_record` returns the number result without the envelope; the 2026-08-19
    live probe observed `17` and `41.91` for 个人支出."""
    assert project({"个人支出": 17}).values["personal_spend"] == "17.00"
    assert project({"个人支出": 41.91}).values["personal_spend"] == "41.91"


@pytest.mark.parametrize(
    "cell",
    [
        {"type": 1, "value": [20]},
        {"type": 2, "value": [20, 30]},
        {"type": 2, "value": "20"},
        {"type": 2},
        True,
        "20",
        [],
    ],
)
def test_an_unrecognised_formula_envelope_is_unreadable(cell) -> None:
    """A changed formula response must not be interpreted as a number."""
    view = project({"个人支出": cell})

    assert "personal_spend" not in view.values
    assert "personal_spend" in view.unreadable_fields


def test_a_column_outside_the_config_never_leaves() -> None:
    view = project({"名称": "午饭", "某人新加的列": "机密"})

    assert "某人新加的列" not in json.dumps(view.to_json(), ensure_ascii=False)


def test_an_unknown_table_kind_is_refused() -> None:
    with pytest.raises(KeyError):
        project({}, table_kind="wardrobe")


def test_income_and_family_fund_project_their_own_fields() -> None:
    income = project({"金额": 100, "名称": "工资", "分类": "工资"}, "income")
    assert income.values["amount"] == "100.00"
    assert income.values["category"] == "工资"
    assert "is_family_expense" not in income.values

    fund = project(
        {"充值金额": 10, "家庭基金余额": {"type": 2, "value": [15454.41]}},
        "family_fund",
    )
    assert fund.values["recharge_amount"] == "10.00"
    assert fund.values["balance"] == "15454.41"
