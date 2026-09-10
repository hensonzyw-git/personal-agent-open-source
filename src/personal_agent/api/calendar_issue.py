"""What must be true before a calendar action reaches the phone.

A device action is the authorisation record: the phone builds an `EKEvent`
from it and nothing re-derives anything. So this module does the two jobs that
cannot be left to the device or to the model:

- **input policy.** A model picks a calendar *name* from a closed enum and may
  hand over a zone, a pair of dates and an instant. Only some combinations are
  coherent, and the design freezes which: an all-day event carries dates and no
  zone, a timed event carries a zone and no dates, and an all-day instant must
  be exactly local midnight of its date. The service is the only refusal point.
  These are `INVALID_ARGUMENT` -- the model got the shape wrong and can fix it.
- **routing.** The name becomes an EventKit identifier, or the action is not
  issued at all. A routing failure is *not* an invalid argument: the user asked
  for something reasonable and the world does not currently allow it, so it
  becomes a question rather than a failure (design 2.1: 不签发, 转澄清).

Keeping the two apart matters because they end differently. An invalid
argument is a refusal the model can recompute its way out of; a routing miss
parks the operation waiting for the user to answer something only they know,
such as which of two identically named calendars they meant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Callable, Final

from personal_agent.api.control_client import (
    CalendarResolution,
    CalendarResolved,
    CalendarUnresolved,
)
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.timeutil import (
    LEDGER_TIMEZONE,
    parse_ledger_date,
    parse_rfc3339,
)
from personal_agent_core.tool_ir import CALENDAR_FLIGHT_PLAN

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class CalendarRequest:
    """One attested create request, validated and normalised.

    `timezone` is the event's own zone, or None for "the ledger zone", which is
    what the wire means by an omitted value: a v1 client has no zone to send, a
    timed event resolved in Shanghai needs none, and an all-day event never has
    one.
    """

    calendar_title: str
    timezone: str | None
    start: datetime
    end: datetime
    all_day: bool
    start_date: str | None
    end_date: str | None


def _invalid(detail: str) -> AppError:
    return AppError(ErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def parse_request(attested: dict[str, Any]) -> CalendarRequest:
    """Validate one attested create input against the frozen contract.

    Every refusal here is `INVALID_ARGUMENT`, because every one of them means
    the model produced a shape the service never accepts -- never "the world
    changed". Nothing is issued on any of these paths.

    The JSON Schema has already run in `authorize`: types, lengths, the enum and
    the all-day/timed exclusivity are enforced there. What is left is what a
    schema cannot state -- that a named zone exists on this host, that the two
    instants and the two dates describe the *same* event, and that the one
    calendar in the enum that belongs to another app is never written to.
    """
    calendar_title = attested.get("calendar")
    if not isinstance(calendar_title, str) or not calendar_title:
        raise _invalid("calendar create input carries no target calendar")
    if calendar_title == CALENDAR_FLIGHT_PLAN:
        # The calendar exists so that choosing it produces this refusal rather
        # than the model inventing a calendar or funnelling a flight into 日常
        # 安排. Nothing here can write to it: the flight app owns it.
        raise _invalid(
            "the flight-plan calendar is managed by the flight app and is read-only"
        )

    try:
        start = parse_rfc3339(attested.get("start"))
        end = parse_rfc3339(attested.get("end"))
    except (TypeError, ValueError) as exc:
        raise _invalid("calendar create input carries an unreadable instant") from exc
    if end <= start:
        raise _invalid("calendar create input does not describe a non-empty interval")

    all_day = attested.get("all_day")
    if not isinstance(all_day, bool):
        raise _invalid("calendar create input does not state all_day")

    zone_name = attested.get("timezone")
    if zone_name is not None and (not isinstance(zone_name, str) or not zone_name):
        raise _invalid("calendar create input carries an unreadable timezone")
    # Existence is checked even for an all-day event, which must not carry a
    # zone at all: an unknown zone is worth reporting on its own terms rather
    # than only through the shape rule below.
    _zone(zone_name)

    start_date = attested.get("start_date")
    end_date = attested.get("end_date")

    if all_day:
        if zone_name is not None:
            # An all-day event has a date, not an instant, so there is no
            # anchor zone to record; the EventKit probe froze this
            # (Henson 2026-09-09).
            raise _invalid("an all-day event must not carry a timezone")
        first, last = _day_pair(start_date, end_date)
        # The model's own midnight arithmetic is not trusted: the dates are the
        # source of truth for an all-day event, and the instants must agree
        # with them rather than redefine them. A mismatch is a recomputation,
        # not a user question -- the model already knows what it meant.
        _require_local_midnight(start, first, "start")
        _require_local_midnight(end, last, "end")
        return CalendarRequest(
            calendar_title=calendar_title,
            timezone=None,
            start=start,
            end=end,
            all_day=True,
            start_date=first.isoformat(),
            end_date=last.isoformat(),
        )

    if start_date is not None or end_date is not None:
        raise _invalid("a timed event must not carry all-day dates")
    return CalendarRequest(
        calendar_title=calendar_title,
        timezone=zone_name,
        start=start,
        end=end,
        all_day=False,
        start_date=None,
        end_date=None,
    )


def action_fields(
    request: CalendarRequest,
    resolution: CalendarResolution,
    *,
    attested: dict[str, Any],
) -> dict[str, Any]:
    """The `event` object a v2 client executes from.

    Built from the *attested* arguments rather than the model's raw output, so
    anything the bridge stripped cannot come back through this door, plus the
    two fields only the server can supply: the resolved EventKit identifier and
    the authoritative name the routing matched on.

    Dates and zone are written out explicitly, including as null. A client that
    has to tell "absent" from "not applicable" would otherwise read a timed
    event as one whose dates simply failed to arrive.
    """
    if not isinstance(resolution, CalendarResolved):
        raise _invalid("a device action needs a resolved calendar")
    fields = dict(attested)
    fields["calendar_identifier"] = resolution.calendar_identifier
    fields["calendar_title"] = resolution.calendar_title
    fields["timezone"] = request.timezone
    fields["start_date"] = request.start_date
    fields["end_date"] = request.end_date
    return fields


def routing_question(resolution: CalendarUnresolved, *, title: str) -> str:
    """The sentence a user sees when the name resolved to no single calendar.

    Deterministic text, never a code: it is delivered as the clarification the
    user answers, and "calendar_not_found" answers nothing. Each case says what
    happened and what can be done about it, because the four misses have four
    different remedies -- and only one of them is a dead end.
    """
    if resolution.reason == "directory_empty":
        return (
            f"我还没有收到你 iPhone 上的日历列表，暂时无法把日程写进「{title}」。"
            "请先打开 App 同步一次日历，然后再说一次。"
        )
    if resolution.reason == "not_found":
        return (
            f"你的 iPhone 上找不到名为「{title}」的可写日历。"
            "请在日历 App 里确认名称，或告诉我改用哪个日历。"
        )
    if resolution.reason == "ambiguous":
        return (
            f"有两个以上的日历都叫「{title}」（{_candidate_names(resolution)}）。"
            "请告诉我要用哪一个账户下的日历。"
        )
    if resolution.reason == "read_only":
        return (
            f"「{title}」在你的 iPhone 上是只读的（{_candidate_names(resolution)}），"
            "我无法往里写日程。请换一个日历。"
        )
    # An unrecognised reason is a version skew between the two services, not
    # something a user can answer. Saying so beats asking a question whose
    # remedy does not exist.
    return f"我暂时无法确定「{title}」对应哪个日历，请稍后再试。"


def issuance_policy(remote_name: str) -> Callable[[dict[str, Any]], CalendarRequest]:
    """The pre-issuance policy for one device-executed tool.

    Keyed by the contract name the dispatch fork already derives. This is a
    registry of *business* policy rather than a fact the IR could compute, so it
    is written out -- but a device tool with no entry is refused, not issued.
    An action that skipped its own validation is precisely the failure the phone
    cannot detect: it builds an `EKEvent` from whatever it was handed.
    """
    policy = _ISSUANCE_POLICIES.get(remote_name)
    if policy is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail=f"device tool {remote_name} has no issuance policy",
        )
    return policy


#: Uniform signature, like every other co-dispatched family in this codebase: a
#: policy that needed fewer parameters would still take them, so no adapter at
#: the call site can silently leave one function un-adapted.
_ISSUANCE_POLICIES: Final[dict[str, Callable[[dict[str, Any]], CalendarRequest]]] = {
    "calendar.create_event": parse_request,
}


def _candidate_names(resolution: CalendarUnresolved) -> str:
    names = [
        candidate.source_title or candidate.title for candidate in resolution.candidates
    ]
    return "、".join(names) if names else "来源未知"


def _zone(name: str | None) -> ZoneInfo:
    """The event's zone, or the ledger zone when the wire carries none."""
    if name is None:
        return LEDGER_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _invalid(f"unknown timezone {name!r}") from exc


def _day_pair(start_date: Any, end_date: Any) -> tuple[date, date]:
    if not isinstance(start_date, str) or not isinstance(end_date, str):
        raise _invalid("an all-day event must carry start_date and end_date")
    try:
        first = parse_ledger_date(start_date)
        last = parse_ledger_date(end_date)
    except ValueError as exc:
        raise _invalid("all-day dates must be YYYY-MM-DD") from exc
    if last <= first:
        # The end is exclusive (last day + 1), so equal dates describe an event
        # with no days in it at all.
        raise _invalid("an all-day end_date must be after its start_date")
    return first, last


def _require_local_midnight(moment: datetime, day: date, which: str) -> None:
    expected = datetime.combine(day, time.min, tzinfo=LEDGER_TIMEZONE)
    if moment != expected:
        raise _invalid(
            f"an all-day event's {which} must be local midnight of its date"
        )


__all__ = [
    "CalendarRequest",
    "action_fields",
    "issuance_policy",
    "parse_request",
    "routing_question",
]
