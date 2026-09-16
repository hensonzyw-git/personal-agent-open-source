"""DEV-028 slice F: the scheduled job that ties the review pieces together.

The job is driven against a real Agent database with a fake control plane. The
cases are the operational ones: a nightly run, a run after an outage, a rerun on
the same night, and -- the reason this job commits per day rather than per run --
a control plane that fails on one day in the middle of the window.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from personal_agent.api import events
from personal_agent.api.control_client import (
    ControlPlaneError,
    RecordFields,
    SuccessfulWrite,
)
from personal_agent.api.daily_review import ReviewResult
from personal_agent.api.notifications import PushNotification, PushSendError
from personal_agent.api.review_job import run_daily_review
from personal_agent.context.config import default_context_config
from personal_agent.context.session_manager import SessionManager
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
TODAY = date(2026, 7, 26)
KEYRING = KeyRing([generate_key("data-2026-01")], service="personal-agent-api")


def write(record_id: str) -> SuccessfulWrite:
    return SuccessfulWrite(
        tool="finance.log_expense",
        table_kind="expense",
        record_id=record_id,
        committed_at="2026-07-25T06:00:00.000000Z",
    )


class FakeControl:
    """Stands in for `FinanceControlClient`, without any HTTP."""

    def __init__(self, by_day=None, *, broken_on: set[str] | None = None) -> None:
        self.by_day = by_day or {}
        self.broken_on = broken_on or set()
        self.asked: list[str] = []

    async def list_successful_writes(self, write_date: str):
        self.asked.append(write_date)
        if write_date in self.broken_on:
            raise ControlPlaneError("finance is down")
        return list(self.by_day.get(write_date, []))


class Recorder:
    def __init__(self) -> None:
        self.seen: list[PushNotification] = []

    def __call__(self, notification: PushNotification) -> None:
        self.seen.append(notification)


class Rejecting:
    def __call__(self, notification: PushNotification) -> None:
        raise PushSendError("nope", permanent=False)


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    factory = session_factory(engine)
    with factory() as session:
        session.add(
            Device(
                device_id="dev-1",
                display_name="iPhone",
                public_key="pk",
                device_key_thumbprint="tp",
                encrypted_push_token=KEYRING.encrypt(
                    b"apns", table="devices", column="encrypted_push_token", row_id="dev-1"
                ),
                status="active",
                scopes="[]",
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.commit()
    yield factory
    engine.dispose()


def run(sessions, control, *, send=None, today=TODAY, max_days=7):
    return run_daily_review(
        sessions,
        control,
        today=today,
        now=lambda: NOW,
        send=send,
        max_days=max_days,
    )


def run_with_timeline(sessions, control, *, send=None, today=TODAY, max_days=7):
    """The same run, wired to seal the frozen `daily_review` Timeline card."""
    return run_daily_review(
        sessions,
        control,
        today=today,
        now=lambda: NOW,
        send=send,
        max_days=max_days,
        keyring=KEYRING,
        session_manager=SessionManager(default_context_config()),
    )


def timeline_entries(sessions):
    with sessions() as session:
        timeline_id = events.canonical_timeline_id(session, now=NOW)
        return events.list_timeline(session, KEYRING, conversation_id=timeline_id)


class ValueControl(FakeControl):
    """A control plane that also answers the review's value read."""

    async def get_record_fields_batch(self, records):
        return [
            RecordFields(
                table_kind=table_kind,
                record_id=record_id,
                values={"name": record_id, "amount": "12.00"},
                unreadable_fields=(),
            )
            for table_kind, record_id in records
        ]


def reviews(sessions) -> list[DailyReview]:
    with sessions() as session:
        return list(session.scalars(select(DailyReview)))


def outbox(sessions) -> list[NotificationOutbox]:
    with sessions() as session:
        return list(session.scalars(select(NotificationOutbox)))


def test_the_nightly_run_builds_yesterday_and_queues_one_push(sessions) -> None:
    control = FakeControl({"2026-07-25": [write("recA"), write("recB")]})
    sender = Recorder()

    report = run(sessions, control, send=sender)

    assert [o.review_date for o in report.created] == ["2026-07-25"]
    assert len(reviews(sessions)) == 1
    assert [row.provider_status for row in outbox(sessions)] == ["provider_accepted"]
    assert report.queued_notifications == 1
    assert sender.seen[0].item_count == 2


