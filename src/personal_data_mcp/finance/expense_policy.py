"""Turning a model's expense arguments into something writable, or a question.

This is the server-side half of the split the design draws at 7.3: the model
reads the user's sentence and hands over structured semantics, and the Finance
MCP decides everything that requires looking at the ledger. Concretely the model
supplies the item text, the amount, the date, the scope, the entry kind, and at
most a bare destination; this module resolves which trip that destination means,
what category a refund inherits, and what the stored name becomes.

Three rules hold no matter what arrives:

- **Scope is never inferred.** `is_family_expense` comes from the current input
  and from nowhere else. There is no code path that reads it from a matched
  original, from history, or from a default.
- **Nothing is guessed.** Every branch that cannot be decided from evidence
  returns a `Clarification`, and a clarification writes nothing at all.
- **The user's text survives.** The only edit ever made to a name is appending
  a resolved `#场次`. Normalised forms exist for matching and never leave.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from personal_agent_core.money import apply_entry_sign, parse_amount
from personal_agent_core.tool_ir import ALLOWED_EXPENSE_CATEGORIES
from personal_data_mcp.finance.expense_record import ExpenseEntry
from personal_data_mcp.finance.ledger_reader import LedgerExpense
from personal_data_mcp.finance.refund_matcher import find_original
from personal_data_mcp.finance.trip_tags import (
    InvalidTripTag,
    ResolvedTrip,
    TripResolution,
    display_name,
    distinct_tags,
    resolve_trip,
)


TRAVEL_CATEGORY: Final[str] = "旅行"
REDUCTION_KINDS: Final[frozenset[str]] = frozenset({"refund", "aa_reimbursement"})


class ClarificationReason(StrEnum):
    AMBIGUOUS_TRIP = "ambiguous_trip"
    TRAVEL_WITHOUT_TRIP = "travel_without_trip"
    CATEGORY_UNRESOLVED = "category_unresolved"
    ORIGINAL_NOT_UNIQUE = "original_not_unique"
    CATEGORY_CONFLICTS_WITH_TRIP = "category_conflicts_with_trip"


@dataclass(frozen=True)
class Clarification:
    """A question for Henson. Carrying one means nothing was written."""

    reason: ClarificationReason
    #: The real options, when there are any, so the question can be concrete
    #: rather than open-ended.
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedExpense:
    """Everything needed to write, with a record of how it was decided."""

    entry: ExpenseEntry
    trip_resolution: TripResolution | None
    inherited_from_record_id: str | None


def _resolved_trip(
    *,
    explicit_tag: str | None,
    destination: str | None,
    rows: list[LedgerExpense],
) -> ResolvedTrip | Clarification:
    existing = distinct_tags([row.name for row in rows])
    try:
        outcome = resolve_trip(
            explicit_tag=explicit_tag, destination=destination, existing=existing
        )
    except InvalidTripTag:
        return Clarification(ClarificationReason.AMBIGUOUS_TRIP)
    if outcome.needs_clarification:
        return Clarification(
            ClarificationReason.AMBIGUOUS_TRIP, outcome.candidates
        )
    return outcome


def resolve_expense(
    *,
    name: str,
    input_amount: str | Decimal,
    occurred_on: date,
    is_family_expense: bool,
    entry_kind: str,
    category: str | None = None,
    trip_tag: str | None = None,
    destination: str | None = None,
    ledger_rows: list[LedgerExpense] | None = None,
) -> ResolvedExpense | Clarification:
    """Resolve one entry, or return the question that blocks it.

    `trip_tag` is a tag the user wrote explicitly; `destination` is a place the
    model merely extracted, which only the ledger can turn into a tag.
    """
    rows = ledger_rows or []

    # --- the trip, first: it can decide the category ------------------------
    trip: ResolvedTrip | None = None
    if trip_tag is not None or destination is not None:
        outcome = _resolved_trip(
            explicit_tag=trip_tag, destination=destination, rows=rows
        )
        if isinstance(outcome, Clarification):
            return outcome
        trip = outcome

    tag = trip.tag if trip else None

    # --- the category --------------------------------------------------------
    if category is not None and category not in ALLOWED_EXPENSE_CATEGORIES:
        return Clarification(ClarificationReason.CATEGORY_UNRESOLVED)

    # Design 6.1 fixes the order, and it matters: inheritance is consulted
    # *before* the trip-tag rule. A refund tagged `#东京02` with no matching
    # original is not quietly filed as 旅行 -- design 7.5 wants an inconsistent
    # trip surfaced, because the likeliest cause is a mistyped tag.
    inherited_from: str | None = None
    if category is None:
        if entry_kind in REDUCTION_KINDS:
            match = find_original(name=name, trip_tag=tag, rows=rows)
            if match.inheritance is None:
                return Clarification(
                    ClarificationReason.ORIGINAL_NOT_UNIQUE,
                    tuple(row.name for row in match.candidates),
                )
            category = match.inheritance.category
            inherited_from = match.inheritance.matched_record_id
        elif tag is not None:
            # A resolved trip settles a plain expense's category (design 7.3),
            # which is what makes `东京机票 2000 家庭支出` writable without a
            # question.
            category = TRAVEL_CATEGORY
        else:
            # A plain expense missing a category is the model's question to
            # ask, not ours to invent.
            return Clarification(ClarificationReason.CATEGORY_UNRESOLVED)

    if tag is not None and category != TRAVEL_CATEGORY:
        # The two confirmed rules genuinely disagree here, so the honest move
        # is to ask rather than to silently override Henson.
        return Clarification(
            ClarificationReason.CATEGORY_CONFLICTS_WITH_TRIP,
            (category, TRAVEL_CATEGORY),
        )
    if tag is None and category == TRAVEL_CATEGORY:
        # 旅行 without a resolvable trip is not writable: the tag is the only
        # place a trip is ever recorded.
        return Clarification(ClarificationReason.TRAVEL_WITHOUT_TRIP)

    # --- the amount, and the name --------------------------------------------
    amount = apply_entry_sign(parse_amount(input_amount), entry_kind)

    return ResolvedExpense(
        entry=ExpenseEntry(
            name=display_name(name, tag),
            amount_cny=amount,
            occurred_on=occurred_on,
            # Straight from the argument. Never from `match`, never a default.
            is_family_expense=is_family_expense,
            category=category,
        ),
        trip_resolution=trip.resolution if trip else None,
        inherited_from_record_id=inherited_from,
    )
