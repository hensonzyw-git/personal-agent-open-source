"""The daily review: one card per ledger day, built from Finance's own truth.

`DEV-028`, technical design 7.7. At 00:00 Asia/Shanghai the previous calendar
day's verified writes are collected from the Finance control plane and, if there
are any, projected into one review card. The card is a list of *pointers*; the
values are read live when it is opened, so a correction Henson made in Feishu on
the computer is what he sees.

Four rules are load-bearing, and each one is a rule about *not* doing something:

- **no writes, no card, no push.** Design 7.7 step 3. Creating an empty card
  every night would train the reviewer to dismiss it unread, which is the exact
  failure a review exists to prevent.
- **the unique `review_date` is the idempotency.** A catch-up run, a restarted
  timer and a manual invocation all collide on it, so a second card for a day is
  structurally impossible rather than merely unlikely.
- **late verified writes are reconciled.** A row still verifying at midnight
  must not disappear because another row already created the day's card. Missing
  pointers are added; a reviewed card is reopened and notified again.
- **an unreadable control plane is not an empty day.** The reader raises; this
  module lets it propagate so the caller rolls back and retries later. Catching
  it here would manufacture the "no writes" case out of a network failure.

The core is synchronous SQLite work with the control read injected, exactly like
`recovery.py`, so the async bridging stays in the composition root.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from personal_agent.api.control_client import ControlPlaneError, SuccessfulWrite
from personal_agent.storage.models import DailyReview, DailyReviewItem
from personal_agent_core.ids import new_id
from personal_agent_core.timeutil import parse_rfc3339


#: `YYYY-MM-DD` -> the verified writes committed on that Asia/Shanghai day.
WritesReader = Callable[[str], list[SuccessfulWrite]]

#: Design 7.7 step 7: a restart compensates day by day, at most a week back.
MAX_CATCH_UP_DAYS: int = 7


class ReviewResult(StrEnum):
    CREATED = "created"
    #: A later-verified write was added and the card was reopened if necessary.
    UPDATED = "updated"
    #: A card already exists for this day; it is left untouched.
    ALREADY_EXISTS = "already_exists"
    #: Nothing was written that day, so there is nothing to review.
    NO_WRITES = "no_writes"


@dataclass(frozen=True)
class ReviewOutcome:
    review_date: str
    result: ReviewResult
    review_id: str | None = None
    item_count: int = 0

    @property
    def should_notify(self) -> bool:
        """A new or materially changed card is worth a push."""
        return self.result in (ReviewResult.CREATED, ReviewResult.UPDATED)


TABLE_KIND_BY_TOOL: dict[str, str] = {
    "finance.log_expense": "expense",
    "finance.log_income": "income",
    "finance.update_family_fund": "family_fund",
}


def build_review(
    session: Session,
    read_writes: WritesReader,
    *,
    day: date,
    now: datetime,
) -> ReviewOutcome:
    """Create the card for one ledger day, or explain why there is none."""

    review_date = day.isoformat()
    existing = session.scalars(
        select(DailyReview).where(DailyReview.review_date == review_date)
    ).one_or_none()
    writes = _validated_unique_writes(read_writes(review_date))
    if not writes:
        if existing is not None:
            return ReviewOutcome(
                review_date=review_date,
                result=ReviewResult.ALREADY_EXISTS,
                review_id=existing.review_id,
                item_count=len(_items_of(session, existing.review_id)),
            )
        return ReviewOutcome(
            review_date=review_date, result=ReviewResult.NO_WRITES
        )

    if existing is None:
        review_id = new_id()
        review = DailyReview(
            review_id=review_id,
            review_date=review_date,
            status="pending",
            created_at=now,
        )
        session.add(review)
        result = ReviewResult.CREATED
        existing_keys: set[tuple[str, str]] = set()
    else:
        review = existing
        existing_keys = {
            (item.tool, item.record_id)
            for item in _items_of(session, review.review_id)
        }
        if all((write.tool, write.record_id) in existing_keys for write in writes):
            return ReviewOutcome(
                review_date=review_date,
                result=ReviewResult.ALREADY_EXISTS,
                review_id=review.review_id,
                item_count=len(existing_keys),
            )
        # A card previously acknowledged is no longer complete once a late
        # verified write appears. Reopen it rather than silently losing the row.
        review.status = "pending"
        review.reviewed_at = None
        result = ReviewResult.UPDATED

    for write in writes:
        if (write.tool, write.record_id) in existing_keys:
            continue
        session.add(
            DailyReviewItem(
                item_id=new_id(),
                review_id=review.review_id,
                tool=write.tool,
                record_id=write.record_id,
                committed_at=_as_instant(write.committed_at),
            )
        )
    try:
        session.flush()
    except IntegrityError:
        # Another run won the unique `review_date`. That is the constraint doing
        # its job, not an error: the day already has exactly one card.
        session.rollback()
        winner = session.scalars(
            select(DailyReview).where(DailyReview.review_date == review_date)
        ).one_or_none()
        return ReviewOutcome(
            review_date=review_date,
            result=ReviewResult.ALREADY_EXISTS,
            review_id=winner.review_id if winner is not None else None,
            item_count=(
                len(_items_of(session, winner.review_id))
                if winner is not None
                else 0
            ),
        )

    return ReviewOutcome(
        review_date=review_date,
        result=result,
        review_id=review.review_id,
        item_count=len(_items_of(session, review.review_id)),
    )


def catch_up_days(today: date, max_days: int = MAX_CATCH_UP_DAYS) -> list[date]:
    """The completed ledger days a run should cover, oldest first.

    Today is excluded because it is not over yet, and the window is bounded so a
    long outage compensates a week rather than the whole year (design 7.7 step
    7). Oldest first so the cards appear in the order they happened.
    """
    if max_days < 1:
        raise ValueError("max_days must be at least 1")
    return [today - timedelta(days=offset) for offset in range(max_days, 0, -1)]


def catch_up_reviews(
    session: Session,
    read_writes: WritesReader,
    *,
    today: date,
    now: datetime,
    max_days: int = MAX_CATCH_UP_DAYS,
) -> list[ReviewOutcome]:
    """Build any missing card for the last `max_days` completed ledger days.

    The nightly run and the restart compensation are the same code path: the
    nightly case is simply the one where only yesterday is missing. This builds
    them all in the caller's transaction; the scheduler job commits each day
    separately instead, so one unreadable day cannot discard the others.
    """
    return [
        build_review(session, read_writes, day=day, now=now)
        for day in catch_up_days(today, max_days)
    ]


def _validated_unique_writes(
    writes: list[SuccessfulWrite],
) -> list[SuccessfulWrite]:
    """Validate the tool/table binding and dedupe only within one table."""
    seen: set[tuple[str, str]] = set()
    unique: list[SuccessfulWrite] = []
    for write in writes:
        expected_table = TABLE_KIND_BY_TOOL.get(write.tool)
        if expected_table != write.table_kind:
            raise ControlPlaneError(
                "a successful write carried an unknown or mismatched tool/table"
            )
        key = (write.tool, write.record_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(write)
    return unique


def _items_of(session: Session, review_id: str) -> list[DailyReviewItem]:
    return list(
        session.scalars(
            select(DailyReviewItem).where(DailyReviewItem.review_id == review_id)
        )
    )


def _as_instant(value: str) -> datetime:
    """Parse the control plane's RFC 3339 commit instant, or refuse.

    `UtcTimestamp` rejects a naive datetime, so an unparseable instant must not
    be replaced with `now`: that would date a ledger row to when the review ran.
    """
    return parse_rfc3339(value)
