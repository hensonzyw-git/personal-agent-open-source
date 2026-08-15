"""The receipt record projection refuses everything it has not seen before.

Written before the wiring, per `AGENTS.md` §5.1: the failure cases at a
model/connector boundary are the design, not an afterthought. The shapes tested
here are the ones that would otherwise reach the iOS receipt card, where a
wrong number is indistinguishable from a right one.
"""

from __future__ import annotations

import json

import pytest

from personal_agent.api.finance_record_projection import (
    FinanceExpenseRecord,
    FinanceRecordProjectionError,
    decode_finance_expense_record,
)


def a_record(**overrides) -> dict:
    record = {
        "name": "午饭",
        "amount_cny": "38.50",
        "occurred_on": "2026-08-15",
        "is_family_expense": False,
        "category": "餐饮",
        "personal_spend_cny": "38.50",
    }
    record.update(overrides)
    return {key: value for key, value in record.items() if value is not _ABSENT}


_ABSENT = object()


# --- what a valid record is ---------------------------------------------------


def test_a_full_record_decodes_every_field():
    record = decode_finance_expense_record(a_record())
    assert record == FinanceExpenseRecord(
        name="午饭",
        amount_cny="38.50",
        occurred_on="2026-08-15",
        is_family_expense=False,
        category="餐饮",
        personal_spend_cny="38.50",
    )


def test_a_canonical_json_string_decodes_identically():
    """History and the live receipt must go through the same reader."""
    live = decode_finance_expense_record(a_record())
    durable = decode_finance_expense_record(json.dumps(a_record()))
    assert live == durable


def test_to_dict_round_trips_through_the_same_decoder():
    record = decode_finance_expense_record(a_record())
    assert decode_finance_expense_record(record.to_dict()) == record


def test_a_family_expense_keeps_the_formula_field_apart_from_the_amount():
    record = decode_finance_expense_record(
        a_record(is_family_expense=True, amount_cny="200.00",
                 personal_spend_cny="100.00")
    )
    assert record.is_family_expense is True
    assert record.amount_cny == "200.00"
    # 个人支出 is the Base formula's answer, never re-derived on this side.
    assert record.personal_spend_cny == "100.00"


# --- the two legitimate absences ---------------------------------------------


def test_a_missing_record_is_none_not_an_error():
    assert decode_finance_expense_record(None) is None


def test_an_idempotent_replays_empty_record_is_none_not_an_error():
    """`_record_receipt` returns `{}` on replay: it knows the receipt, not the
    fields. That must fall back to the status row, never to invented fields."""
    assert decode_finance_expense_record({}) is None


def test_an_uncomputed_formula_field_is_absent_not_zero():
    record = decode_finance_expense_record(a_record(personal_spend_cny=_ABSENT))
    assert record is not None
    assert record.personal_spend_cny is None


def test_a_refund_may_carry_no_category():
    """The write contract allows a null category for refunds and AA receipts."""
    record = decode_finance_expense_record(a_record(category=None))
    assert record is not None
    assert record.category is None


# --- everything else fails closed --------------------------------------------


def test_an_unknown_field_is_refused_rather_than_dropped():
    with pytest.raises(FinanceRecordProjectionError, match="unknown fields"):
        decode_finance_expense_record(a_record(merchant_note="内部备注"))


def test_a_non_object_record_is_refused():
    with pytest.raises(FinanceRecordProjectionError, match="not an object"):
        decode_finance_expense_record(["午饭", "38.50"])


def test_invalid_json_is_refused():
    with pytest.raises(FinanceRecordProjectionError, match="not valid JSON"):
        decode_finance_expense_record("{not json")


@pytest.mark.parametrize("amount", [38.5, 38, 0, True])
def test_a_numeric_amount_is_refused_never_stringified(amount):
    """Money is a decimal string. Accepting a float and rendering `str()` of it
    is how a card shows ¥0.30000000000000004 for two dimes."""
    with pytest.raises(FinanceRecordProjectionError, match="money must stay"):
        decode_finance_expense_record(a_record(amount_cny=amount))


def test_a_numeric_personal_spend_is_refused_too():
    with pytest.raises(FinanceRecordProjectionError, match="money must stay"):
        decode_finance_expense_record(a_record(personal_spend_cny=100.0))


@pytest.mark.parametrize("field", ["name", "occurred_on", "amount_cny"])
def test_a_missing_required_field_is_refused(field):
    with pytest.raises(FinanceRecordProjectionError):
        decode_finance_expense_record(a_record(**{field: _ABSENT}))


@pytest.mark.parametrize("field", ["name", "occurred_on", "amount_cny"])
def test_an_empty_required_string_is_refused(field):
    with pytest.raises(FinanceRecordProjectionError):
        decode_finance_expense_record(a_record(**{field: ""}))


def test_a_missing_family_flag_is_refused_never_defaulted_to_false():
    """Defaulting would silently turn a family expense into a personal one on
    the card — the one field where a wrong default is a wrong accounting fact."""
    with pytest.raises(FinanceRecordProjectionError, match="is_family_expense"):
        decode_finance_expense_record(a_record(is_family_expense=_ABSENT))


@pytest.mark.parametrize("value", ["true", 1, 0, None])
def test_a_non_boolean_family_flag_is_refused(value):
    with pytest.raises(FinanceRecordProjectionError, match="is_family_expense"):
        decode_finance_expense_record(a_record(is_family_expense=value))


def test_a_non_string_category_is_refused():
    with pytest.raises(FinanceRecordProjectionError, match="category"):
        decode_finance_expense_record(a_record(category=7))


def test_an_empty_category_string_is_refused_as_distinct_from_null():
    """Null means "this row legitimately has no category". Empty string is a
    connector that lost one, and the two must not collapse."""
    with pytest.raises(FinanceRecordProjectionError, match="category"):
        decode_finance_expense_record(a_record(category=""))


# --- the edit marker ----------------------------------------------------------


def test_with_category_marks_the_edit_and_drops_the_stale_formula():
    original = decode_finance_expense_record(a_record())
    edited = original.with_category("购物", updated_at="2026-08-15T10:00:00Z")

    assert edited.category == "购物"
    assert edited.category_updated_at == "2026-08-15T10:00:00Z"
    # The Base formula may depend on 分类; carrying the old number forward would
    # put a value on the card the ledger no longer agrees with.
    assert edited.personal_spend_cny is None
    # Nothing else moved.
    assert edited.name == original.name
    assert edited.amount_cny == original.amount_cny
    assert edited.occurred_on == original.occurred_on
    assert edited.is_family_expense == original.is_family_expense


def test_an_edited_record_round_trips():
    edited = decode_finance_expense_record(a_record()).with_category(
        "购物", updated_at="2026-08-15T10:00:00Z"
    )
    assert decode_finance_expense_record(edited.to_dict()) == edited


def test_a_non_string_edit_marker_is_refused():
    with pytest.raises(FinanceRecordProjectionError, match="category_updated_at"):
        decode_finance_expense_record(a_record(category_updated_at=1755250000))
