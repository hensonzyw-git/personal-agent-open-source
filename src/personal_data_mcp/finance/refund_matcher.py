"""Finding the original expense a refund or AA receipt reduces.

A refund may inherit *one* thing from the expense it reduces: the category
(design 7.5). It must never inherit the family/personal scope, and the shape of
this module is the reason it cannot -- `CategoryInheritance` carries a category
and nothing else, so there is no field through which scope could travel even by
mistake.

Matching is deliberately strict. It compares a normalised copy of the item text
and requires the trip tag to agree, and it only inherits when exactly one
candidate survives. Zero candidates, several candidates, or a name too irregular
to compare all produce a clarification rather than a guess, because inheriting
the wrong category quietly mis-files money.

The normalised copy is for comparison only. It never becomes a stored name --
Feishu keeps exactly what the user typed (design 6.2).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from personal_data_mcp.finance.ledger_reader import LedgerExpense
from personal_data_mcp.finance.trip_tags import tag_of


#: Words that mark an entry as the *reduction* rather than the original. They
#: are stripped before comparison so `机票退款 #东京02` can find `机票 #东京02`.
#: Only forms Henson's own ledger and the design examples use are listed; this
#: is not a place to invent vocabulary.
_REDUCTION_MARKERS: Final[tuple[str, ...]] = (
    "退款",
    "退费",
    "AA收款",
    "AA 收款",
    "aa收款",
    "AA",
)

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
_TRAILING_TAG_SUB: Final[re.Pattern[str]] = re.compile(r"#[^#\s]+\s*$")


@dataclass(frozen=True)
class CategoryInheritance:
    """What a matched original is allowed to give the new entry.

    One field, on purpose. Scope is absent by construction, not by discipline.
    """

    category: str
    matched_record_id: str


@dataclass(frozen=True)
class MatchOutcome:
    inheritance: CategoryInheritance | None
    #: Populated when the match was not unique, so a clarification can show the
    #: real candidates instead of asking an open question.
    candidates: tuple[LedgerExpense, ...] = ()

    @property
    def needs_clarification(self) -> bool:
        return self.inheritance is None


def normalise_for_match(name: str) -> str:
    """A comparison-only form of an item name.

    Case, width and spacing differences are noise for matching; the trip tag is
    handled separately, so it is removed here. This value is never written
    anywhere.
    """
    text = unicodedata.normalize("NFKC", name)
    text = _TRAILING_TAG_SUB.sub("", text)
    for marker in _REDUCTION_MARKERS:
        text = text.replace(marker, "")
    text = _WHITESPACE.sub("", text)
    return text.casefold()



def find_original(
    *,
    name: str,
    trip_tag: str | None,
    rows: list[LedgerExpense],
) -> MatchOutcome:
    """Find the single original expense this reduction refers to.

    A candidate must match on the normalised item text, on the trip tag, and be
    a positive amount -- a refund cannot be the original of another refund.
    """
    target = normalise_for_match(name)
    if not target:
        # Nothing comparable is left once markers are stripped, so there is no
        # honest way to match. Ask rather than pick something.
        return MatchOutcome(None)

    candidates = [
        row
        for row in rows
        if row.amount_cny is not None
        and row.amount_cny > 0
        and row.category
        and tag_of(row.name) == trip_tag
        and normalise_for_match(row.name) == target
    ]

    if len(candidates) == 1:
        row = candidates[0]
        return MatchOutcome(
            CategoryInheritance(
                category=row.category, matched_record_id=row.record_id
            )
        )
    # Several candidates ask, even when they happen to agree on the category.
    # Design 7.5 inherits only from a *unique* original, and "they all say
    # 旅行 anyway" is exactly the shortcut that stops being true the first time
    # a genuinely different original hides among duplicates.
    return MatchOutcome(None, tuple(candidates))
