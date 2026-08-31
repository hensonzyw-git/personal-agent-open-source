"""The Timeline: one sealed, permanently-retained, ordered personal archive.

Every user message and every operation outcome is appended here as a sealed
event (design 4: conversation events are retained permanently and always
sealed). The content is AEAD-bound by AAD to its own row, so an event cannot be
lifted to another conversation. Retention is not permission to feed the archive
to a model; this module only appends and reads the owner's own Timeline.

`CAP-001` turns it into a single canonical Timeline with a stable order:

- **one Timeline.** A single-user deployment has exactly one canonical row.
  An unknown client `conversation_id` is a stable `TIMELINE_MISMATCH`, never a
  second Timeline created by accident; a pre-migration id still resolves,
  through an HMAC alias that keeps no historical identifier in the clear.
- **one order.** `timeline_sequence` is allocated by a conditional UPDATE on
  the Timeline row, so two devices appending in the same instant cannot receive
  the same number. `created_at` and SQLite's `rowid` are deliberately not the
  pagination contract: the first is not unique and the second does not survive
  a migration.
- **opaque cursors.** A page cursor is server-signed and binds the schema
  version, the Timeline, the anchor sequence and the direction. Tampering,
  using an `older` cursor as `newer`, or replaying one against another Timeline
  are all one refusal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from personal_agent.keys import HmacKey, HmacKeyRing
from personal_agent.storage.models import (
    ContextSession,
    Conversation,
    ConversationAlias,
    ConversationEvent,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json


_TABLE = "conversation_events"
_COLUMN = "encrypted_content"

USER_MESSAGE = "user_message"
OPERATION_RESULT = "operation_result"
#: `DEV-031`. A user's duplicate choice is a permanent Timeline fact, separate
#: from the operation result it caused. It is presentation state, not dialogue,
#: and therefore deliberately absent from `MODEL_VISIBLE_EVENT_TYPES`.
DUPLICATE_DECISION = "duplicate_decision"
#: `G1`. A verified category correction for an existing expense row. This is a
#: presentation fact, like `DUPLICATE_DECISION`: it lets every client resolve an
#: old receipt to the ledger row's newer category without rewriting the sealed
#: original operation. It is deliberately absent from
#: `MODEL_VISIBLE_EVENT_TYPES`; tapping a picker is not new dialogue or an
#: instruction for a later model turn.
EXPENSE_CATEGORY_CORRECTED = "expense_category_corrected"
#: `CAP-001` design 6.1: a boundary is a persisted, displayable Timeline fact,
#: so every device shows the same divider. Neither divider event is ever fed to
#: the model as an instruction.
SESSION_DIVIDER = "session_divider"
SESSION_BOUNDARY_CORRECTED = "session_boundary_corrected"
#: The daily review card, sealed by the nightly job with the ledger values read
#: at build time (design `1j`). It is presentation, not dialogue: the scheduler
#: writes it without a user turn, and feeding a frozen review to the model as an
#: instruction would be noise at best. Deliberately absent from
#: `MODEL_VISIBLE_EVENT_TYPES`.
DAILY_REVIEW = "daily_review"
#: The systemic-risk daily card, sealed by the risk-monitor job with the scores
#: read at build time. Presentation, not dialogue, exactly like ``DAILY_REVIEW``:
#: the scheduler writes it without a user turn, and a frozen score snapshot is
#: not an instruction for a later model turn. Deliberately absent from
#: `MODEL_VISIBLE_EVENT_TYPES`.
RISK_REPORT = "risk_report"

#: Event types the Context Builder may show the model as conversation history.
#: Dividers are presentation, not dialogue.
MODEL_VISIBLE_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {USER_MESSAGE, OPERATION_RESULT}
)

CURSOR_VERSION: Final[str] = "timeline_cursor_v1"
DIRECTIONS: Final[frozenset[str]] = frozenset({"older", "newer"})


@dataclass(frozen=True)
class TimelineEntry:
    event_id: str
    event_type: str
    operation_id: str | None
    created_at: datetime
    content: dict[str, Any]
    #: Internal ordering key. Never serialised to a client (design 14).
    timeline_sequence: int
    session_id: str
    turn_id: str


@dataclass(frozen=True)
class TimelinePage:
    entries: tuple[TimelineEntry, ...]
    older_cursor: str | None
    newer_cursor: str | None
    has_older: bool
    has_newer: bool


# -- canonical Timeline ---------------------------------------------------


def canonical_timeline_id(session, *, now: datetime) -> str:
    """The one canonical Timeline, created on first use if absent.

    Creation is guarded by the partial unique index rather than by the read
    above it: two concurrent first requests both see no canonical row, and the
    index is what makes only one of them create it.
    """
    existing = _canonical_row(session)
    if existing is not None:
        return existing
    conversation = Conversation(
        conversation_id=f"tl_{uuid.uuid4().hex}",
        created_at=now,
        next_sequence=1,
        is_canonical=True,
    )
    session.add(conversation)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        winner = _canonical_row(session)
        if winner is None:  # pragma: no cover - the index only fails on a race
            raise
        return winner
    return conversation.conversation_id


def _canonical_row(session) -> str | None:
    return session.execute(
        text(
            "SELECT conversation_id FROM conversations WHERE is_canonical = 1"
        )
    ).scalar_one_or_none()


def resolve_timeline(
    session,
    identifier_key: HmacKey | HmacKeyRing,
    *,
    client_conversation_id: str,
    now: datetime,
) -> str:
    """Map whatever the client sent onto the canonical Timeline, or refuse.

    Called before the request fingerprint is computed, the event is written or
    the operation is created (design 4.2.3): the client value must never decide
    a data boundary.
    """
    canonical = canonical_timeline_id(session, now=now)
    if not isinstance(client_conversation_id, str) or not client_conversation_id:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="conversation_id must be a non-empty string",
        )
    if client_conversation_id == canonical:
        return canonical
    for candidate in _hmac_keys(identifier_key):
        alias = session.get(
            ConversationAlias,
            timeline_alias_hmac(candidate, client_conversation_id),
        )
        if alias is not None and alias.conversation_id == canonical:
            return canonical
    # Stable and uninformative: an unknown id and an id from someone else's
    # database are the same answer, and neither creates anything.
    raise AppError(
        ErrorCode.TIMELINE_MISMATCH,
        internal_detail="conversation_id does not resolve to the canonical Timeline",
    )


def timeline_alias_hmac(identifier_key: HmacKey, conversation_id: str) -> str:
    """The stored form of a pre-`CAP-001` conversation id."""
    return hmac.new(
        identifier_key.secret,
        f"timeline-alias\x1f{conversation_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _hmac_keys(key: HmacKey | HmacKeyRing) -> tuple[HmacKey, ...]:
    if isinstance(key, HmacKeyRing):
        return key.verification_keys
    return (key,)


# -- appending ------------------------------------------------------------


def new_turn_id() -> str:
    """A turn groups one user message with the result of its operation."""
    return f"trn_{uuid.uuid4().hex}"


def allocate_sequence(session, *, conversation_id: str) -> int:
    """Take the next `timeline_sequence` with a conditional UPDATE.

    Read-then-write would hand the same number to two concurrent appends. The
    `UNIQUE(conversation_id, timeline_sequence)` constraint would then reject
    the loser -- correct, but as a late failure of an already-accepted message.
    Allocating inside a single statement makes the ordinary case correct and
    leaves the constraint as the backstop it should be.
    """
    updated = session.execute(
        text(
            "UPDATE conversations SET next_sequence = next_sequence + 1 "
            "WHERE conversation_id = :cid RETURNING next_sequence - 1"
        ),
        {"cid": conversation_id},
    ).scalar_one_or_none()
    if updated is None:
        raise AppError(
            ErrorCode.TIMELINE_MISMATCH,
            internal_detail="no such Timeline to append to",
        )
    return int(updated)


def append_event(
    session,
    keyring: KeyRing,
    *,
    conversation_id: str,
    session_id: str,
    turn_id: str,
    event_type: str,
    content: dict[str, Any],
    operation_id: str | None,
    now: datetime,
) -> str:
    """Seal one event onto the Timeline and return the event id."""
    event_id = f"evt_{uuid.uuid4().hex}"
    envelope = keyring.encrypt(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        table=_TABLE,
        column=_COLUMN,
        row_id=event_id,
    )
    sequence = allocate_sequence(session, conversation_id=conversation_id)
    session.add(
        ConversationEvent(
            event_id=event_id,
            conversation_id=conversation_id,
            timeline_sequence=sequence,
            session_id=session_id,
            turn_id=turn_id,
            event_type=event_type,
            encrypted_content=envelope,
            operation_id=operation_id,
            created_at=now,
        )
    )
    conversation = session.get(Conversation, conversation_id)
    conversation.last_event_at = now
    context_session = session.get(ContextSession, session_id)
    if (
        context_session is None
        or context_session.conversation_id != conversation_id
    ):
        raise AppError(
            ErrorCode.CONTEXT_UNAVAILABLE,
            internal_detail="event Session does not belong to the Timeline",
        )
    context_session.last_event_at = now
    session.flush()
    return event_id


# -- reading --------------------------------------------------------------


def list_timeline(
    session, keyring: KeyRing, *, conversation_id: str
) -> list[TimelineEntry]:
    """The whole decrypted Timeline, oldest first.

    Server-side callers only: the client-facing path is `read_page`, which is
    bounded. Ordering is by `timeline_sequence`, the only stable contract.
    """
    events = (
        session.query(ConversationEvent)
        .filter(ConversationEvent.conversation_id == conversation_id)
        .order_by(ConversationEvent.timeline_sequence)
        .all()
    )
    return [_entry(keyring, event) for event in events]


def event_exists_with(
    session,
    keyring: KeyRing,
    *,
    event_type: str,
    content_key: str,
    content_value: Any,
) -> bool:
    """True when an event of ``event_type`` already carries
    ``content[content_key] == content_value`` (decrypted).

    The idempotency primitive for scheduler-written cards: the risk report is
    sealed once per Shanghai calendar day (its ``sealed_on``), so a same-day
    rerun finds the existing card and adds nothing, while a new morning always
    seals a fresh card. Scans only that event type, not the whole Timeline.
    """
    rows = (
        session.query(ConversationEvent)
        .filter(ConversationEvent.event_type == event_type)
        .all()
    )
    return any(
        _entry(keyring, event).content.get(content_key) == content_value
        for event in rows
    )


def read_page(
    session,
    keyring: KeyRing,
    cursor_key: HmacKey | HmacKeyRing,
    *,
    conversation_id: str,
    cursor: str | None,
    direction: str,
    limit: int,
) -> TimelinePage:
    """One bounded page, always presented oldest-to-newest within the page.

    The first request returns the newest page, which is what a chat opens on;
    scrolling up continues with `older_cursor`. Incremental sync uses
    `direction=newer`, which requires a cursor -- "everything newer than
    nothing" is the whole Timeline, and that is not a page.
    """
    if direction not in DIRECTIONS:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="direction must be older or newer",
        )
    if limit < 1:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT, internal_detail="limit must be positive"
        )
    anchor: int | None = None
    if cursor is not None:
        anchor = _decode_cursor(
            cursor,
            key=cursor_key,
            conversation_id=conversation_id,
            direction=direction,
        )
    elif direction == "newer":
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="direction=newer requires a cursor",
        )

    query = session.query(ConversationEvent).filter(
        ConversationEvent.conversation_id == conversation_id
    )
    if direction == "older":
        if anchor is not None:
            query = query.filter(ConversationEvent.timeline_sequence < anchor)
        # One extra row answers `has_older` without a second count query.
        rows = (
            query.order_by(ConversationEvent.timeline_sequence.desc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        rows = list(reversed(rows[:limit]))
    else:
        query = query.filter(ConversationEvent.timeline_sequence > anchor)
        rows = (
            query.order_by(ConversationEvent.timeline_sequence)
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]

    entries = tuple(_entry(keyring, event) for event in rows)
    if not entries:
        # An empty page still has to say honestly whether more exists in the
        # direction that was asked for, and must not mint a cursor that anchors
        # on nothing.
        return TimelinePage(
            entries=(),
            older_cursor=None,
            newer_cursor=None,
            has_older=has_more if direction == "older" else False,
            has_newer=has_more if direction == "newer" else False,
        )

    oldest = entries[0].timeline_sequence
    newest = entries[-1].timeline_sequence
    has_older = (
        has_more
        if direction == "older"
        else _exists_before(session, conversation_id, oldest)
    )
    has_newer = (
        has_more
        if direction == "newer"
        else _exists_after(session, conversation_id, newest)
    )
    return TimelinePage(
        entries=entries,
        older_cursor=(
            _encode_cursor(
                key=cursor_key,
                conversation_id=conversation_id,
                anchor=oldest,
                direction="older",
            )
            if has_older
            else None
        ),
        newer_cursor=_encode_cursor(
            key=cursor_key,
            conversation_id=conversation_id,
            anchor=newest,
            direction="newer",
        ),
        has_older=has_older,
        has_newer=has_newer,
    )


def _exists_before(session, conversation_id: str, sequence: int) -> bool:
    return bool(
        session.query(ConversationEvent.event_id)
        .filter(
            ConversationEvent.conversation_id == conversation_id,
            ConversationEvent.timeline_sequence < sequence,
        )
        .first()
    )


def _exists_after(session, conversation_id: str, sequence: int) -> bool:
    return bool(
        session.query(ConversationEvent.event_id)
        .filter(
            ConversationEvent.conversation_id == conversation_id,
            ConversationEvent.timeline_sequence > sequence,
        )
        .first()
    )


def _entry(keyring: KeyRing, event: ConversationEvent) -> TimelineEntry:
    plaintext = keyring.decrypt(
        event.encrypted_content,
        table=_TABLE,
        column=_COLUMN,
        row_id=event.event_id,
    )
    return TimelineEntry(
        event_id=event.event_id,
        event_type=event.event_type,
        operation_id=event.operation_id,
        created_at=event.created_at,
        content=json.loads(plaintext.decode("utf-8")),
        timeline_sequence=event.timeline_sequence,
        session_id=event.session_id,
        turn_id=event.turn_id,
    )


# -- cursors --------------------------------------------------------------


def _encode_cursor(
    *,
    key: HmacKey | HmacKeyRing,
    conversation_id: str,
    anchor: int,
    direction: str,
) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "tl": conversation_id,
        "anchor": anchor,
        "dir": direction,
    }
    raw = canonical_json(payload).encode("utf-8")
    signature = hmac.new(key.secret, raw, hashlib.sha256).digest()
    return ".".join(
        base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        for value in (raw, signature)
    )


def _decode_cursor(
    cursor: str,
    *,
    key: HmacKey | HmacKeyRing,
    conversation_id: str,
    direction: str,
) -> int:
    """Return the anchor sequence, or refuse.

    Every rejection is the same outward code. A cursor that was tampered with,
    one minted for the other direction, and one from another Timeline are all
    "this cursor is not usable here"; distinguishing them would tell a caller
    which guess was closer.
    """
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        raw = _b64u(encoded_payload)
        supplied = _b64u(encoded_signature)
        if not any(
            hmac.compare_digest(
                supplied,
                hmac.new(candidate.secret, raw, hashlib.sha256).digest(),
            )
            for candidate in _hmac_keys(key)
        ):
            raise ValueError("signature mismatch")
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or payload.get("v") != CURSOR_VERSION
            or payload.get("tl") != conversation_id
            or payload.get("dir") != direction
            or type(payload.get("anchor")) is not int
            or payload["anchor"] < 1
        ):
            raise ValueError("invalid cursor payload")
        return payload["anchor"]
    except (
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
        AttributeError,
        TypeError,
    ) as exc:
        raise AppError(
            ErrorCode.INVALID_CURSOR,
            internal_detail="timeline cursor is invalid for this request",
        ) from exc


def _b64u(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
