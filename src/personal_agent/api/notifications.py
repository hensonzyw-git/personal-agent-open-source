"""The review notification outbox.

`DEV-028`, technical design 7.7 steps 4 and 6. A created review card is queued
for the enrolled devices; a worker later hands each queued row to a push
provider. Two separations carry the whole design:

- **queuing is not sending, and sending is not seeing.** A row moves to
  `provider_accepted` when APNs takes it, and nothing here ever writes
  `reviewed`. Only `POST /v1/daily-reviews/{id}/ack` does that, because the
  provider cannot tell us whether Henson looked at the card. The outbox is
  therefore never consulted to decide whether a review was handled;
- **the payload is a count, never an amount.** Design 7.7 step 4 limits the push
  body to how many entries there are. Anything more would put ledger data in a
  lock-screen banner and in Apple's infrastructure, so `PushNotification` is
  given the count and the review id and is structurally incapable of carrying a
  name or a sum.

There is no APNs client here on purpose. The Apple bundle id and APNs
entitlement are external inputs this project does not have yet (`DEV-029`), so
the sender is an injected protocol and the only implementation shipped is one
that refuses honestly rather than pretending to deliver.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent.storage.models import (
    DailyReview,
    DailyReviewItem,
    Device,
    NotificationOutbox,
)
from personal_agent_core.ids import new_id


#: After this many failed attempts a row stops being retried. The card is still
#: in the app; a push is a convenience, and retrying forever would keep a dead
#: token alive in the queue indefinitely.
MAX_ATTEMPTS: int = 5

#: Backoff per attempt, capped. Deliberately coarse: the review is a daily
#: card, so there is nothing to gain from retrying aggressively.
_BACKOFF: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
)


@dataclass(frozen=True)
class PushNotification:
    """Everything a provider is allowed to know about a review card."""

    device_id: str
    review_id: str
    #: How many entries are on the card. Never what they were.
    item_count: int


class PushSendError(RuntimeError):
    """The provider did not accept this notification."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        #: A rejected token or an unregistered device will not succeed later.
        self.permanent = permanent


class PushSender(Protocol):
    """Hand one notification to the push provider.

    Returning normally means the *provider accepted it*, which is all a provider
    can ever tell us. Raising `PushSendError` means it did not.
    """

    def __call__(self, notification: PushNotification) -> None: ...


class UnavailablePushSender:
    """The only sender that exists until APNs credentials do.

    It fails permanently rather than silently succeeding, so a queued row stays
    honestly undelivered instead of being recorded as accepted by a provider
    that was never contacted.
    """

    def __call__(self, notification: PushNotification) -> None:
        raise PushSendError(
            "no push provider is configured: APNs credentials are a DEV-029 "
            "external input",
            permanent=True,
        )


def enqueue_review_notification(
    session: Session,
    *,
    review_id: str,
    now: datetime,
    requeue_existing: bool = False,
) -> list[NotificationOutbox]:
    """Queue one notification per active device that can receive one.

    A device with no push token is skipped rather than queued: a row that can
    never be sent is not a pending notification, it is noise in a table used to
    tell whether anything is outstanding.
    """
    devices = list(
        session.scalars(
            select(Device).where(
                Device.status == "active",
                Device.encrypted_push_token.is_not(None),
            )
        )
    )
    existing = {
        row.device_id: row
        for row in session.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.review_id == review_id
            )
        )
    }

    queued: list[NotificationOutbox] = []
    for device in devices:
        current = existing.get(device.device_id)
        if current is not None:
            if requeue_existing and current.provider_status == "provider_accepted":
                current.provider_status = "pending"
                current.attempts = 0
                current.next_attempt_at = now
                queued.append(current)
            continue
        row = NotificationOutbox(
            event_id=new_id(),
            device_id=device.device_id,
            review_id=review_id,
            provider_status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
        session.add(row)
        queued.append(row)
    return queued


def deliver_pending(
    session: Session,
    send: PushSender,
    *,
    now: datetime,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[NotificationOutbox]:
    """Attempt every due notification once. Returns the rows that were tried.

    Each row is attempted at most once per call, and a failure schedules the
    next attempt rather than looping here: a provider that is down should not be
    hammered inside one job run.

    The caller commits once, after the loop. That is safe only while the sender
    reaches nothing: `UnavailablePushSender` is the only one shipped. Wiring a
    real APNs sender must also split this so no transaction stays open across a
    provider call -- otherwise a concurrent commit costs the whole batch its
    delivery records after APNs already accepted them, and the next run sends
    them again.
    """
    due = list(
        session.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.provider_status == "pending",
                NotificationOutbox.next_attempt_at.is_not(None),
                NotificationOutbox.next_attempt_at <= now,
            )
        )
    )

    attempted: list[NotificationOutbox] = []
    for row in due:
        device = session.get(Device, row.device_id)
        if (
            device is None
            or device.status != "active"
            or device.encrypted_push_token is None
            or row.review_id is None
        ):
            row.provider_status = "undeliverable"
            row.next_attempt_at = None
            continue
        count = _item_count(session, row.review_id)
        row.attempts += 1
        attempted.append(row)
        try:
            send(
                PushNotification(
                    device_id=row.device_id,
                    review_id=row.review_id or "",
                    item_count=count,
                )
            )
        except PushSendError as exc:
            if exc.permanent or row.attempts >= max_attempts:
                row.provider_status = "undeliverable"
                row.next_attempt_at = None
            else:
                row.next_attempt_at = now + _backoff_for(row.attempts)
            continue
        # Accepted by the provider. That is not delivery, and it is certainly
        # not review: `daily_reviews.status` is untouched here.
        row.provider_status = "provider_accepted"
        row.next_attempt_at = None

    return attempted


def _item_count(session: Session, review_id: str | None) -> int:
    if review_id is None:
        return 0
    return len(
        list(
            session.scalars(
                select(DailyReviewItem).where(
                    DailyReviewItem.review_id == review_id
                )
            )
        )
    )


def _backoff_for(attempts: int) -> timedelta:
    index = min(attempts, len(_BACKOFF)) - 1
    return _BACKOFF[max(index, 0)]


def pending_review_ids(session: Session) -> list[str]:
    """Review ids still awaiting human acknowledgement, oldest first."""
    return [
        review.review_id
        for review in session.scalars(
            select(DailyReview)
            .where(DailyReview.status.in_(("pending", "deferred")))
            .order_by(DailyReview.review_date)
        )
    ]
