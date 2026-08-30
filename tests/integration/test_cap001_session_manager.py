"""Regression coverage for CAP-001 Session transaction and idle semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_agent.api import events
from personal_agent.context.config import default_context_config
from personal_agent.context.session_manager import (
    CompactSessionState,
    SessionManager,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    ContextSession,
    Conversation,
    ConversationAlias,
)
from personal_agent_core.crypto import KeyRing, generate_key


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
TIMELINE_ID = "tl-session-tests"


class ContinueClassifier:
    def __init__(self) -> None:
        self.calls = []

    def classify(self, request):
        self.calls.append(request)
        return {
            "decision": "continue_session",
            "reason": "task_boundary",
            "confidence_band": "high",
        }


class SessionStateProvider:
    def compact_state(self, db, *, session):
        return CompactSessionState(
            topic_summary="继续完成当前任务",
            domain="general",
            task_state="active",
        )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(
            Conversation(
                conversation_id=TIMELINE_ID,
                created_at=NOW - timedelta(hours=2),
                next_sequence=1,
                is_canonical=True,
            )
        )
        session.add(
            ContextSession(
                session_id="ses-current",
                conversation_id=TIMELINE_ID,
                status="open",
                relation_kind="new_topic",
                opened_at=NOW - timedelta(minutes=100),
            )
        )
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-test", state="active")],
        service="personal-agent-api",
    )


def test_boundary_race_preserves_the_outer_transaction(engine) -> None:
    """The race loser keeps work created before the Session savepoint."""
    with session_factory(engine)() as session:
        session.add(
            ConversationAlias(
                alias_hmac="outer-write",
                conversation_id=TIMELINE_ID,
                created_at=NOW,
            )
        )
        session.flush()

        decision = SessionManager(default_context_config())._open_new(
            session,
            conversation_id=TIMELINE_ID,
            previous=None,
            reason="explicit_reset",
            relation_kind="new_topic",
            parent=None,
            now=NOW,
            classifier_version=None,
            confidence_band=None,
        )

        assert decision.decision == "continue_session"
        assert decision.session_id == "ses-current"
        assert session.get(ConversationAlias, "outer-write") is not None
        session.commit()

    with session_factory(engine)() as session:
        assert session.get(ConversationAlias, "outer-write") is not None


def test_appending_an_event_refreshes_the_session_activity(
    engine, keyring: KeyRing
) -> None:
    recent = NOW - timedelta(minutes=1)
    with session_factory(engine)() as session:
        events.append_event(
            session,
            keyring,
            conversation_id=TIMELINE_ID,
            session_id="ses-current",
            turn_id="trn-recent",
            event_type=events.USER_MESSAGE,
            content={"text": "recent"},
            operation_id=None,
            now=recent,
        )
        session.commit()

    with session_factory(engine)() as session:
        stored = session.get(ContextSession, "ses-current")
        assert stored is not None
        assert stored.last_event_at == recent


def test_idle_time_is_input_not_a_boundary_by_itself(engine) -> None:
    classifier = ContinueClassifier()
    # A wide idle window keeps the deterministic idle boundary out of the way:
    # this test pins the classifier still seeing the idle input (100 minutes)
    # and deciding on semantics, not the 60-minute hard cutoff.
    manager = SessionManager(
        default_context_config({"CONTEXT_SESSION_IDLE_MINUTES": 600}),
        classifier=classifier,
        state_provider=SessionStateProvider(),
    )

    with session_factory(engine)() as session:
        decision = manager.select_session(
            session,
            conversation_id=TIMELINE_ID,
            user_text="继续完成同一个任务",
            now=NOW,
        )

    assert decision.decision == "continue_session"
    assert len(classifier.calls) == 1
    assert classifier.calls[0].minutes_since_last_event == 100