def test_a_quiet_week_creates_no_card_and_sends_nothing(sessions) -> None:
    control = FakeControl({})
    sender = Recorder()

    report = run(sessions, control, send=sender)

    assert report.created == []
    assert reviews(sessions) == []
    assert outbox(sessions) == []
    assert sender.seen == []
    # Every day in the window was still asked about.
    assert len(control.asked) == 7


def test_a_rerun_the_same_night_changes_nothing(sessions) -> None:
    control = FakeControl({"2026-07-25": [write("recA")]})
    sender = Recorder()

    run(sessions, control, send=sender)
    second = run(sessions, control, send=sender)

    assert second.created == []
    assert len(reviews(sessions)) == 1
    assert len(outbox(sessions)) == 1
    assert len(sender.seen) == 1


def test_a_late_success_reopens_the_card_and_requeues_the_push(sessions) -> None:
    control = FakeControl({"2026-07-25": [write("recA")]})
    sender = Recorder()
    run(sessions, control, send=sender)
    with sessions() as session:
        card = session.scalars(select(DailyReview)).one()
        card.status = "reviewed"
        card.reviewed_at = NOW
        session.commit()

    control.by_day["2026-07-25"].append(write("recLate"))
    second = run(sessions, control, send=sender)

    assert [outcome.result for outcome in second.created] == [ReviewResult.UPDATED]
    with sessions() as session:
        card = session.scalars(select(DailyReview)).one()
        assert card.status == "pending"
        assert card.reviewed_at is None
        assert {
            item.record_id for item in session.scalars(select(DailyReviewItem))
        } == {"recA", "recLate"}
    assert len(sender.seen) == 2
    assert sender.seen[-1].item_count == 2


def test_a_restart_compensates_the_missed_days(sessions) -> None:
    control = FakeControl(
        {
            "2026-07-21": [write("rec21")],
            "2026-07-23": [write("rec23")],
            "2026-07-25": [write("rec25")],
        }
    )

    report = run(sessions, control, send=Recorder())

    assert [o.review_date for o in report.created] == [
        "2026-07-21",
        "2026-07-23",
        "2026-07-25",
    ]
    assert len(outbox(sessions)) == 3


def test_a_day_that_cannot_be_read_does_not_discard_the_earlier_days(
    sessions,
) -> None:
    """This is why the job commits per day instead of per run."""
    control = FakeControl(
        {
            "2026-07-21": [write("rec21")],
            "2026-07-23": [write("rec23")],
            "2026-07-25": [write("rec25")],
        },
        broken_on={"2026-07-23"},
    )

    report = run(sessions, control, send=Recorder())

    assert report.stopped_early_on == "2026-07-23"
    assert [r.review_date for r in reviews(sessions)] == ["2026-07-21"]
    # It stopped rather than skipping ahead, so the order stays chronological.
    assert "2026-07-24" not in control.asked

    # The next run picks up where it left off once Finance is back.
    control.broken_on.clear()
    second = run(sessions, control, send=Recorder())

    assert [o.review_date for o in second.created] == ["2026-07-23", "2026-07-25"]
    assert {r.review_date for r in reviews(sessions)} == {
        "2026-07-21",
        "2026-07-23",
        "2026-07-25",
    }


def test_a_failed_push_leaves_the_card_intact(sessions) -> None:
    control = FakeControl({"2026-07-25": [write("recA")]})

    report = run(sessions, control, send=Rejecting())

    assert len(report.created) == 1
    assert reviews(sessions)[0].status == "pending"
    row = outbox(sessions)[0]
    assert row.provider_status == "pending"
    assert row.attempts == 1


def test_a_card_with_no_enrolled_device_queues_nothing(sessions) -> None:
    """Today's real state: a card exists, and no push can go anywhere."""
    control = FakeControl({"2026-07-25": [write("recA")]})
    with sessions() as session:
        session.delete(session.get(Device, "dev-1"))
        session.commit()

    report = run(sessions, control, send=Recorder())

    assert len(report.created) == 1
    assert report.queued_notifications == 0
    assert outbox(sessions) == []


