"""What a review card looks like to the client.

`DEV-028`, design 5.3 and 7.7. The list is cheap and local; opening one card is
the expensive part, because each item's values are read from the ledger *now*
rather than from anything cached here.

Two decisions are deliberate:

- **one unreadable row does not sink the card.** If a record cannot be read, the
  item is returned marked `unavailable` with the reason, and the rest of the
  card still renders. Failing the whole request would hide four good rows
  because of one, and silently dropping the row would be worse still -- the
  count would no longer match what was written.
- **`ack` and `defer` never touch the ledger.** They move a status column in the
  Agent database. Design 7.7 step 6 puts the boundary here: acknowledging that
  you looked at a card is not a financial act, and a review job that could
  mutate Finance would be a second write path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent.api.control_client import (
    ControlPlaneError,
    RecordFields,
    RecordUnavailable,
)
from personal_agent.api.daily_review import TABLE_KIND_BY_TOOL
from personal_agent.storage.models import DailyReview, DailyReviewItem
from personal_agent_core.errors import AppError, ErrorCode


#: One ordered card of record pointers -> current values. One batch keeps schema
#: validation and control-plane failure bounded per card.
RecordReader = Callable[
    [list[tuple[str, str]]],
    list[RecordFields | RecordUnavailable | None],
]

@dataclass(frozen=True)
class ReviewSummary:
    review_id: str
    review_date: str
    status: str
    item_count: int
    created_at: datetime
    reviewed_at: datetime | None

    def to_json(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "review_date": self.review_date,
            "status": self.status,
            "item_count": self.item_count,
            "created_at": self.created_at.isoformat(),
            "reviewed_at": (
                self.reviewed_at.isoformat() if self.reviewed_at else None
            ),
        }


def list_reviews(session: Session, *, status: str | None = None) -> list[ReviewSummary]:
    """Cards, newest day first. No ledger read happens here."""
    query = select(DailyReview).order_by(DailyReview.review_date.desc())
    if status is not None:
        query = query.where(DailyReview.status == status)
    return [
        ReviewSummary(
            review_id=review.review_id,
            review_date=review.review_date,
            status=review.status,
            item_count=_count_items(session, review.review_id),
            created_at=review.created_at,
            reviewed_at=review.reviewed_at,
        )
        for review in session.scalars(query)
    ]


def review_detail(
    session: Session, review_id: str, read_record: RecordReader
) -> dict[str, Any]:
    """One card, with every item's *current* ledger values."""
    review = _require_review(session, review_id)
    items = list(
        session.scalars(
            select(DailyReviewItem)
            .where(DailyReviewItem.review_id == review_id)
            .order_by(DailyReviewItem.committed_at)
        )
    )

    projected: list[dict[str, Any]] = []
    readable_indexes: list[int] = []
    pointers: list[tuple[str, str]] = []
    for item in items:
        entry: dict[str, Any] = {
            "record_id": item.record_id,
            "tool": item.tool,
            "committed_at": item.committed_at.isoformat(),
        }
        table_kind = TABLE_KIND_BY_TOOL.get(item.tool)
        if table_kind is None:
            entry["unavailable"] = "unknown_tool"
            projected.append(entry)
            continue
        entry["table_kind"] = table_kind
        readable_indexes.append(len(projected))
        pointers.append((table_kind, item.record_id))
        projected.append(entry)

    if pointers:
        try:
            records = read_record(pointers)
            if len(records) != len(pointers):
                raise ControlPlaneError("record batch returned the wrong result count")
        except ControlPlaneError:
            for index in readable_indexes:
                projected[index]["unavailable"] = "source_unavailable"
        else:
            for index, record in zip(readable_indexes, records, strict=True):
                if record is None:
                    projected[index]["unavailable"] = "no_receipt"
                elif isinstance(record, RecordUnavailable):
                    projected[index]["unavailable"] = "source_unavailable"
                else:
                    projected[index]["values"] = record.values
                    projected[index]["unreadable_fields"] = list(
                        record.unreadable_fields
                    )

    summary = ReviewSummary(
        review_id=review.review_id,
        review_date=review.review_date,
        status=review.status,
        item_count=len(projected),
        created_at=review.created_at,
        reviewed_at=review.reviewed_at,
    )
    return {**summary.to_json(), "items": projected}


def acknowledge(session: Session, review_id: str, *, now: datetime) -> ReviewSummary:
    """Mark the card as looked at. Idempotent, and free of ledger effects."""
    review = _require_review(session, review_id)
    if review.status != "reviewed":
        review.status = "reviewed"
        review.reviewed_at = now
    return _summary_of(session, review)


def defer(session: Session, review_id: str, *, now: datetime) -> ReviewSummary:
    """Push the card back for later.

    A reviewed card cannot be deferred: that would walk an acknowledgement
    backwards, and `reviewed_at` would then describe a review the user is being
    asked to redo.
    """
    review = _require_review(session, review_id)
    if review.status == "reviewed":
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="a reviewed card cannot be deferred",
        )
    review.status = "deferred"
    return _summary_of(session, review)


def _require_review(session: Session, review_id: str) -> DailyReview:
    review = session.get(DailyReview, review_id)
    if review is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"no such review {review_id}",
        )
    return review


def _summary_of(session: Session, review: DailyReview) -> ReviewSummary:
    return ReviewSummary(
        review_id=review.review_id,
        review_date=review.review_date,
        status=review.status,
        item_count=_count_items(session, review.review_id),
        created_at=review.created_at,
        reviewed_at=review.reviewed_at,
    )


def _count_items(session: Session, review_id: str) -> int:
    return len(
        list(
            session.scalars(
                select(DailyReviewItem.item_id).where(
                    DailyReviewItem.review_id == review_id
                )
            )
        )
    )
