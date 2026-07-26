"""DEV-028 slice C: building the daily review card.

Design 7.7 is mostly a list of things this job must *not* do, so the cases are
the refusals: no card on a day with no writes, never a second card for a day,
never losing a late-verified write behind an existing card, and never turning
an unreadable control plane into a quiet day.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from personal_agent.api.control_client import ControlPlaneError, SuccessfulWrite
from personal_agent.api.daily_review import (
    MAX_CATCH_UP_DAYS,
    ReviewResult,
    build_review,
    catch_up_reviews,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import DailyReview, DailyReviewItem


NOW = datetime(2026, 7, 26, 16, 5, tzinfo=timezone.utc)
YESTERDAY = date(2026, 7, 25)


def write(
    record_id: str,
    *,
    tool: str = "finance.log_expense",
    table_kind: str = "expense",
) -> SuccessfulWrite:
    return SuccessfulWrite(
        tool=tool,
        table_kind=table_kind,
        record_id=record_id,
        committed_at="2026-07-25T06:00:00.000000Z",
    )


class Reader:
    """A control-plane stand-in that records which days were asked for."""

    def __init__(self, by_day: dict[str, list[SuccessfulWrite]] | None = None):
        self.by_day = by_day or {}
        self.asked: list[str] = []

    def __call__(self, review_date: str) -> list[SuccessfulWrite]:
        self.asked.append(review_date)
        return list(self.by_day.get(review_date, []))


class BrokenReader:
    def __call__(self, review_date: str) -> list[SuccessfulWrite]:
        raise ControlPlaneError("finance is down")


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


def reviews(session) -> list[DailyReview]:
    return list(session.scalars(select(DailyReview)))


def items(session, review_id: str) -> list[DailyReviewItem]:
    return list(
        session.scalars(
            select(DailyReviewItem).where(DailyReviewItem.review_id == review_id)
        )
    )


def test_a_day_with_writes_becomes_one_pending_card(sessions) -> None:
    reader = Reader({"2026-07-25": [write("recA"), write("recB")]})

    with sessions() as session:
        outcome = build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()

        assert outcome.result is ReviewResult.CREATED
        assert outcome.item_count == 2
        assert outcome.should_notify is True
        card = reviews(session)[0]
        assert card.status == "pending"
        assert card.review_date == "2026-07-25"
        assert {item.record_id for item in items(session, card.review_id)} == {
            "recA",
            "recB",
        }


def test_the_card_stores_pointers_not_values(sessions) -> None:
    """A cached amount would show the value at write time, not the corrected one."""
    reader = Reader({"2026-07-25": [write("recA")]})

    with sessions() as session:
        build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()
        item = items(session, reviews(session)[0].review_id)[0]

    columns = {column.name for column in DailyReviewItem.__table__.columns}
    assert columns == {"item_id", "review_id", "tool", "record_id", "committed_at"}
    assert item.committed_at == datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)


def test_a_day_with_no_writes_creates_nothing(sessions) -> None:
    """Design 7.7 step 3: no writes means no review and no push."""
    reader = Reader({})

    with sessions() as session:
        outcome = build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()

        assert outcome.result is ReviewResult.NO_WRITES
        assert outcome.should_notify is False
        assert reviews(session) == []


def test_a_second_run_for_the_same_day_creates_no_second_card(sessions) -> None:
    reader = Reader({"2026-07-25": [write("recA")]})

    with sessions() as session:
        first = build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()
        second = build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()

        assert first.result is ReviewResult.CREATED
        assert second.result is ReviewResult.ALREADY_EXISTS
        assert second.review_id == first.review_id
        assert second.should_notify is False
        assert len(reviews(session)) == 1


def test_a_later_verified_write_reopens_and_extends_the_card(sessions) -> None:
    reader = Reader({"2026-07-25": [write("recA")]})

    with sessions() as session:
        first = build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()
        card = reviews(session)[0]
        card.status = "reviewed"
        card.reviewed_at = NOW
        session.commit()
        reader.by_day["2026-07-25"].append(write("recLate"))
        updated = build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()

        card = reviews(session)[0]
        assert {item.record_id for item in items(session, card.review_id)} == {
            "recA",
            "recLate",
        }
        assert first.result is ReviewResult.CREATED
        assert updated.result is ReviewResult.UPDATED
        assert card.status == "pending"
        assert card.reviewed_at is None


def test_an_existing_card_is_requeried_to_find_late_writes(sessions) -> None:
    reader = Reader({"2026-07-25": [write("recA")]})

    with sessions() as session:
        build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()
        reader.asked.clear()
        build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()

    assert reader.asked == ["2026-07-25"]


def test_a_repeated_record_id_lands_on_the_card_once(sessions) -> None:
    reader = Reader({"2026-07-25": [write("recA"), write("recA")]})

    with sessions() as session:
        outcome = build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()

        assert outcome.item_count == 1
        assert len(items(session, outcome.review_id)) == 1


def test_the_same_record_id_in_two_tables_keeps_both_items(sessions) -> None:
    reader = Reader(
        {
            "2026-07-25": [
                write("recSame"),
                write(
                    "recSame",
                    tool="finance.log_income",
                    table_kind="income",
                ),
            ]
        }
    )

    with sessions() as session:
        outcome = build_review(session, reader, day=YESTERDAY, now=NOW)
        session.commit()
        stored = items(session, outcome.review_id)

    assert {(item.tool, item.record_id) for item in stored} == {
        ("finance.log_expense", "recSame"),
        ("finance.log_income", "recSame"),
    }


def test_a_mismatched_tool_and_table_is_refused(sessions) -> None:
    reader = Reader(
        {
            "2026-07-25": [
                write(
                    "recA",
                    tool="finance.log_income",
                    table_kind="expense",
                )
            ]
        }
    )
    with sessions() as session:
        with pytest.raises(ControlPlaneError):
            build_review(session, reader, day=YESTERDAY, now=NOW)


def test_an_unreadable_control_plane_is_not_a_quiet_day(sessions) -> None:
    with sessions() as session:
        with pytest.raises(ControlPlaneError):
            build_review(session, BrokenReader(), day=YESTERDAY, now=NOW)
        session.rollback()

        assert reviews(session) == []


def test_an_unparseable_commit_instant_refuses_rather_than_using_now(
    sessions,
) -> None:
    reader = Reader(
        {
            "2026-07-25": [
                SuccessfulWrite(
                    tool="finance.log_expense",
                    table_kind="expense",
                    record_id="recA",
                    committed_at="yesterday afternoon",
                )
            ]
        }
    )

    with sessions() as session:
        with pytest.raises(ValueError):
            build_review(session, reader, day=YESTERDAY, now=NOW)
        session.rollback()

        assert reviews(session) == []


def test_two_runs_racing_on_the_same_day_still_produce_one_card(sessions) -> None:
    """The unique key, not the pre-check, is what makes this safe.

    The interleave is forced deterministically: the second run commits its card
    while the first is still inside its own control read, so the first run's
    pre-check has already passed and only the constraint can stop it.
    """
    other = Reader({"2026-07-25": [write("recB")]})

    def racing_reader(review_date: str) -> list[SuccessfulWrite]:
        with sessions() as competitor:
            build_review(competitor, other, day=YESTERDAY, now=NOW)
            competitor.commit()
        return [write("recA")]

    with sessions() as session:
        outcome = build_review(session, racing_reader, day=YESTERDAY, now=NOW)
        session.commit()

        assert outcome.result is ReviewResult.ALREADY_EXISTS
        assert outcome.should_notify is False
        assert len(reviews(session)) == 1
        # The winner's card is intact; the loser wrote nothing.
        card = reviews(session)[0]
        assert {item.record_id for item in items(session, card.review_id)} == {
            "recB"
        }


# --- catch-up ----------------------------------------------------------------


def test_catch_up_covers_the_last_seven_completed_days_oldest_first(
    sessions,
) -> None:
    reader = Reader({})
    today = date(2026, 7, 26)

    with sessions() as session:
        catch_up_reviews(session, reader, today=today, now=NOW)
        session.commit()

    assert reader.asked == [
        "2026-07-19",
        "2026-07-20",
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
        "2026-07-25",
    ]
    assert len(reader.asked) == MAX_CATCH_UP_DAYS
    # Today is not over yet, so it is never reviewed.
    assert "2026-07-26" not in reader.asked


def test_catch_up_fills_only_the_missing_days(sessions) -> None:
    reader = Reader(
        {
            "2026-07-24": [write("recOld")],
            "2026-07-25": [write("recNew")],
        }
    )
    today = date(2026, 7, 26)

    with sessions() as session:
        build_review(session, reader, day=date(2026, 7, 24), now=NOW)
        session.commit()
        outcomes = catch_up_reviews(session, reader, today=today, now=NOW)
        session.commit()

        by_date = {o.review_date: o.result for o in outcomes}
        assert by_date["2026-07-24"] is ReviewResult.ALREADY_EXISTS
        assert by_date["2026-07-25"] is ReviewResult.CREATED
        assert by_date["2026-07-23"] is ReviewResult.NO_WRITES
        assert len(reviews(session)) == 2


def test_catch_up_never_reaches_back_further_than_a_week(sessions) -> None:
    reader = Reader({"2026-07-18": [write("recAncient")]})
    today = date(2026, 7, 26)

    with sessions() as session:
        catch_up_reviews(session, reader, today=today, now=NOW)
        session.commit()

        assert reviews(session) == []
