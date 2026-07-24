"""Conversation events: the sealed, permanently-retained chat timeline.

Every user message and every operation outcome is appended here as a sealed
event (design 4: conversation events are retained permanently and always sealed).
The content is AEAD-bound by AAD to its own row, so an event cannot be lifted to
another conversation. Retention is not permission to feed the archive to a model;
this module only appends and lists the owner's own timeline.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from personal_agent.storage.models import Conversation, ConversationEvent
from personal_agent_core.crypto import KeyRing


_TABLE = "conversation_events"
_COLUMN = "encrypted_content"

USER_MESSAGE = "user_message"
OPERATION_RESULT = "operation_result"


@dataclass(frozen=True)
class TimelineEntry:
    event_id: str
    event_type: str
    operation_id: str | None
    created_at: datetime
    content: dict[str, Any]


def ensure_conversation(session, conversation_id: str, *, now: datetime) -> None:
    if session.get(Conversation, conversation_id) is None:
        session.add(
            Conversation(conversation_id=conversation_id, created_at=now)
        )
        session.flush()


def append_event(
    session,
    keyring: KeyRing,
    *,
    conversation_id: str,
    event_type: str,
    content: dict[str, Any],
    operation_id: str | None,
    now: datetime,
) -> str:
    """Seal one event onto its conversation and return the event id."""
    ensure_conversation(session, conversation_id, now=now)
    event_id = f"evt_{uuid.uuid4().hex}"
    envelope = keyring.encrypt(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        table=_TABLE,
        column=_COLUMN,
        row_id=event_id,
    )
    session.add(
        ConversationEvent(
            event_id=event_id,
            conversation_id=conversation_id,
            event_type=event_type,
            encrypted_content=envelope,
            operation_id=operation_id,
            created_at=now,
        )
    )
    conversation = session.get(Conversation, conversation_id)
    conversation.last_event_at = now
    session.flush()
    return event_id


def list_timeline(
    session, keyring: KeyRing, *, conversation_id: str
) -> list[TimelineEntry]:
    """The owner's decrypted timeline for one conversation, oldest first."""
    # `rowid` is the insertion order, the deterministic tiebreaker when two
    # events share a timestamp (the Agent database is SQLite-only).
    events = (
        session.query(ConversationEvent)
        .filter(ConversationEvent.conversation_id == conversation_id)
        .order_by(ConversationEvent.created_at, text("rowid"))
        .all()
    )
    timeline: list[TimelineEntry] = []
    for event in events:
        plaintext = keyring.decrypt(
            event.encrypted_content,
            table=_TABLE,
            column=_COLUMN,
            row_id=event.event_id,
        )
        timeline.append(
            TimelineEntry(
                event_id=event.event_id,
                event_type=event.event_type,
                operation_id=event.operation_id,
                created_at=event.created_at,
                content=json.loads(plaintext.decode("utf-8")),
            )
        )
    return timeline
