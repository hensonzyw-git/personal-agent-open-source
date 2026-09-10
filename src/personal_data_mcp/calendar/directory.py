"""The device's calendar directory: how a name becomes an EventKit identifier.

A create names a *calendar* (`日常安排`, `出游计划`, `演出&活动`), but the
action that reaches the phone must carry the calendar's EventKit identifier --
the phone does not choose a calendar. The directory is what makes that
translation possible, and it is per device: the same name resolves to
different identifiers on a re-installed phone.

The device uploads the whole directory with each sync batch. Uploads are
idempotent by construction: a repeated entry is the same row written again,
which is exactly what "the phone keeps me current" should mean -- and "whole"
is load-bearing, because the statement a batch makes is about a *set*: a
calendar it does not name is one the device does not have, so the row is
retired rather than kept. A directory that only ever grew was how a
re-installed phone ended up with two live rows under one title, turning a
calendar the user could name into a question with no good answer.

A calendar the server cannot resolve is never guessed at -- an unresolvable
name becomes a clarification to the user, not a write into whatever calendar
looked closest. That rule lives with its caller (the dispatcher, design 2.1);
this module's job is the honest state it reads from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.storage.models import CalendarDirectory


def upsert_calendars(
    session,
    *,
    device_id: str,
    calendars: list[Any],
    now: datetime,
    snapshot_ts: int,
    states_the_whole_directory: bool,
) -> int:
    """Apply one batch's calendar entries. Returns how many were named.

    The list is the device's **whole** directory, not a delta, so this is a
    statement about a set and not a merge into one: when
    `states_the_whole_directory` is true, a calendar the statement does not
    name is one the device no longer has, and its row is retired (design 2.1;
    2026-09-10 review). Retirement is not deletion -- see `CalendarDirectory`.
    A batch that says nothing about the directory (`calendars` absent, which is
    what a v1 client sends) may not retire anything, and neither may a batch
    older than the newest statement: `snapshot_ts` orders the statements the
    same way `calendar_events` orders its snapshots, so older testimony can
    neither rename a calendar, nor re-add one a newer statement dropped, nor
    retire one it keeps.

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
                snapshot_ts=snapshot_ts,
                updated_at=now,
            )
            session.add(row)
            continue
        if row.snapshot_ts is not None and snapshot_ts < row.snapshot_ts:
            # Older testimony than the row already holds: it may not rename a
            # calendar the phone has since renamed, and it may not clear a
            # retirement a newer statement set (which is how a late packet
            # would otherwise re-add a calendar the device dropped).
            continue
        # A rename or a permission change arrives as the same row with new
        # facts: the identifier is the identity, the rest is testimony.
        row.title = title
        row.source_title = source_title
        row.allows_content_modifications = writable
        row.is_subscribed = subscribed
        row.snapshot_ts = snapshot_ts
        # The phone listing it again is the phone having it. A transient empty
        # read on the device must not be permanent, and neither must a
        # re-install's new identifiers being reported after the old ones went.
        row.retired_at = None
        row.updated_at = now

    if not states_the_whole_directory:
        return len(seen)

    # What the statement did *not* name. Rows are read rather than deleted, so
    # this walks the device's rows -- capped at `MAX_CALENDARS` by the ingest
    # contract, the same bound the directory read itself relies on.
    for row in (
        session.execute(
            select(CalendarDirectory).where(CalendarDirectory.device_id == device_id)
        )
        .scalars()
        .all()
    ):
        if row.calendar_identifier in seen or row.retired_at is not None:
            continue
        if row.snapshot_ts is not None and row.snapshot_ts > snapshot_ts:
            # A *newer* statement named it: this one is a late packet, and a
            # late packet is not this device's current word about its
            # calendars.
            continue
        row.retired_at = now
    return len(seen)
