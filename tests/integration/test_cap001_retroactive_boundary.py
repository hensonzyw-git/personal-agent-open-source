"""CAP-001 F-D15 / F-H16: the guards of `apply_retroactive_boundary`.

The classifier runs outside the request path, so its answer can be stale by
the time it is applied. Four guards make a stale `open_new_session` answer a
no-op (`src/personal_agent/context/session_manager.py`, "Move one completed
turn"): the operation must be terminal, its events must still sit in the
source Session, that Session must still be the open one, no later event may
rely on it, and no active Checkpoint may cover its range.

The API-level tests (`test_agent_api.py::test_async_classifier_reassigns_a_completed_new_topic_turn`,
`::test_async_classifier_never_splits_a_parked_clarification`) cover one
accepted application and one skip; they cannot reach the individual guards
without walking a real chat through each perturbation. This file calls the
manager directly so every guard is exercised in isolation, against the same
production transaction wrapper the API uses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text as sql

from personal_agent.api import events
from personal_agent.context.config import default_context_config
from personal_agent.context.session_manager import (
    ClassifierOutcome,
    ResolvedClassification,
    SessionManager,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    ApiRequest,
    ContextCheckpoint,
    ContextSession,
    Conversation,
    Device,
    Operation,
)
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.sqlite import run_write_transaction


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
TIMELINE = "tl-retroactive"
OPEN_NEW = ResolvedClassification(
    expected_session_id="ses-1",
    expected_last_event_at=NOW,
    expected_timeline_sequence=2,
    outcome=ClassifierOutcome(
        decision="open_new_session",
        reason="task_boundary",
        confidence_band="high",
    ),
)


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id="dev-1",
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint="THUMB",
                status="active",
                scopes="[]",
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.add(
            Conversation(
                conversation_id=TIMELINE,
                created_at=NOW - timedelta(hours=1),
                next_sequence=1,
                is_canonical=True,
            )
        )
        session.add(
            ContextSession(
                session_id="ses-1",
                conversation_id=TIMELINE,
                status="open",
                relation_kind="new_topic",
                opened_at=NOW - timedelta(minutes=30),
                last_event_at=NOW,
            )
        )
        session.commit()
    yield engine
    engine.dispose()


def _anchored_operation(
    engine, keyring: KeyRing, *, operation_id: str = "op-1"
) -> None:
    """One terminal operation whose user event anchors it in `ses-1`."""
    with session_factory(engine)() as session:
        session.add(
            ApiRequest(
                request_id=f"req-{operation_id}",
                device_id="dev-1",
                client_request_id=f"client-{operation_id}",
                request_fingerprint=f"fp-{operation_id}",
                received_at=NOW,
            )
        )
        session.add(
            Operation(
                operation_id=operation_id,
                request_id=f"req-{operation_id}",
                trace_id=f"tr-{operation_id}",
                idempotency_key=f"key-{operation_id}",
                state="succeeded",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        events.append_event(
            session,
            keyring,
            conversation_id=TIMELINE,
            session_id="ses-1",
            turn_id=f"trn-{operation_id}",
            event_type=events.USER_MESSAGE,
            content={"text": "帮我规划周末爬山"},
            operation_id=operation_id,
            now=NOW,
        )
        session.commit()


def _apply(engine, *, operation_id: str = "op-1"):
    """Apply the classification the way the API does, inside a retryable
    write transaction, and return whether the turn was reassigned."""
    with session_factory(engine)() as session:
        def work():
            return SessionManager(default_context_config()).apply_retroactive_boundary(
                session,
                conversation_id=TIMELINE,
                operation_id=operation_id,
                resolved=OPEN_NEW,
                now=NOW,
            )

        decision = run_write_transaction(session, work)
        session.commit()
        return decision


def _event_session(engine, operation_id: str = "op-1") -> str | None:
    with session_factory(engine)() as session:
        row = session.execute(
            sql(
                "SELECT session_id FROM conversation_events "
                "WHERE operation_id = :oid"
            ),
            {"oid": operation_id},
        ).scalar_one_or_none()
        return row


# -- accepted path ------------------------------------------------------------


def test_a_compliant_completed_turn_is_reassigned(engine, keyring) -> None:
    _anchored_operation(engine, keyring)

    decision = _apply(engine)

    assert decision is not None
    assert decision.decision == "open_new_session"
    assert _event_session(engine) != "ses-1"
    with session_factory(engine)() as session:
        assert session.get(ContextSession, "ses-1").status == "closed"


# -- guard: operation_not_terminal -------------------------------------------


def test_a_non_terminal_operation_is_never_reassigned(engine, keyring) -> None:
    _anchored_operation(engine, keyring)
    with session_factory(engine)() as session:
        operation = session.get(Operation, "op-1")
        operation.state = "verifying"
        session.commit()

    assert _apply(engine) is None
    assert _event_session(engine) == "ses-1"


# -- guard: operation_events_changed -----------------------------------------


def test_an_operation_that_left_the_source_session_is_never_reassigned(
    engine, keyring
) -> None:
    _anchored_operation(engine, keyring)
    with session_factory(engine)() as session:
        # The unique open-session constraint allows only one open Session, so
        # park ses-1 to stage the move the guard must refuse.
        parked = session.get(ContextSession, "ses-1")
        parked.status = "closed"
        parked.closed_at = NOW
        session.add(
            ContextSession(
                session_id="ses-2",
                conversation_id=TIMELINE,
                status="open",
                relation_kind="new_topic",
                opened_at=NOW,
                last_event_at=NOW,
            )
        )
        session.flush()
        session.execute(
            sql(
                "UPDATE conversation_events SET session_id = 'ses-2' "
                "WHERE operation_id = 'op-1'"
            )
        )
        session.commit()

    assert _apply(engine) is None
    assert _event_session(engine) == "ses-2"


def test_an_operation_without_events_is_never_reassigned(engine, keyring) -> None:
    _anchored_operation(engine, keyring)
    with session_factory(engine)() as session:
        session.execute(
            sql("DELETE FROM conversation_events WHERE operation_id = 'op-1'")
        )
        session.commit()

    assert _apply(engine) is None


# -- guard: open_session_changed ---------------------------------------------


def test_a_source_session_that_is_no_longer_open_is_never_reassigned(
    engine, keyring
) -> None:
    _anchored_operation(engine, keyring)
    with session_factory(engine)() as session:
        # The unique open-session constraint allows only one open Session, so
        # close ses-1 and open a successor first.
        closed = session.get(ContextSession, "ses-1")
        closed.status = "closed"
        closed.closed_at = NOW
        session.add(
            ContextSession(
                session_id="ses-2",
                conversation_id=TIMELINE,
                status="open",
                relation_kind="new_topic",
                opened_at=NOW,
                last_event_at=NOW,
            )
        )
        session.commit()

    assert _apply(engine) is None
    assert _event_session(engine) == "ses-1"


# -- guard: later_event_exists ------------------------------------------------


def test_a_turn_with_a_later_event_in_its_session_is_never_reassigned(
    engine, keyring
) -> None:
    _anchored_operation(engine, keyring)
    with session_factory(engine)() as session:
        events.append_event(
            session,
            keyring,
            conversation_id=TIMELINE,
            session_id="ses-1",
            turn_id="trn-later",
            event_type=events.USER_MESSAGE,
            content={"text": "再帮我看看天气"},
            operation_id=None,
            now=NOW + timedelta(minutes=1),
        )
        session.commit()

    assert _apply(engine) is None
    assert _event_session(engine) == "ses-1"


# -- guard: checkpointed -------------------------------------------------------


def test_a_turn_covered_by_an_active_checkpoint_is_never_reassigned(
    engine, keyring
) -> None:
    _anchored_operation(engine, keyring)
    with session_factory(engine)() as session:
        anchor = session.execute(
            sql(
                "SELECT timeline_sequence FROM conversation_events "
                "WHERE operation_id = 'op-1'"
            ),
            {"oid": "op-1"},
        ).scalar_one()
        checkpoint_id = "ckpt-retroactive"
        session.add(
            ContextCheckpoint(
                checkpoint_id=checkpoint_id,
                session_id="ses-1",
                parent_checkpoint_id=None,
                status="active",
                covered_from_sequence=1,
                covered_through_sequence=anchor,
                encrypted_payload=keyring.encrypt(
                    b"{}",
                    table="context_checkpoints",
                    column="encrypted_payload",
                    row_id=checkpoint_id,
                ),
                source_hash="retroactive-source",
                schema_version="context_checkpoint_v1",
                compactor_version="test",
                estimated_tokens=0,
                created_at=NOW,
            )
        )
        session.commit()

    assert _apply(engine) is None
    assert _event_session(engine) == "ses-1"


def test_a_checkpoint_that_does_not_reach_the_turn_does_not_block(engine, keyring) -> None:
    """Only coverage of the turn's own range is the guard's evidence."""
    with session_factory(engine)() as session:
        events.append_event(
            session,
            keyring,
            conversation_id=TIMELINE,
            session_id="ses-1",
            turn_id="trn-earlier",
            event_type=events.USER_MESSAGE,
            content={"text": "先聊点别的"},
            operation_id=None,
            now=NOW - timedelta(minutes=1),
        )
        session.commit()
    _anchored_operation(engine, keyring)
    with session_factory(engine)() as session:
        anchor = session.execute(
            sql(
                "SELECT timeline_sequence FROM conversation_events "
                "WHERE operation_id = 'op-1'"
            ),
            {"oid": "op-1"},
        ).scalar_one()
        checkpoint_id = "ckpt-short"
        session.add(
            ContextCheckpoint(
                checkpoint_id=checkpoint_id,
                session_id="ses-1",
                parent_checkpoint_id=None,
                status="active",
                covered_from_sequence=1,
                # Stops one sequence short of the anchored event: the earlier
                # message is summarized, the classified turn is not covered.
                covered_through_sequence=anchor - 1,
                encrypted_payload=keyring.encrypt(
                    b"{}",
                    table="context_checkpoints",
                    column="encrypted_payload",
                    row_id=checkpoint_id,
                ),
                source_hash="short-source",
                schema_version="context_checkpoint_v1",
                compactor_version="test",
                estimated_tokens=0,
                created_at=NOW,
            )
        )
        session.commit()

    assert _apply(engine) is not None
    assert _event_session(engine) != "ses-1"
