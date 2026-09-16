"""The 00:00 job: build yesterday's card, compensate the last week, then push.

`DEV-028`, design 7.7. A systemd timer calls this once a day (`DEV-036` owns the
timer); running it more often, or twice at once, is safe because every step is
idempotent on `review_date` and on `(review_id, device_id)`.

The one design decision worth stating: **each day is committed on its own.**
Startup recovery rolls its whole scan back when the control plane fails, because
its rows are a single consistent projection. A week of review cards is not --
each day stands alone, and discarding four good cards because the fifth day
could not be read would mean a day that always fails could suppress every other
day forever. So a failing day is logged, left for the next run, and the loop
stops there rather than skipping ahead: the days are walked oldest first, and
stopping keeps the cards in the order they happened.

Sending is separated from building for the same reason it is separated in the
outbox: a push that cannot be delivered must not be able to undo a card that was
correctly created.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from personal_agent.api import events
from personal_agent.api.control_client import (
    ControlPlaneError,
    FinanceControlClient,
    SuccessfulWrite,
)
from personal_agent.api.review_view import review_detail
from personal_agent.storage.models import DailyReview
from personal_agent_core.sqlite import run_write_transaction
from personal_agent.api.daily_review import (
    MAX_CATCH_UP_DAYS,
    ReviewOutcome,
    ReviewResult,
    build_review,
    catch_up_days,
)
from personal_agent.api.notifications import (
    PushSender,
    UnavailablePushSender,
    deliver_pending,
    enqueue_review_notification,
)
from personal_agent_core.timeutil import ledger_date, utc_now


logger = logging.getLogger(__name__)


@dataclass
class ReviewRunReport:
    """What the run did, for the operator and for tests."""

    outcomes: list[ReviewOutcome] = field(default_factory=list)
    #: Reviews for which notifications were queued, whether or not any device
    #: could receive one.
    notified_reviews: list[str] = field(default_factory=list)
    #: Outbox rows actually created. Zero with no enrolled device, which is the
    #: current state of the world and must not read as "a push went out".
    queued_notifications: int = 0
    attempted_notifications: int = 0
    #: Review ids whose frozen Timeline card was sealed this run.
    emitted_review_events: list[str] = field(default_factory=list)
    stopped_early_on: str | None = None

    @property
    def created(self) -> list[ReviewOutcome]:
        return [outcome for outcome in self.outcomes if outcome.should_notify]


def run_daily_review(
    sessions: Callable[[], Any],
    control: FinanceControlClient,
    *,
    today: date | None = None,
    now: Callable[[], datetime] = utc_now,
    send: PushSender | None = None,
    run: Callable[[Any], Any] = asyncio.run,
    max_days: int = MAX_CATCH_UP_DAYS,
    keyring: Any | None = None,
    session_manager: Any | None = None,
) -> ReviewRunReport:
    """Build any missing cards, queue their notifications, and attempt delivery.

    When `keyring` and `session_manager` are both supplied the run also seals each
    created / reopened card as a frozen `daily_review` Timeline event, and repairs
    any card whose event is missing (a crash between the review-row commit and
    the event append). Without them the run is the pre-Timeline behaviour; the
    console entrypoint always supplies them.
    """
    moment = now()
    day_now = today if today is not None else ledger_date(moment)
    read_writes = _writes_reader(control, run)
    emit_events = keyring is not None and session_manager is not None
    report = ReviewRunReport()

    for day in catch_up_days(day_now, max_days):
        with sessions() as session:
            try:
                def build_day() -> ReviewOutcome:
                    """One day's card, re-runnable if another run raced us.

                    The only external call inside is the control plane's own
                    read of that day's verified writes, which is idempotent; a
                    retry repeats it rather than acting twice.
                    """
                    outcome = build_review(
                        session, read_writes, day=day, now=moment
                    )
                    if outcome.should_notify and outcome.review_id is not None:
                        queued = enqueue_review_notification(
                            session,
                            review_id=outcome.review_id,
                            now=moment,
                            requeue_existing=(
                                outcome.result is ReviewResult.UPDATED
                            ),
                        )
                        report.notified_reviews.append(outcome.review_id)
                        report.queued_notifications += len(queued)
                    return outcome

                outcome = run_write_transaction(session, build_day)
            except ControlPlaneError as exc:
                session.rollback()
                logger.warning(
                    "daily review could not read the Finance control plane for "
                    "%s (%s); this day and the ones after it are left for the "
                    "next run",
                    day.isoformat(),
                    type(exc).__name__,
                )
                report.stopped_early_on = day.isoformat()
                break
            except Exception:
                session.rollback()
                raise
        report.outcomes.append(outcome)

        # Seal the card after the build has committed: the value read is a Feishu
        # round-trip and must not sit inside the build transaction. Emitting for a
        # CREATED or UPDATED card, and for a card whose event is still owed, keeps
        # "read once at push time" true while the repair path closes the one crash
        # window that would otherwise leave a review with no Timeline entry at all.
        if emit_events and outcome.review_id is not None:
            if outcome.should_notify or _review_missing_event(
                sessions, outcome.review_id
            ):
                _emit_review_event(
                    sessions,
                    control,
                    run,
                    keyring,
                    session_manager,
                    review_id=outcome.review_id,
                    now=moment,
                )
                report.emitted_review_events.append(outcome.review_id)

    # `deliver_pending` commits each row itself, so no transaction is held
    # across a provider call. A real APNs sender crosses the network to Apple;
    # holding one transaction across N sends would let a concurrent commit cost
    # the whole batch its delivery records after Apple already accepted them.
    with sessions() as session:
        try:
            attempted = deliver_pending(
                session, send or UnavailablePushSender(), now=moment
            )
            report.attempted_notifications = len(attempted)
        except Exception:
            session.rollback()
            raise

    return report


def _review_missing_event(sessions: Callable[[], Any], review_id: str) -> bool:
    """Whether a review has no frozen Timeline card yet.

    Read fresh rather than through a caller's session: the build committed in a
    different session, and the identity map of any session that already read the
    row would not see the just-written `timeline_event_id`.
    """
    with sessions() as session:
        review = session.get(DailyReview, review_id)
        return review is not None and review.timeline_event_id is None


def _emit_review_event(
    sessions: Callable[[], Any],
    control: FinanceControlClient,
    run: Callable[[Any], Any],
    keyring: Any,
    session_manager: Any,
    *,
    review_id: str,
    now: datetime,
) -> str:
    """Seal one card's frozen snapshot onto the Timeline.

    Values are read once here and never again: the sealed event is what a client
    renders from now on. The read and the append are separate transactions
    because the read crosses into Feishu and must not be held open across it
    (rule 5.2).
    """

    def read_record(records: list[tuple[str, str]]):
        return run(control.get_record_fields_batch(records))

    with sessions() as session:
        detail = review_detail(session, review_id, read_record)

    content = {
        "review_id": detail["review_id"],
        "review_date": detail["review_date"],
        "item_count": detail["item_count"],
        "items": detail["items"],
    }

    # The append closure is defined inside the `with` block, exactly like
    # `build_day`, so the `session` it reads is unambiguously the write
    # transaction's session and not the one the value read used above.
    with sessions() as session:
        def append_and_mark() -> str:
            review = session.get(DailyReview, review_id)
            if review is None:
                raise ControlPlaneError(
                    "the review vanished before its card was sealed"
                )
            timeline_id = events.canonical_timeline_id(session, now=now)
            event_id = events.append_event(
                session,
                keyring,
                conversation_id=timeline_id,
                session_id=session_manager.system_event_session(
                    session, conversation_id=timeline_id, now=now
                ),
                turn_id=events.new_turn_id(),
                event_type=events.DAILY_REVIEW,
                content=content,
                operation_id=None,
                now=now,
            )
            review.timeline_event_id = event_id
            return event_id

        return run_write_transaction(session, append_and_mark)


def _writes_reader(
    control: FinanceControlClient, run: Callable[[Any], Any]
) -> Callable[[str], list[SuccessfulWrite]]:
    """Bridge the async control read into the synchronous review core."""

    def read(review_date: str) -> list[SuccessfulWrite]:
        return run(control.list_successful_writes(review_date))

    return read
