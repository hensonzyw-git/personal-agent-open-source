"""Trip tags: how they live in a name, and which one a destination means.

The ledger has no trip-tag column and will not get one (design 7.2). A trip's
only persistent form is the `#场次` suffix inside the expense name, so this
module owns both directions of that convention: reading tags out of existing
names, and deciding which tag a bare destination refers to.

The decision rule is the conservative one Henson confirmed (design 7.3), and the
counting subtlety matters: tags are deduplicated by *value*, not by how many
records carry them. Twelve rows all ending `#东京` are one trip, and reusing it
is correct. `东京01` and `东京02` are two, and choosing between them is a
question for Henson, never a guess.

Nothing here invents an abbreviation or a number. A destination with no existing
trip becomes the plain root (`东京`); anything ambiguous becomes a clarification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


#: A tag is the trailing `#...` of a display name. Anchored to the end because
#: that is the only position the user convention ever puts it in, so a `#` that
#: appears mid-name is part of the item text, not a trip.
_TRAILING_TAG: Final[re.Pattern[str]] = re.compile(r"#([^#\s]+)\s*$")

#: A repeat visit is distinguished by a numeric suffix (`东京01`, `东京02`).
#: Anything else after the root is a *different* destination, not a variant of
#: this one -- `东京迪士尼` must not be treated as a `东京` trip.
_NUMERIC_SUFFIX: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")


class TripResolution(StrEnum):
    CREATED_ROOT = "created_root"
    REUSED_EXISTING = "reused_existing"
    EXPLICIT = "explicit"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ResolvedTrip:
    """The outcome of resolving a destination against the ledger's trips."""

    resolution: TripResolution
    tag: str | None
    #: Every distinct same-root tag found, sorted. Populated for the ambiguous
    #: case so the clarification can show Henson the real choices.
    candidates: tuple[str, ...] = ()

    @property
    def needs_clarification(self) -> bool:
        return self.resolution is TripResolution.AMBIGUOUS


class InvalidTripTag(ValueError):
    """A tag that cannot be stored under the naming convention."""


def normalise_tag(raw: str) -> str:
    """Strip surrounding whitespace and refuse anything unstorable.

    A tag never contains `#` itself: the `#` is the delimiter the name uses, so
    a tag carrying one would produce a name that cannot be parsed back.
    """
    tag = raw.strip()
    if not tag:
        raise InvalidTripTag("a trip tag must not be empty")
    if "#" in tag:
        raise InvalidTripTag("a trip tag must not contain '#'")
    if any(character.isspace() for character in tag):
        raise InvalidTripTag("a trip tag must not contain whitespace")
    return tag


def tag_of(name: str) -> str | None:
    """The trip tag carried by a display name, if any."""
    match = _TRAILING_TAG.search(name)
    return match.group(1) if match else None


def display_name(item: str, tag: str | None) -> str:
    """Compose the stored name: `<item> #<tag>`.

    The item text is the user's, preserved exactly (design 6.2). The only thing
    added is the resolved tag, and only when there is one.
    """
    if tag is None:
        return item
    return f"{item} #{normalise_tag(tag)}"


def distinct_tags(names: list[str]) -> frozenset[str]:
    """Every distinct trip tag in a set of ledger names."""
    return frozenset(
        tag for tag in (tag_of(name) for name in names) if tag is not None
    )


def same_root_tags(root: str, tags: frozenset[str]) -> tuple[str, ...]:
    """The distinct existing tags that are this destination, sorted.

    A tag belongs to the root when it *is* the root, or when it is the root
    followed by a numeric repeat-visit suffix. That is deliberately narrow: a
    prefix match alone would silently fold a different destination into this
    one.
    """
    root = normalise_tag(root)
    matched = {
        tag
        for tag in tags
        if tag == root
        or (tag.startswith(root) and _NUMERIC_SUFFIX.match(tag[len(root):]))
    }
    return tuple(sorted(matched))


def resolve_trip(
    *, explicit_tag: str | None, destination: str | None, existing: frozenset[str]
) -> ResolvedTrip:
    """Decide which trip an entry belongs to.

    An explicitly given tag is used as written -- the user has already made the
    decision, and second-guessing it would be the guessing the contract forbids.
    Otherwise a destination is resolved against the trips the ledger already
    has: none means create the plain root, exactly one means reuse it, and two
    or more means ask.
    """
    if explicit_tag is not None:
        return ResolvedTrip(TripResolution.EXPLICIT, normalise_tag(explicit_tag))
    if destination is None:
        return ResolvedTrip(TripResolution.AMBIGUOUS, None)

    root = normalise_tag(destination)
    candidates = same_root_tags(root, existing)
    if not candidates:
        return ResolvedTrip(TripResolution.CREATED_ROOT, root)
    if len(candidates) == 1:
        return ResolvedTrip(TripResolution.REUSED_EXISTING, candidates[0])
    return ResolvedTrip(TripResolution.AMBIGUOUS, None, candidates)
