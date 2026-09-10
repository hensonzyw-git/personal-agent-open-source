"""The device's calendar directory: how a name becomes an EventKit identifier.

A create names a *calendar* (`日常安排`, `出游计划`, `演出&活动`), but the
action that reaches the phone must carry the calendar's EventKit identifier --
the phone does not choose a calendar. The directory is what makes that
translation possible, and it is per device: the same name resolves to
different identifiers on a re-installed phone.

The device uploads the whole directory with each sync batch. Uploads are
idempotent by construction: a repeated entry is the same row written again,
which is exactly what "the phone keeps me current" should mean.

A calendar the server cannot resolve is never guessed at -- an unresolvable
name becomes a clarification to the user, not a write into whatever calendar
looked closest. That rule lives with its caller (the dispatcher, design 2.1);
this module's job is the honest state it reads from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.storage.models import CalendarDirectory


def upsert_calendars(
    session,
    *,
    device_id: str,
    calendars: list[Any],
    now: datetime,
) -> int:
    """Merge one batch's calendar entries. Returns how many were written.

    Rejects a batch that names one calendar twice (§5.1: an incoherent batch
    is refused whole, never resolved by taking the last one), and rejects an
    entry whose fields are not the declared shape -- the core is a boundary
    too, so it does not trust an upstream validator to have run.
    """
    seen: set[str] = set()
    for entry in calendars:
        if not isinstance(entry, dict):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="calendar directory entry must be an object",
            )
        identifier = entry.get("calendar_identifier")
        title = entry.get("title")
        if not isinstance(identifier, str) or not identifier:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="calendar directory entry has no identifier",
            )
        if not isinstance(title, str) or not title:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="calendar directory entry has no title",
            )
        source_title = entry.get("source_title")
        if source_title is not None and not isinstance(source_title, str):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="calendar directory source_title must be text or null",
            )
        writable = entry.get("allows_content_modifications")
        subscribed = entry.get("is_subscribed")
        if not isinstance(writable, bool) or not isinstance(subscribed, bool):
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=(
                    "calendar directory entry must state whether the calendar is "
                    "writable and whether it is subscribed"
                ),
            )
        if identifier in seen:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail="calendar directory batch repeats one calendar",
            )
        seen.add(identifier)

        row = session.get(CalendarDirectory, (device_id, identifier))
        if row is None:
            row = CalendarDirectory(
                device_id=device_id,
                calendar_identifier=identifier,
                title=title,
                source_title=source_title,
                allows_content_modifications=writable,
                is_subscribed=subscribed,
                updated_at=now,
            )
            session.add(row)
            continue
        # A rename or a permission change arrives as the same row with new
        # facts: the identifier is the identity, the rest is testimony.
        row.title = title
        row.source_title = source_title
        row.allows_content_modifications = writable
        row.is_subscribed = subscribed
        row.updated_at = now
    return len(seen)
