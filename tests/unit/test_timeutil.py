"""DEV-003: ledger dates resolve in Asia/Shanghai, instants stay aware UTC."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from personal_agent_core.timeutil import (
    LEDGER_TIMEZONE,
    NaiveDatetimeError,
    format_ledger_date,
    ledger_date,
    ledger_day_epoch_millis,
    ledger_day_start_utc,
    parse_ledger_date,
    parse_rfc3339,
    relative_ledger_date,
    to_rfc3339,
    utc_now,
)


def test_utc_now_is_aware_and_utc() -> None:
    moment = utc_now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 7, 23, 12, 0, 0), "2026-07-23", None],
)
def test_naive_or_wrong_types_are_rejected(value: object) -> None:
    with pytest.raises((NaiveDatetimeError, ValueError)):
        ledger_date(value)  # type: ignore[arg-type]


def test_ledger_date_uses_shanghai_not_utc() -> None:
    # 16:10 UTC is already the next calendar day in Shanghai.
    moment = datetime(2026, 7, 23, 16, 10, tzinfo=timezone.utc)
    assert moment.date() == date(2026, 7, 23)
    assert ledger_date(moment) == date(2026, 7, 24)


def test_relative_dates_cross_the_year_boundary() -> None:
    just_after_new_year = datetime(2026, 1, 1, 0, 30, tzinfo=LEDGER_TIMEZONE)
    assert relative_ledger_date(just_after_new_year, 0) == date(2026, 1, 1)
    assert relative_ledger_date(just_after_new_year, 1) == date(2025, 12, 31)
    assert relative_ledger_date(just_after_new_year, 2) == date(2025, 12, 30)


def test_relative_dates_never_resolve_into_the_future() -> None:
    with pytest.raises(ValueError):
        relative_ledger_date(utc_now(), -1)


def test_day_start_is_shanghai_midnight_expressed_in_utc() -> None:
    start = ledger_day_start_utc(date(2026, 7, 23))
    assert start == datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)
    assert start.astimezone(LEDGER_TIMEZONE).hour == 0


def test_epoch_millis_round_trip_to_the_same_ledger_date() -> None:
    day = date(2026, 7, 23)
    millis = ledger_day_epoch_millis(day)
    restored = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    assert ledger_date(restored) == day


def test_rfc3339_round_trip_is_utc_normalised() -> None:
    shanghai_noon = datetime(2026, 7, 23, 12, 10, tzinfo=LEDGER_TIMEZONE)
    text = to_rfc3339(shanghai_noon)
    assert text == "2026-07-23T04:10:00Z"
    assert parse_rfc3339(text) == shanghai_noon


def test_ledger_date_strings_are_strict() -> None:
    assert format_ledger_date(date(2026, 7, 23)) == "2026-07-23"
    assert parse_ledger_date("2026-07-23") == date(2026, 7, 23)
    for bad in ["2026-7-23", "23/07/2026", "2026-07-23T00:00:00", 20260723]:
        with pytest.raises(ValueError):
            parse_ledger_date(bad)  # type: ignore[arg-type]


def test_datetime_is_not_accepted_where_a_date_is_required() -> None:
    with pytest.raises(ValueError):
        format_ledger_date(utc_now())  # type: ignore[arg-type]
