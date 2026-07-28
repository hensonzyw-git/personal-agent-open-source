"""Build real `ContextEnvelope` values for tests that are not about the builder.

A `ContextEnvelope` cannot be constructed directly -- it carries the Budgeter's
witness -- and that is deliberate. So tests of the gateway, the interpreter and
the orchestrator use the **real** `ContextBuilder` over a throwaway SQLite
database instead of a hand-written stand-in.

That is not just a workaround for the witness. A fake envelope written from the
same assumptions as the code under test could only ever confirm those
assumptions (`CLAUDE.md` §5.1); an envelope the real builder produced is the
shape the gateway will actually receive in production, including its untrusted
framing and its component ordering.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from cap001_fixtures import IDENTIFIER_KEY
from personal_agent.api import events
from personal_agent.context.builder import ContextBuilder, ContextEnvelope
from personal_agent.context.compactor import Compactor
from personal_agent.context.config import default_context_config
from personal_agent.policy.bridge import VisibleTool
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import ContextSession, Conversation
from personal_agent_core.crypto import KeyRing, generate_key


ENVELOPE_NOW = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
ENVELOPE_TIMELINE = "tl_envelope_fixture"
ENVELOPE_SESSION = "ses_envelope_fixture"


def envelope_for(
    tmp_path: Path,
    *,
    user_text: str = "午饭 45 个人支出",
    system: str = "SYS",
    tools: Sequence[VisibleTool] = (),
    history: Sequence[str] = (),
    config=None,
) -> ContextEnvelope:
    """One envelope built by the production builder over a fresh database."""
    with _database(tmp_path) as (session, keyring):
        for index, text in enumerate(history):
            events.append_event(
                session,
                keyring,
                conversation_id=ENVELOPE_TIMELINE,
                session_id=ENVELOPE_SESSION,
                turn_id=f"trn-{index}",
                event_type=events.USER_MESSAGE,
                content={"text": text},
                operation_id=None,
                now=ENVELOPE_NOW + timedelta(seconds=index),
            )
        current = events.append_event(
            session,
            keyring,
            conversation_id=ENVELOPE_TIMELINE,
            session_id=ENVELOPE_SESSION,
            turn_id="trn-current",
            event_type=events.USER_MESSAGE,
            content={"text": user_text},
            operation_id=None,
            now=ENVELOPE_NOW + timedelta(minutes=1),
        )
        session.commit()
        resolved = config or default_context_config()
        builder = ContextBuilder(resolved, compactor=Compactor(resolved))
        return builder.build(
            session,
            keyring,
            IDENTIFIER_KEY,
            conversation_id=ENVELOPE_TIMELINE,
            session_id=ENVELOPE_SESSION,
            current_event_id=current,
            system_instruction=system,
            user_text=user_text,
            effective_tools=list(tools),
        )


@contextmanager
def _database(tmp_path: Path) -> Iterator[tuple[Any, KeyRing]]:
    engine = create_database_engine(tmp_path / "envelope.sqlite")
    create_all(engine)
    keyring = KeyRing(
        [generate_key("envelope-fixture", state="active")],
        service="personal-agent-api",
    )
    try:
        with session_factory(engine)() as session:
            session.add(
                Conversation(
                    conversation_id=ENVELOPE_TIMELINE,
                    created_at=ENVELOPE_NOW,
                    next_sequence=1,
                    is_canonical=True,
                )
            )
            session.add(
                ContextSession(
                    session_id=ENVELOPE_SESSION,
                    conversation_id=ENVELOPE_TIMELINE,
                    status="open",
                    relation_kind="new_topic",
                    opened_at=ENVELOPE_NOW,
                )
            )
            session.commit()
            yield session, keyring
    finally:
        engine.dispose()
