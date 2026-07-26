"""DEV-028 slice E: the review notification outbox.

Design 7.7 steps 4 and 6 draw two lines this suite exists to hold: a push body
carries only a count, and a provider accepting a notification is not the user
having reviewed anything. The rest of the cases are the ones where an outbox
usually goes wrong -- a retry that becomes a second push, a dead token retried
forever, and a device that was revoked after the card was queued.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from personal_agent.api.notifications import (
    MAX_ATTEMPTS,
    PushNotification,
    PushSendError,
    UnavailablePushSender,
    deliver_pending,
    enqueue_review_notification,
    pending_review_ids,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    DailyReview,
    DailyReviewItem,
    Device,
    NotificationOutbox,
)
from personal_agent_core.crypto import KeyRing, generate_key


NOW = datetime(2026, 7, 26, 16, 5, tzinfo=timezone.utc)
KEYRING = KeyRing([generate_key("data-2026-01")], service="personal-agent-api")


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


def add_device(
    session, device_id: str, *, with_token: bool = True, status: str = "active"
) -> None:
    session.add(
        Device(
            device_id=device_id,
            display_name=device_id,
            public_key="pk",
            device_key_thumbprint=f"tp-{device_id}",
            encrypted_push_token=(
                KEYRING.encrypt(
                    b"apns-token",
                    table="devices",
                    column="encrypted_push_token",
                    row_id=device_id,
                )
                if with_token
                else None
            ),
            status=status,
            scopes="[]",
            allowed_tools_version="v1",
            created_at=NOW,
            revoked_at=None if status == "active" else NOW,
        )
    )


def add_review(session, review_id: str, *, items: int = 2) -> None:
    session.add(
        DailyReview(
            review_id=review_id,
            review_date="2026-07-25",
            status="pending",
            created_at=NOW,
        )
    )
    for index in range(items):
        session.add(
            DailyReviewItem(
                item_id=f"{review_id}-{index}",
                review_id=review_id,
                tool="finance.log_expense",
                record_id=f"rec{index}",
                committed_at=NOW,
            )
        )


def outbox(session) -> list[NotificationOutbox]:
    return list(session.scalars(select(NotificationOutbox)))


class Recorder:
    """Accepts everything, and remembers exactly what it was told."""

    def __init__(self) -> None:
        self.seen: list[PushNotification] = []

    def __call__(self, notification: PushNotification) -> None:
        self.seen.append(notification)


class Rejecting:
    def __init__(self, *, permanent: bool) -> None:
        self.calls = 0
        self.permanent = permanent

    def __call__(self, notification: PushNotification) -> None:
        self.calls += 1
        raise PushSendError("nope", permanent=self.permanent)


# --- queuing -----------------------------------------------------------------


def test_one_row_is_queued_per_active_device_with_a_token(sessions) -> None:
    with sessions() as session:
        add_device(session, "dev-1")
        add_device(session, "dev-2")
        add_device(session, "dev-no-token", with_token=False)
        add_device(session, "dev-revoked", status="revoked")
        add_review(session, "rev-1")
        session.commit()

        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        assert {row.device_id for row in outbox(session)} == {"dev-1", "dev-2"}
        assert {row.provider_status for row in outbox(session)} == {"pending"}


def test_queuing_the_same_review_twice_adds_nothing(sessions) -> None:
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()

        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        assert len(outbox(session)) == 1


# --- sending -----------------------------------------------------------------


def test_the_provider_is_told_a_count_and_nothing_else(sessions) -> None:
    """A lock-screen banner must never carry a name or an amount."""
    recorder = Recorder()
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1", items=3)
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        deliver_pending(session, recorder, now=NOW)
        session.commit()

    assert recorder.seen == [
        PushNotification(device_id="dev-1", review_id="rev-1", item_count=3)
    ]
    assert set(PushNotification.__dataclass_fields__) == {
        "device_id",
        "review_id",
        "item_count",
    }


def test_acceptance_is_recorded_as_acceptance_not_as_review(sessions) -> None:
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        deliver_pending(session, Recorder(), now=NOW)
        session.commit()

        assert outbox(session)[0].provider_status == "provider_accepted"
        # The card itself is untouched: only /ack marks it reviewed.
        assert session.get(DailyReview, "rev-1").status == "pending"
        assert session.get(DailyReview, "rev-1").reviewed_at is None
        assert pending_review_ids(session) == ["rev-1"]


def test_an_accepted_row_is_not_sent_again(sessions) -> None:
    recorder = Recorder()
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        deliver_pending(session, recorder, now=NOW)
        session.commit()
        deliver_pending(session, recorder, now=NOW + timedelta(hours=1))
        session.commit()

    assert len(recorder.seen) == 1


def test_a_device_revoked_after_queue_is_not_sent_to(sessions) -> None:
    recorder = Recorder()
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()
        device = session.get(Device, "dev-1")
        device.status = "revoked"
        device.revoked_at = NOW
        session.commit()

        attempted = deliver_pending(session, recorder, now=NOW)
        session.commit()

        assert attempted == []
        assert recorder.seen == []
        assert outbox(session)[0].provider_status == "undeliverable"


def test_a_transient_failure_backs_off_and_retries(sessions) -> None:
    rejecting = Rejecting(permanent=False)
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        deliver_pending(session, rejecting, now=NOW)
        session.commit()
        row = outbox(session)[0]
        assert row.provider_status == "pending"
        assert row.attempts == 1
        assert row.next_attempt_at > NOW

        # Not due yet: the backoff is real, not decorative.
        deliver_pending(session, rejecting, now=NOW + timedelta(seconds=10))
        session.commit()
        assert rejecting.calls == 1

        deliver_pending(session, rejecting, now=NOW + timedelta(hours=1))
        session.commit()
        assert rejecting.calls == 2


def test_a_permanent_rejection_is_not_retried(sessions) -> None:
    """A rejected token will not start working an hour later."""
    rejecting = Rejecting(permanent=True)
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        deliver_pending(session, rejecting, now=NOW)
        session.commit()
        assert outbox(session)[0].provider_status == "undeliverable"

        deliver_pending(session, rejecting, now=NOW + timedelta(days=1))
        session.commit()
        assert rejecting.calls == 1


def test_attempts_are_bounded(sessions) -> None:
    rejecting = Rejecting(permanent=False)
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        moment = NOW
        for _ in range(MAX_ATTEMPTS + 3):
            moment = moment + timedelta(days=1)
            deliver_pending(session, rejecting, now=moment)
            session.commit()

        row = outbox(session)[0]
        assert row.attempts == MAX_ATTEMPTS
        assert row.provider_status == "undeliverable"
        assert row.next_attempt_at is None


def test_the_only_shipped_sender_refuses_rather_than_claiming_delivery(
    sessions,
) -> None:
    """No APNs entitlement exists yet; silence must not look like success."""
    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()
        enqueue_review_notification(session, review_id="rev-1", now=NOW)
        session.commit()

        deliver_pending(session, UnavailablePushSender(), now=NOW)
        session.commit()

        assert outbox(session)[0].provider_status == "undeliverable"
        assert session.get(DailyReview, "rev-1").status == "pending"


def test_a_status_outside_the_closed_set_is_rejected_by_the_database(
    sessions,
) -> None:
    from sqlalchemy.exc import IntegrityError

    with sessions() as session:
        add_device(session, "dev-1")
        add_review(session, "rev-1")
        session.commit()
        session.add(
            NotificationOutbox(
                event_id="evt-1",
                device_id="dev-1",
                review_id="rev-1",
                provider_status="delivered",
                attempts=0,
                next_attempt_at=None,
                created_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
