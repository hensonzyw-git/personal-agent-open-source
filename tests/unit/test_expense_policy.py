"""DEV-021: resolvers that ask instead of guessing, and never inherit scope.

The rules under test are Henson's confirmed product contract, so the cases are
written as the contract states them -- including the ones whose correct answer
is "ask". A resolver that quietly picks a trip or a category is worse than one
that stops, because the mistake lands in a ledger and looks deliberate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from personal_data_mcp.finance.expense_policy import (
    Clarification,
    ClarificationReason,
    ResolvedExpense,
    resolve_expense,
)
from personal_data_mcp.finance.ledger_reader import LedgerExpense
from personal_data_mcp.finance.refund_matcher import find_original
from personal_data_mcp.finance.trip_tags import (
    InvalidTripTag,
    TripResolution,
    display_name,
    distinct_tags,
    normalise_tag,
    same_root_tags,
    tag_of,
)


TODAY = date(2026, 7, 23)


def row(
    name: str,
    *,
    amount: str = "100.00",
    category: str = "旅行",
    record_id: str = "rec1",
    when: date | None = None,
) -> LedgerExpense:
    return LedgerExpense(
        record_id=record_id,
        name=name,
        amount_cny=Decimal(amount),
        occurred_on=when or TODAY,
        category=category,
    )


def resolve(**overrides):
    args = {
        "name": "机票",
        "input_amount": "2000",
        "occurred_on": TODAY,
        "is_family_expense": True,
        "entry_kind": "expense",
    }
    args.update(overrides)
    return resolve_expense(**args)


# --- tag parsing and composition ---------------------------------------------


def test_a_tag_is_read_from_the_end_of_a_name() -> None:
    assert tag_of("机票 #东京02") == "东京02"
    assert tag_of("机酒退款 #潮汕") == "潮汕"
    assert tag_of("午饭") is None


def test_a_hash_inside_the_item_text_is_not_a_trip_tag() -> None:
    # Only a trailing tag is a trip; a `#` mid-name belongs to the item text.
    assert tag_of("买了 #2 号电池 若干") is None


def test_a_name_is_composed_as_item_space_hash_tag() -> None:
    assert display_name("机票", "东京02") == "机票 #东京02"
    assert display_name("午饭", None) == "午饭"


def test_an_unstorable_tag_is_refused() -> None:
    for bad in ("", "  ", "东京#02", "东 京"):
        with pytest.raises(InvalidTripTag):
            normalise_tag(bad)


# --- distinct trips, counted by value not by row -----------------------------


def test_twelve_rows_of_one_trip_are_still_one_trip() -> None:
    names = [f"消费{i} #东京" for i in range(12)]
    assert distinct_tags(names) == frozenset({"东京"})
    assert same_root_tags("东京", distinct_tags(names)) == ("东京",)


def test_a_numeric_suffix_is_the_same_destination() -> None:
    tags = distinct_tags(["机票 #东京01", "酒店 #东京02", "午饭 #大阪"])
    assert same_root_tags("东京", tags) == ("东京01", "东京02")


def test_a_non_numeric_suffix_is_a_different_destination() -> None:
    # `东京迪士尼` must not be folded into a `东京` trip by prefix alone.
    tags = distinct_tags(["门票 #东京迪士尼"])
    assert same_root_tags("东京", tags) == ()


# --- the travel resolver, exactly as the design's examples state -------------


def test_a_destination_with_no_existing_trip_creates_the_plain_root() -> None:
    rows = [row(f"消费{i} #大阪") for i in range(3)]
    resolved = resolve(destination="东京", ledger_rows=rows)
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.name == "机票 #东京"
    assert resolved.entry.category == "旅行"
    assert resolved.trip_resolution is TripResolution.CREATED_ROOT


def test_twelve_existing_tokyo_rows_are_reused_not_recreated() -> None:
    rows = [row(f"消费{i} #东京", record_id=f"rec{i}") for i in range(12)]
    resolved = resolve(destination="东京", ledger_rows=rows)
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.name == "机票 #东京"
    assert resolved.trip_resolution is TripResolution.REUSED_EXISTING


def test_two_same_destination_trips_must_ask() -> None:
    rows = [row("机票 #东京01"), row("酒店 #东京02", record_id="rec2")]
    resolved = resolve(destination="东京", ledger_rows=rows)
    assert isinstance(resolved, Clarification)
    assert resolved.reason is ClarificationReason.AMBIGUOUS_TRIP
    assert resolved.options == ("东京01", "东京02")


def test_a_root_and_a_numbered_trip_together_must_also_ask() -> None:
    rows = [row("机票 #东京"), row("酒店 #东京01", record_id="rec2")]
    resolved = resolve(destination="东京", ledger_rows=rows)
    assert isinstance(resolved, Clarification)
    assert resolved.reason is ClarificationReason.AMBIGUOUS_TRIP


def test_an_explicit_tag_is_used_as_written_without_consulting_the_ledger() -> None:
    rows = [row("机票 #东京01"), row("酒店 #东京02", record_id="rec2")]
    resolved = resolve(trip_tag="东京02", ledger_rows=rows)
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.name == "机票 #东京02"
    assert resolved.trip_resolution is TripResolution.EXPLICIT


def test_travel_without_any_resolvable_trip_must_ask() -> None:
    resolved = resolve(category="旅行", ledger_rows=[])
    assert isinstance(resolved, Clarification)
    assert resolved.reason is ClarificationReason.TRAVEL_WITHOUT_TRIP


def test_a_trip_tag_forces_the_travel_category() -> None:
    resolved = resolve(trip_tag="东京", category=None)
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.category == "旅行"


def test_a_taxi_in_a_trip_context_resolves_to_travel_with_the_destination() -> None:
    """A fictional trip-context taxi case must reuse the destination root.

    The ledger keeps the taxi action name, adds the trip tag, and stores the
    expense under the travel category.
    """
    rows = [row("机票 #示例城"), row("示例商品 #示例城", record_id="rec2")]
    # The MCP write path passes the model's `trip_tag` argument as
    # `destination=`: only the ledger decides which trip it names.
    resolved = resolve(
        name="打车",
        input_amount="27.3",
        destination="示例城",
        category=None,
        ledger_rows=rows,
    )
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.name == "打车 #示例城"
    assert resolved.entry.category == "旅行"
    assert resolved.trip_resolution is TripResolution.REUSED_EXISTING


def test_a_stated_category_that_contradicts_a_trip_tag_asks() -> None:
    resolved = resolve(trip_tag="东京", category="餐饮")
    assert isinstance(resolved, Clarification)
    assert resolved.reason is ClarificationReason.CATEGORY_CONFLICTS_WITH_TRIP


# --- refunds inherit a category, and nothing else ----------------------------


def test_a_refund_inherits_the_category_of_its_unique_original() -> None:
    rows = [row("机票 #东京02", amount="2000.00", category="旅行")]
    resolved = resolve(
        name="机票退款",
        input_amount="500",
        entry_kind="refund",
        category=None,
        trip_tag="东京02",
        is_family_expense=False,
        ledger_rows=rows,
    )
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.category == "旅行"
    assert resolved.entry.amount_cny == Decimal("-500")
    assert resolved.inherited_from_record_id == "rec1"


def test_a_refund_never_inherits_family_scope() -> None:
    # The original is a family expense; the refund says personal. The refund's
    # own statement must win, every time.
    rows = [row("机票 #东京02", amount="2000.00", category="旅行")]
    resolved = resolve(
        name="机票退款",
        input_amount="500",
        entry_kind="refund",
        category=None,
        trip_tag="东京02",
        is_family_expense=False,
        ledger_rows=rows,
    )
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.is_family_expense is False


def test_the_inheritance_object_can_only_carry_a_category() -> None:
    # Structural, not a convention: there is no scope field to misuse.
    rows = [row("机票 #东京02", amount="2000.00", category="旅行")]
    match = find_original(name="机票退款", trip_tag="东京02", rows=rows)
    assert match.inheritance is not None
    assert not any(
        "family" in field or "scope" in field
        for field in vars(match.inheritance)
    )


def test_two_candidate_originals_ask_even_when_they_agree() -> None:
    rows = [
        row("机票 #东京02", amount="2000.00", category="旅行", record_id="rec1"),
        row("机票 #东京02", amount="1800.00", category="旅行", record_id="rec2"),
    ]
    resolved = resolve(
        name="机票退款",
        input_amount="500",
        entry_kind="refund",
        category=None,
        trip_tag="东京02",
        ledger_rows=rows,
    )
    assert isinstance(resolved, Clarification)
    assert resolved.reason is ClarificationReason.ORIGINAL_NOT_UNIQUE


def test_a_refund_with_no_original_asks() -> None:
    resolved = resolve(
        name="机票退款",
        input_amount="500",
        entry_kind="refund",
        category=None,
        trip_tag="东京02",
        ledger_rows=[],
    )
    assert isinstance(resolved, Clarification)
    assert resolved.reason is ClarificationReason.ORIGINAL_NOT_UNIQUE


def test_a_mismatched_trip_tag_prevents_inheritance() -> None:
    rows = [row("机票 #东京01", amount="2000.00", category="旅行")]
    resolved = resolve(
        name="机票退款",
        input_amount="500",
        entry_kind="refund",
        category=None,
        trip_tag="东京02",
        ledger_rows=rows,
    )
    assert isinstance(resolved, Clarification)


def test_a_refund_cannot_be_the_original_of_another_refund() -> None:
    rows = [row("机票 #东京02", amount="-500.00", category="旅行")]
    match = find_original(name="机票退款", trip_tag="东京02", rows=rows)
    assert match.inheritance is None


def test_an_explicit_category_on_a_refund_wins_over_inheritance() -> None:
    rows = [row("机票 #东京02", amount="2000.00", category="旅行")]
    resolved = resolve(
        name="机票退款",
        input_amount="500",
        entry_kind="refund",
        category="旅行",
        trip_tag="东京02",
        ledger_rows=rows,
    )
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.inherited_from_record_id is None


# --- a plain expense may not arrive without a category -----------------------


def test_a_plain_expense_without_a_category_asks() -> None:
    resolved = resolve(name="午饭", input_amount="20", category=None)
    assert isinstance(resolved, Clarification)
    assert resolved.reason is ClarificationReason.CATEGORY_UNRESOLVED


def test_an_illegal_category_is_never_created() -> None:
    resolved = resolve(name="午饭", input_amount="20", category="新分类")
    assert isinstance(resolved, Clarification)
    assert resolved.reason is ClarificationReason.CATEGORY_UNRESOLVED


def test_the_users_item_text_is_preserved_exactly() -> None:
    resolved = resolve(name="  午饭 和 咖啡  ", input_amount="20", category="餐饮")
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.name == "  午饭 和 咖啡  "


def test_an_aa_receipt_is_stored_negative() -> None:
    resolved = resolve(
        name="AA收款 晚饭",
        input_amount="45",
        entry_kind="aa_reimbursement",
        category="餐饮",
        is_family_expense=False,
    )
    assert isinstance(resolved, ResolvedExpense)
    assert resolved.entry.amount_cny == Decimal("-45")
