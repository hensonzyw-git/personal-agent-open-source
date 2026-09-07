"""The governed read path behind ``calendar.query_events``.

The mirror only knows what the phone last reported, so this module's second
job — after filtering — is honesty about freshness: `data_as_of` is the
newest `synced_at` in the mirror (or `None` when it has never heard from a
device), and `mirror_stale` is a pure function of that instant's age, not of
the model's confidence. The summary prompt receives both as part of the tool
contract, not as a courtesy.

Pagination follows the Finance read: a server-signed, opaque, expiring
cursor bound to the canonical window. A cursor from a different window is a
distinguishable `INVALID_CURSOR`, never silently reinterpreted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import select

from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import parse_rfc3339, to_rfc3339
from personal_data_mcp.storage.models import CalendarEvent


PAGE_SIZE: Final[int] = 50
MAX_WINDOW_DAYS: Final[int] = 400
CURSOR_VERSION: Final[int] = 1
CURSOR_TTL: Final[timedelta] = timedelta(minutes=10)
#: A mirror older than this against the query's own wall clock is stale.
STALE_AFTER: Final[timedelta] = timedelta(hours=25)


@dataclass(frozen=True)
class _DecodedCursor:
    start_ts: int
    end_ts: int
    offset: int
    expires_at: datetime


def _encode_cursor(
    *,
    start_ts: int,
    end_ts: int,
    offset: int,
    expires_at: datetime,
    secret: bytes,
) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "start": start_ts,
        "end": end_ts,
        "offset": offset,
        "exp": int(expires_at.timestamp()),
    }
    raw = canonical_json(json.loads(json.dumps(payload))).encode("utf-8")
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return ".".join(
        base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        for value in (raw, signature)
    )


def _decode_cursor(
    cursor: str, *, secret: bytes, now: datetime
) -> _DecodedCursor:
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        padding = "=" * (-len(encoded_payload) % 4)
        raw = base64.urlsafe_b64decode(encoded_payload + padding)
        padding = "=" * (-len(encoded_signature) % 4)
        supplied_signature = base64.urlsafe_b64decode(encoded_signature + padding)
        expected_signature = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("signature mismatch")
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or payload.get("v") != CURSOR_VERSION
            or type(payload.get("start")) is not int
            or type(payload.get("end")) is not int
            or type(payload.get("offset")) is not int
            or payload["offset"] < 0
            or type(payload.get("exp")) is not int
            or payload["exp"] <= int(now.timestamp())
        ):
            raise ValueError("invalid cursor payload")
        return _DecodedCursor(
            start_ts=payload["start"],
            end_ts=payload["end"],
            offset=payload["offset"],
            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
    except (
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise AppError(
            ErrorCode.INVALID_CURSOR,
            internal_detail="calendar query cursor is invalid or expired",
        ) from exc


def _window(arguments: dict[str, Any]) -> tuple[datetime, datetime, str | None]:
    try:
        start = parse_rfc3339(arguments["start"])
        end = parse_rfc3339(arguments["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"calendar query window missing or malformed: {exc}",
        ) from exc
    if end <= start:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="calendar query end must be after start",
        )
    if (end - start).days > MAX_WINDOW_DAYS:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"calendar query window exceeds {MAX_WINDOW_DAYS} days",
        )
    cursor = arguments.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="calendar query cursor must be a string or null",
        )
    return start, end, cursor


def query_events(
    arguments: dict[str, Any],
    *,
    sessions,
    keyring: KeyRing,
    cursor_secret: bytes,
    now: datetime,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    """Read one page of the mirror for the requested window."""
    start, end, cursor_text = _window(arguments)
    if cursor_text is not None:
        decoded = _decode_cursor(cursor_text, secret=cursor_secret, now=now)
        if decoded.start_ts != int(start.timestamp()) or decoded.end_ts != int(
            end.timestamp()
        ):
            raise AppError(
                ErrorCode.INVALID_CURSOR,
                internal_detail="calendar query cursor belongs to another window",
            )
        offset = decoded.offset
    else:
        offset = 0

    with sessions() as session:
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        # Overlap semantics: an event belongs to the window when it intersects
        # it. An all-day event therefore matches its whole day, and a meeting
        # that straddles the boundary is not invisibly split in half.
        rows = (
            session.execute(
                select(CalendarEvent)
                .where(
                    CalendarEvent.start_ts < end_ts,
                    CalendarEvent.end_ts > start_ts,
                    CalendarEvent.is_deleted.is_(False),
                )
                .order_by(CalendarEvent.start_ts, CalendarEvent.event_identifier)
            )
            .scalars()
            .all()
        )

        data_as_of: datetime | None = None
        for row in rows:
            if data_as_of is None or row.synced_at > data_as_of:
                data_as_of = row.synced_at
        if data_as_of is None:
            for value in (
                session.execute(select(CalendarEvent.synced_at)).scalars().all()
            ):
                if data_as_of is None or value > data_as_of:
                    data_as_of = value

        page = rows[offset : offset + page_size]
        next_offset = offset + len(page)

        events: list[dict[str, Any]] = []
        for row in page:
            event: dict[str, Any] = {
                "event_identifier": row.event_identifier,
                "calendar_identifier": row.calendar_identifier,
                "title": _open(keyring, row, "title"),
                "start": to_rfc3339(
                    datetime.fromtimestamp(row.start_ts, tz=timezone.utc)
                ),
                "end": to_rfc3339(
                    datetime.fromtimestamp(row.end_ts, tz=timezone.utc)
                ),
                "all_day": row.all_day,
                "location": _open(keyring, row, "location"),
                "notes": _open(keyring, row, "notes"),
                "created_by_agent": row.created_by_agent,
            }
            events.append(event)

        next_cursor = (
            _encode_cursor(
                start_ts=start_ts,
                end_ts=end_ts,
                offset=next_offset,
                expires_at=(
                    decoded.expires_at
                    if cursor_text is not None
                    else now + CURSOR_TTL
                ),
                secret=cursor_secret,
            )
            if next_offset < len(rows)
            else None
        )

        return {
            "status": "ok",
            "events": events,
            "record_count": len(rows),
            "next_cursor": next_cursor,
            "data_as_of": to_rfc3339(data_as_of) if data_as_of is not None else None,
            "mirror_stale": data_as_of is None or (now - data_as_of) > STALE_AFTER,
            "source_system": "apple_calendar_mirror",
        }


def _open(keyring: KeyRing, row: CalendarEvent, column: str) -> str | None:
    envelope = getattr(row, column)
    if envelope is None:
        return None
    return (
        keyring.decrypt(
            envelope, table="calendar_events", column=column, row_id=row.row_key
        )
        .decode("utf-8")
    )
