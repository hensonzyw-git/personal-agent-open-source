"""Time and ledger-date helpers.

Two rules from the PRD and the technical design drive this module:

- every stored instant is UTC and aware; naive datetimes are rejected;
- every ledger *date* is resolved in `Asia/Shanghai`, from the moment the server
  received the message, not from a client supplied clock.

`occurred_on` is the actual payment or entry date. Paying today for a future
trip is still recorded today, so nothing here looks ahead.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Final
from zoneinfo import ZoneInfo


LEDGER_TIMEZONE: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")
LEDGER_TIMEZONE_NAME: Final[str] = "Asia/Shanghai"


class NaiveDatetimeError(ValueError):
    """A datetime without tzinfo was supplied where an instant was required."""


def _require_aware(moment: datetime) -> datetime:
    if not isinstance(moment, datetime):
        raise NaiveDatetimeError(
            f"expected datetime, got {type(moment).__name__}"
        )
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise NaiveDatetimeError(
            "naive datetimes are ambiguous; supply an aware UTC instant"
        )
    return moment


def utc_now() -> datetime:
    """Current instant as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_utc(moment: datetime) -> datetime:
    """Normalise any aware datetime to UTC."""
    return _require_aware(moment).astimezone(timezone.utc)


def to_rfc3339(moment: datetime) -> str:
    """Serialise an aware instant as RFC 3339 UTC with a `Z` suffix."""
    return to_utc(moment).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_rfc3339(text: str) -> datetime:
    """Parse an RFC 3339 timestamp into an aware UTC datetime."""
    if not isinstance(text, str):
        raise ValueError(f"expected RFC 3339 string, got {type(text).__name__}")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(candidate)
    return to_utc(parsed)


def ledger_date(moment: datetime) -> date:
    """The `Asia/Shanghai` calendar date an instant falls on."""
    return _require_aware(moment).astimezone(LEDGER_TIMEZONE).date()


def relative_ledger_date(moment: datetime, days_ago: int) -> date:
    """Resolve `今天` / `昨天` / `前天` to an absolute date.

    Subtracting whole days from the resolved local date, rather than from the
    instant, keeps year and month boundaries correct: `昨天` on 1 January 2026
    is 31 December 2025.
    """
    if not isinstance(days_ago, int) or isinstance(days_ago, bool):
        raise ValueError(f"days_ago must be an int, got {days_ago!r}")
    if days_ago < 0:
        raise ValueError("relative ledger dates never resolve into the future")
    return ledger_date(moment) - timedelta(days=days_ago)


def ledger_day_start_utc(day: date) -> datetime:
    """Midnight of a ledger date in `Asia/Shanghai`, expressed in UTC."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError(f"expected a date, got {type(day).__name__}")
    local_midnight = datetime(
        day.year, day.month, day.day, tzinfo=LEDGER_TIMEZONE
    )
    return local_midnight.astimezone(timezone.utc)


def ledger_day_epoch_millis(day: date) -> int:
    """Epoch milliseconds for a ledger date, as Feishu datetime fields expect.

    The connector always writes the date explicitly, even though the Feishu
    field has a creation-time default, so that `昨天` and `前天` land correctly.
    """
    return int(ledger_day_start_utc(day).timestamp() * 1000)


def format_ledger_date(day: date) -> str:
    """Render `YYYY-MM-DD`, the only date shape the MCP contract accepts."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError(f"expected a date, got {type(day).__name__}")
    return day.isoformat()


def parse_ledger_date(text: str) -> date:
    """Parse a strict `YYYY-MM-DD` absolute date."""
    if not isinstance(text, str) or len(text) != 10:
        raise ValueError(f"expected a YYYY-MM-DD date, got {text!r}")
    return date.fromisoformat(text)
