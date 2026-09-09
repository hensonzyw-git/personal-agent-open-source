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
from datetime import datetime
from typing import Any

from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import LEDGER_TIMEZONE, parse_rfc3339


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
        "title",
        "start",
        "end",
        "all_day",
        "location",
        "notes",
        "created_by_agent",
    }
)

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
            title = event.get("title") or "（无标题日程）"
            # The wall-clock time the user lives in, not the wire's UTC: the
            # calendar contract is Asia/Shanghai absolute time, and the
            # summary that renders 07:00 for a 15:00 appointment answers a
            # different question than the one asked. The structured card
            # carries the full instant; this is only the human line.
            start_text = event.get("start") or ""
            try:
                local = parse_rfc3339(start_text).astimezone(LEDGER_TIMEZONE)
                readable = local.strftime("%m-%d %H:%M")
            except ValueError:
                readable = start_text[:16].replace("T", " ")
            lines.append(f"{title}（{readable} 开始）")
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
    return dict(raw)