def test_the_default_sender_does_not_pretend_to_deliver(sessions) -> None:
    """No APNs credentials exist yet; the run must say so, not fake success."""
    control = FakeControl({"2026-07-25": [write("recA")]})

    run(sessions, control)

    assert outbox(sessions)[0].provider_status == "undeliverable"
    assert reviews(sessions)[0].status == "pending"


def test_a_pending_notification_from_an_earlier_night_is_retried(sessions) -> None:
    control = FakeControl({"2026-07-25": [write("recA")]})
    run(sessions, control, send=Rejecting())

    later = run_daily_review(
        sessions,
        control,
        today=TODAY,
        now=lambda: NOW + timedelta(hours=6),
        send=Recorder(),
    )

    assert later.attempted_notifications == 1
    assert outbox(sessions)[0].provider_status == "provider_accepted"


def test_the_ledger_day_is_derived_from_asia_shanghai(sessions) -> None:
    """`now` is UTC; the day the job compensates is the local one."""
    control = FakeControl({"2026-07-25": [write("recA")]})

    # 2026-07-26 00:30 Asia/Shanghai is 2026-07-25 16:30 UTC, so "yesterday"
    # must still be the 25th.
    run_daily_review(
        sessions,
        control,
        now=lambda: datetime(2026, 7, 25, 16, 30, tzinfo=timezone.utc),
        send=Recorder(),
    )

    assert [r.review_date for r in reviews(sessions)] == ["2026-07-25"]


# --- the frozen Timeline card (design `1j`) ----------------------------------


def test_a_created_card_is_sealed_onto_the_timeline(sessions) -> None:
    control = ValueControl({"2026-07-25": [write("recA"), write("recB")]})

    report = run_with_timeline(sessions, control, send=Recorder())

    assert report.emitted_review_events
    entries = timeline_entries(sessions)
    assert [entry.event_type for entry in entries] == ["daily_review"]
    content = entries[0].content
    assert content["review_date"] == "2026-07-25"
    assert content["item_count"] == 2
    assert [item["record_id"] for item in content["items"]] == ["recA", "recB"]
    # The values are frozen at build time, exactly as read once from the control
    # plane -- not pointers, and not read again on later runs.
    assert content["items"][0]["values"]["amount"] == "12.00"
    with sessions() as session:
        review = session.scalars(select(DailyReview)).one()
        assert review.timeline_event_id == entries[0].event_id


def test_a_rerun_does_not_duplicate_the_timeline_card(sessions) -> None:
    control = ValueControl({"2026-07-25": [write("recA")]})

    run_with_timeline(sessions, control, send=Recorder())
    second = run_with_timeline(sessions, control, send=Recorder())

    assert second.emitted_review_events == []
    assert len(timeline_entries(sessions)) == 1


def test_a_late_success_seals_a_newer_snapshot(sessions) -> None:
    control = ValueControl({"2026-07-25": [write("recA")]})
    run_with_timeline(sessions, control, send=Recorder())
    with sessions() as session:
        card = session.scalars(select(DailyReview)).one()
        card.status = "reviewed"
        card.reviewed_at = NOW
        session.commit()

    control.by_day["2026-07-25"].append(write("recLate"))
    second = run_with_timeline(sessions, control, send=Recorder())

    assert [outcome.result for outcome in second.created] == [ReviewResult.UPDATED]
    assert second.emitted_review_events
    entries = timeline_entries(sessions)
    assert len(entries) == 2
    # The newest snapshot carries the late write; the older one stays sealed.
    assert entries[-1].content["item_count"] == 2
    with sessions() as session:
        review = session.scalars(select(DailyReview)).one()
        assert review.timeline_event_id == entries[-1].event_id


def test_a_review_whose_event_was_lost_is_repaired(sessions) -> None:
    """A crash between the review-row commit and the event append is repaired.

    Simulated by building without the Timeline machinery -- the card and its push
    exist but no event does -- then running the wired job again.
    """
    control = ValueControl({"2026-07-25": [write("recA")]})
    run(sessions, control, send=Recorder())
    assert timeline_entries(sessions) == []
    with sessions() as session:
        assert session.scalars(select(DailyReview)).one().timeline_event_id is None

    second = run_with_timeline(sessions, control, send=Recorder())

    assert second.emitted_review_events
    entries = timeline_entries(sessions)
    assert [entry.event_type for entry in entries] == ["daily_review"]
    assert entries[0].content["review_date"] == "2026-07-25"
