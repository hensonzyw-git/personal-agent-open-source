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
from personal_agent.api.operation_store import open_operation
from personal_agent.context.builder import ContextBuilder, ContextEnvelope
from personal_agent.context.compactor import Compactor
from personal_agent.context.config import default_context_config
from personal_agent.context.continuation import (
    ClarificationContext,
    FinanceRetryContext,
)
from personal_agent.policy.bridge import VisibleTool
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import ContextSession, Conversation, Device
from personal_agent_core.crypto import KeyRing, generate_key


ENVELOPE_NOW = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
ENVELOPE_TIMELINE = "tl_envelope_fixture"
ENVELOPE_SESSION = "ses_envelope_fixture"
ENVELOPE_DEVICE = "dev_envelope_fixture"


def envelope_for(
    tmp_path: Path,
    *,
    user_text: str = "午饭 45 个人支出",
    system: str = "SYS",
    tools: Sequence[VisibleTool] = (),
    history: Sequence[str] = (),
    clarification: ClarificationContext | None = None,
    finance_retry: FinanceRetryContext | None = None,
    materialize_clarification_sources: bool = True,
    max_session_event_scan: int = 400,
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
        resolved_clarification = clarification
        if clarification is not None and materialize_clarification_sources:
            source_ids: list[str] = []
            source_texts = [
                clarification.original_user_text,
                *(item.answer for item in clarification.completed_exchanges),
            ]
            source_questions = [
                *(item.question for item in clarification.completed_exchanges),
                clarification.question,
            ]
            for index, (source_text, question) in enumerate(
                zip(source_texts, source_questions, strict=True)
            ):
                opened = open_operation(
                    session,
                    device_id=ENVELOPE_DEVICE,
                    client_request_id=f"envelope-source-{index}",
                    request_fingerprint=f"source-fingerprint-{index}",
                    now=ENVELOPE_NOW + timedelta(seconds=10 + index),
                )
                source = opened.operation
                source_ids.append(source.operation_id)
                events.append_event(
                    session,
                    keyring,
                    conversation_id=ENVELOPE_TIMELINE,
                    session_id=ENVELOPE_SESSION,
                    turn_id=f"trn-source-{index}",
                    event_type=events.USER_MESSAGE,
                    content={"text": source_text},
                    operation_id=source.operation_id,
                    now=ENVELOPE_NOW + timedelta(seconds=10 + index),
                )
                events.append_event(
                    session,
                    keyring,
                    conversation_id=ENVELOPE_TIMELINE,
                    session_id=ENVELOPE_SESSION,
                    turn_id=f"trn-source-{index}",
                    event_type=events.OPERATION_RESULT,
                    content={
                        "state": "waiting_for_clarification",
                        "clarification": question,
                    },
                    operation_id=source.operation_id,
                    now=ENVELOPE_NOW + timedelta(seconds=11 + index),
                )
                source.state = "cancelled_pre_submit"
                source.state_version = 2
                source.safe_result = question
            resolved_clarification = ClarificationContext(
                original_user_text=clarification.original_user_text,
                question=clarification.question,
                completed_exchanges=clarification.completed_exchanges,
                source_operation_ids=tuple(source_ids),
            )
        resolved_retry = finance_retry
        if finance_retry is not None and materialize_clarification_sources:
            # The Builder verifies that a retry's source operation belongs to
            # this Session, so the fixture materializes it the same way the
            # API does: one failed operation with its user message and a
            # terminal failed_safe result, referenced by sealed source id.
            opened = open_operation(
                session,
                device_id=ENVELOPE_DEVICE,
                client_request_id="envelope-retry-source",
                request_fingerprint="retry-source-fingerprint",
                now=ENVELOPE_NOW + timedelta(seconds=30),
            )
            retry_source = opened.operation
            events.append_event(
                session,
                keyring,
                conversation_id=ENVELOPE_TIMELINE,
                session_id=ENVELOPE_SESSION,
                turn_id="trn-retry-source",
                event_type=events.USER_MESSAGE,
                content={"text": finance_retry.original_user_text},
                operation_id=retry_source.operation_id,
                now=ENVELOPE_NOW + timedelta(seconds=30),
            )
            events.append_event(
                session,
                keyring,
                conversation_id=ENVELOPE_TIMELINE,
                session_id=ENVELOPE_SESSION,
                turn_id="trn-retry-source",
                event_type=events.OPERATION_RESULT,
                content={
                    "state": "failed_safe",
                    "failure_reason": finance_retry.source_failure_reason,
                },
                operation_id=retry_source.operation_id,
                now=ENVELOPE_NOW + timedelta(seconds=31),
            )
            retry_source.state = "failed_safe"
            retry_source.state_version = 2
            retry_source.failure_reason = finance_retry.source_failure_reason
            resolved_retry = FinanceRetryContext(
                original_user_text=finance_retry.original_user_text,
                completed_exchanges=finance_retry.completed_exchanges,
                source_operation_id=retry_source.operation_id,
                source_failure_reason=finance_retry.source_failure_reason,
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
        builder = ContextBuilder(
            resolved,
            compactor=Compactor(resolved),
            max_session_event_scan=max_session_event_scan,
        )
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
            clarification_context=resolved_clarification,
            finance_retry_context=resolved_retry,
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
                Device(
                    device_id=ENVELOPE_DEVICE,
                    display_name="Envelope fixture",
                    public_key="fixture-public-key",
                    device_key_thumbprint="fixture-thumbprint",
                    status="active",
                    scopes="[]",
                    allowed_tools_version="fixture-v1",
                    created_at=ENVELOPE_NOW,
                )
            )
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
