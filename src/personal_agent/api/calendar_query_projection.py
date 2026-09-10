"""The strict, whitelisted projection of ``calendar.query_events`` results.

The calendar read is one governed MCP read whose result the Agent turns into a
durable, client-visible outcome. The MCP output schema is the *connector's*
contract; the projection here is the narrower *display* contract. Everything a
client could mis-render fails closed here instead of being echoed.

It exists for the same reason ``finance_query_projection`` does: a governed
read whose ``trusted_result`` is a structured projection must never surface as
raw canonical JSON in ``answer``. The two projections are deliberately separate
modules — Finance's decoder keys on the expense metric const, the calendar one
on the mirror's ``source_system`` const — so a result that belongs to one can
never be decoded by the other. Both are selected through IR-derived sets in the
dispatcher; neither module trusts shape alone.

``summarise_calendar_projection`` is the deterministic text fallback for
clients that predate ``query_result``, derived from the projection only —
never from the model — so the compatibility ``answer`` and the structured card
can never disagree. It leads with the mirror's freshness because the summary
prompt's own contract requires the staleness to be stated, not implied.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import (
    LEDGER_TIMEZONE,
    LEDGER_TIMEZONE_NAME,
    parse_rfc3339,
)


#: The stable failure reason a non-projectable query result receives. Shared
#: with the Finance projection by design: the client-facing meaning ("this read
#: produced nothing safe to show") is the same, and the operation's failure
#: handling is projection-agnostic.
QUERY_RESULT_UNREADABLE = "query_result_unreadable"


class CalendarQueryProjectionError(ValueError):
    """A calendar query result is not a safe, projectable shape.

    Raised by ``decode_calendar_query_projection`` for unknown fields, wrong
    field types and unparseable payloads. Callers fail closed — a safe
    failure, never a presentation of the raw result.
    """


_TOP_LEVEL_FIELDS = frozenset(
    {
        "status",
        "events",
        "record_count",
        "next_cursor",
        "data_as_of",
        "mirror_stale",
        "source_system",
    }
)

_EVENT_FIELDS = frozenset(
    {
        "event_identifier",
        "calendar_identifier",
        "calendar_title",
        "title",
        "start",
        "end",
        "all_day",
        "timezone",
        "start_date",
        "end_date",
        "date_anchor_unknown",
        "title_over_limit",
        "location_over_limit",
        "notes_over_limit",
        "location",
        "notes",
        "created_by_agent",
    }
)

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SOURCE_SYSTEM = "apple_calendar_mirror"


def decode_calendar_query_projection(
    raw: dict[str, Any] | str,
) -> dict[str, Any]:
    """Strictly decode a calendar query result into the display projection.

    Accepts the MCP ``trusted_result`` dict or its canonical JSON string (the
    durable ``safe_result`` carrier), so one decoder is the single reader for
    both live results and history. Unlike the Finance projection this domain
    has no view variants: the output contract is one shape, so the projection
    is that shape, whitelisted field by field.
    """
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CalendarQueryProjectionError(
                "calendar query result is not valid JSON"
            ) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise CalendarQueryProjectionError("calendar query result is not an object")

    unknown = set(data) - _TOP_LEVEL_FIELDS
    if unknown:
        raise CalendarQueryProjectionError(
            f"calendar query result carries unknown fields: {', '.join(sorted(unknown))}"
        )
    if data.get("status") != "ok":
        raise CalendarQueryProjectionError("calendar query status is not ok")
    if data.get("source_system") != _SOURCE_SYSTEM:
        raise CalendarQueryProjectionError(
            "calendar query source_system is not the Apple mirror"
        )

    record_count = data.get("record_count")
    if (
        not isinstance(record_count, int)
        or isinstance(record_count, bool)
        or record_count < 0
    ):
        raise CalendarQueryProjectionError("record_count is not a non-negative integer")

    events = data.get("events")
    if not isinstance(events, list):
        raise CalendarQueryProjectionError("events is not a list")
    next_cursor = data.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise CalendarQueryProjectionError("next_cursor is not a string or null")
    # Second review F5: `events` is one *page* of the result; `record_count`
    # is the whole window's total. A page may carry fewer events than the
    # total, but never more, and a page with a next cursor must be a strict
    # prefix. The final page legitimately carries fewer than the total — it
    # carries only the remainder.
    if len(events) > record_count:
        raise CalendarQueryProjectionError(
            "the page carries more events than the record_count total"
        )
    if next_cursor is not None and len(events) >= record_count:
        raise CalendarQueryProjectionError(
            "a page with a next_cursor cannot already hold every record"
        )
    next_cursor = data.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise CalendarQueryProjectionError("next_cursor is not a string or null")
    data_as_of = data.get("data_as_of")
    if not isinstance(data_as_of, str) or not data_as_of:
        raise CalendarQueryProjectionError("data_as_of is not a non-empty string")
    mirror_stale = data.get("mirror_stale")
    if not isinstance(mirror_stale, bool):
        raise CalendarQueryProjectionError("mirror_stale is not a boolean")

    return {
        "status": "ok",
        "events": [_decode_event(item) for item in events],
        "record_count": record_count,
        "next_cursor": next_cursor,
        "data_as_of": data_as_of,
        "mirror_stale": mirror_stale,
        "source_system": _SOURCE_SYSTEM,
    }


#: The Chinese short names the summary and the list card use for the zones
#: Henson's calendar actually holds (design §5.3). A zone outside this map is
#: still named — as its UTC offset at that event's instant, which is exact —
#: rather than silently rendered as if it were local time.
_ZONE_LABELS: Final[dict[str, str]] = {
    "Asia/Tokyo": "日本时间",
    "Asia/Seoul": "韩国时间",
    "Asia/Singapore": "新加坡时间",
    "Asia/Hong_Kong": "香港时间",
    "Asia/Bangkok": "泰国时间",
    "Asia/Kolkata": "印度时间",
    "Asia/Dubai": "迪拜时间",
    "Europe/London": "英国时间",
    "Europe/Paris": "法国时间",
    "Europe/Berlin": "德国时间",
    "Europe/Moscow": "莫斯科时间",
    "America/New_York": "纽约时间",
    "America/Chicago": "芝加哥时间",
    "America/Denver": "丹佛时间",
    "America/Los_Angeles": "洛杉矶时间",
    "America/Sao_Paulo": "圣保罗时间",
    "Australia/Sydney": "悉尼时间",
    "Pacific/Auckland": "奥克兰时间",
    "UTC": "UTC",
    "Etc/UTC": "UTC",
}


def summarise_calendar_projection(projection: dict[str, Any]) -> str:
    """Deterministic text fallback for clients that predate ``query_result``.

    Never model prose: it is derived solely from the validated projection, so
    the compatibility ``answer`` and the structured card can never disagree.

    Second review F6: the fallback *names* what the mirror holds (up to the
    first three events, with titles and start times), says when the data was
    taken, and warns when the mirror is stale — a summary that says only
    共 N 条日程 answers none of the question the user asked. A never-synced
    mirror (the placeholder `data_as_of` shape) says so in words rather than
    presenting the query instant as an observation.

    Each line is rendered by `_event_line`, which is where this side's
    presentation rules live: an all-day event from its dates, a timed event in
    its own zone with the zone named, and the honesty annotations
    (§5.2/§5.3/§6). The iOS list card renders the same rules from the same
    fields rather than this string — the two are kept in step by the row
    fields the projection carries, not by sharing code, so a rule changed
    here has to be changed there too (design §13 step 6).
    """
    count = projection["record_count"]
    more = "，还有更多" if projection["next_cursor"] else ""
    # How many lines the summary will actually show. The omitted count is
    # computed against *this* number (third review G5): the first cut
    # subtracted the whole page's length, so a page of five showed three
    # lines and claimed nothing was missing.
    displayed = min(3, len(projection["events"]))
    lines: list[str] = []
    if count == 0:
        lines.append("这个时间段没有日程")
    else:
        for event in projection["events"][:displayed]:
            lines.append(_event_line(event))
        if count > displayed:
            lines.append(f"另有 {count - displayed} 条未列出")
    summary = "；".join(lines)
    if projection["mirror_stale"]:
        # The stale flag already covers both the age and the window-coverage
        # shapes (F7): the phrasing stays generic on purpose.
        summary += "；注意：日历镜像已陈旧或未覆盖该时间段，结果可能不全"
        if projection["record_count"] == 0 and not projection["events"]:
            summary = "日历镜像尚未同步，暂时无法给出安排"
    else:
        summary += f"，数据截至 {projection['data_as_of']}"
    return summary + more


def canonical_calendar_projection_json(projection: dict[str, Any]) -> str:
    """The durable carrier: canonical JSON of the whitelisted projection."""
    return canonical_json(projection)


def _event_line(event: dict[str, Any]) -> str:
    """One event as a human line, by the rules the design fixed.

    - **all-day events come from their dates** (§5.2, R1-F2). An all-day
      event's content is a day or a range of days, and converting its epoch
      instant is how a Tokyo 10-01 became a 09-30 in the summary. The stored
      `end_date` is exclusive (EventKit's own convention), so the last day is
      one before it.
    - **timed events are rendered in their own zone and the zone is named**
      (§5.3/Q12). Folding 19:00 Tokyo into 18:00 Shanghai answers a question
      nobody asked; `timezone` null means the upload predates the v2 shape
      and Asia/Shanghai is the honest reading, byte-identical to before.
    - **the uncertainty and the truncation are stated, never hidden**: a date
      whose anchor the device could not confirm, and a field the mirror had to
      drop for length (§6 — 不静默). A dropped title is named as such rather
      than shown as 「无标题」, which is a different fact about the same null.
    """
    title = event.get("title")
    if title is None:
        title = "标题过长未同步" if event.get("title_over_limit") else "（无标题日程）"

    notes: list[str] = []
    if event["all_day"]:
        when = _all_day_span(event.get("start_date"), event.get("end_date"))
        if event["date_anchor_unknown"]:
            notes.append("日期归属未确认")
    else:
        when = _start_moment(event)
    if event["location_over_limit"]:
        notes.append("地点过长未同步")
    if event["notes_over_limit"]:
        notes.append("备注过长未同步")
    suffix = f"，{'、'.join(notes)}" if notes else ""
    return f"{title}（{when}{suffix}）"


def _all_day_span(start_date: Any, end_date: Any) -> str:
    """`10-02 全天`, or `10-01 至 10-03 全天` for a range.

    The decoder has already proved both dates are `YYYY-MM-DD` on an all-day
    row, so a date that will not parse here is impossible rather than
    unlikely; the raw text is returned instead of raising, because a summary
    is not a place to discover a contract violation that was already checked.
    """
    try:
        first = date.fromisoformat(start_date)
        # Exclusive end: 10-04 ends the range that covers 10-01..10-03.
        last = date.fromisoformat(end_date) - timedelta(days=1)
    except (TypeError, ValueError):
        return f"{start_date} 至 {end_date} 全天"
    if last <= first:
        return f"{first.isoformat()[5:]} 全天"
    return f"{first.isoformat()[5:]} 至 {last.isoformat()[5:]} 全天"


def _start_moment(event: dict[str, Any]) -> str:
    """`09-13 10:00 开始`, with the zone named when it is not the reference."""
    start_text = event.get("start") or ""
    identifier = event.get("timezone")
    try:
        start = parse_rfc3339(start_text)
    except ValueError:
        return f"{start_text[:16].replace('T', ' ')} 开始"
    if identifier is None or identifier == LEDGER_TIMEZONE_NAME:
        zone = LEDGER_TIMEZONE
        label = None
    else:
        try:
            zone = ZoneInfo(identifier)
        except (ZoneInfoNotFoundError, ValueError):
            # A zone the server cannot construct is still a fact the device
            # reported; naming it is more honest than showing Shanghai time.
            return f"{start.astimezone(LEDGER_TIMEZONE).strftime('%m-%d %H:%M')} {identifier} 开始"
        label = _zone_label(identifier, start)
    clock = start.astimezone(zone).strftime("%m-%d %H:%M")
    return f"{clock} {label} 开始" if label else f"{clock} 开始"


def _zone_label(identifier: str, at: datetime) -> str:
    """The Chinese short name for a zone, or its offset at that instant."""
    label = _ZONE_LABELS.get(identifier)
    if label is not None:
        return label
    try:
        offset = at.astimezone(ZoneInfo(identifier)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - see above
        return identifier
    if offset is None:  # pragma: no cover - a zone always has an offset
        return identifier
    seconds = int(offset.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    hours, minutes = divmod(abs(seconds) // 60, 60)
    if not hours and not minutes:
        return "UTC"
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _decode_event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CalendarQueryProjectionError("events item is not an object")
    unknown = set(raw) - _EVENT_FIELDS
    if unknown:
        raise CalendarQueryProjectionError(
            f"events item carries unknown fields: {', '.join(sorted(unknown))}"
        )
    for field in ("event_identifier", "calendar_identifier"):
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            raise CalendarQueryProjectionError(
                f"events item {field} is not a non-empty string"
            )
    for field in ("start", "end"):
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            raise CalendarQueryProjectionError(
                f"events item {field} is not a non-empty string"
            )
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        raise CalendarQueryProjectionError("events item title is not a string or null")
    # The list card's 日历名 (design 9.2). Null is a fact -- the device has no
    # name for that identifier -- and never a reason to fall back to the
    # EventKit UUID, which would put an identifier where a person's own name
    # for the calendar belongs.
    calendar_title = raw.get("calendar_title")
    if calendar_title is not None and not isinstance(calendar_title, str):
        raise CalendarQueryProjectionError(
            "events item calendar_title is not a string or null"
        )
    all_day = raw.get("all_day")
    if not isinstance(all_day, bool):
        raise CalendarQueryProjectionError("events item all_day is not a boolean")
    for field in ("location", "notes"):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise CalendarQueryProjectionError(
                f"events item {field} is not a string or null"
            )
    created_by_agent = raw.get("created_by_agent")
    if not isinstance(created_by_agent, bool):
        raise CalendarQueryProjectionError(
            "events item created_by_agent is not a boolean"
        )
    timezone = raw.get("timezone")
    if timezone is not None and (not isinstance(timezone, str) or not timezone):
        raise CalendarQueryProjectionError(
            "events item timezone is not a non-empty string or null"
        )
    for field in ("start_date", "end_date"):
        value = raw.get(field)
        if value is not None and (
            not isinstance(value, str) or not _DATE_PATTERN.match(value)
        ):
            raise CalendarQueryProjectionError(
                f"events item {field} is not a YYYY-MM-DD date or null"
            )
    date_anchor_unknown = raw.get("date_anchor_unknown")
    if not isinstance(date_anchor_unknown, bool):
        raise CalendarQueryProjectionError(
            "events item date_anchor_unknown is not a boolean"
        )
    # The date columns and the epoch instants must agree about which kind of
    # event this is. An all-day event with no dates cannot be displayed the
    # way the contract requires (its content *is* the dates), and a timed
    # event carrying them would invite a renderer to use a date the mirror
    # never claimed. Checked here, at the display boundary, so no client has
    # to be defensive about it.
    if all_day:
        if not isinstance(raw.get("start_date"), str) or not isinstance(
            raw.get("end_date"), str
        ):
            raise CalendarQueryProjectionError(
                "an all-day event must carry start_date and end_date"
            )
        if timezone is not None:
            raise CalendarQueryProjectionError(
                "an all-day event must not carry a timezone"
            )
    else:
        if raw.get("start_date") is not None or raw.get("end_date") is not None:
            raise CalendarQueryProjectionError(
                "a timed event must not carry all-day dates"
            )
        if date_anchor_unknown:
            raise CalendarQueryProjectionError(
                "a timed event has no date attribution to be unknown"
            )
    for field in ("title_over_limit", "location_over_limit", "notes_over_limit"):
        flag = raw.get(field)
        if not isinstance(flag, bool):
            raise CalendarQueryProjectionError(
                f"events item {field} is not a boolean"
            )
        # The flag is the only surviving evidence of content the mirror could
        # not keep. If it is set and the text is still there, one of the two
        # is lying, and a card that showed the flag would be describing a
        # state the mirror is not in.
        if flag and raw.get(field[: -len("_over_limit")]) is not None:
            raise CalendarQueryProjectionError(
                f"events item {field} is set but the field it describes is present"
            )
    return dict(raw)
