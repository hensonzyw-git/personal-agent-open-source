"""DEV-026 E: the FastAPI Client API contract, end to end over HTTP.

A real Agent SQLite database, a real access-token ring and Agent key ring, and
fakes for the model interpreter and the Finance dispatcher. The FastAPI
`TestClient` drives the actual routes, so idempotency, auth, cancellation and the
duplicate decision are tested as the wire sees them.
"""

from __future__ import annotations

import json
import asyncio
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from cap001_fixtures import CURSOR_KEY, IDENTIFIER_KEY
from personal_agent.api import app as agent_app
from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.api.finance_query_projection import (
    canonical_projection_json,
    decode_finance_query_projection,
    summarise_query_projection,
)
from personal_agent.api.finance_record_projection import FinanceExpenseRecord
from personal_agent.api.orchestrator import (
    Clarification,
    CommitClarificationZeroWrite,
    CommitFailedSafe,
    DirectAnswer,
    InterpreterError,
    PossibleDuplicate,
    ReadCompleted,
    Resolved,
    ToolCall,
    Written,
)
from personal_agent.api.intent import WriteIntent
from personal_agent.api.operation_state import StaleOperationVersionError
from personal_agent.auth.tokens import (
    SigningKey,
    TokenKeyRing,
    issue_access_token,
)
from personal_agent.context.budget import ComponentKind
from personal_agent.context.config import default_context_config
from personal_agent.context.session_manager import (
    CompactSessionState,
    SessionManager,
)
from personal_agent.diagnostics import transcript
from personal_agent.diagnostics.recording_dispatcher import RecordingDispatcher
from personal_agent.diagnostics.transcript import TranscriptRecorder
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from envelope_factory import envelope_factory
from personal_agent.storage.models import (
    ApiRequest,
    ContextSession,
    ContextCheckpoint,
    Conversation,
    ConversationEvent,
    Device,
    Operation,
)
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode, ModelFailureReason


NOW = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
REQUEST_ID_1 = "11111111-1111-4111-8111-111111111111"
REQUEST_ID_2 = "22222222-2222-4222-8222-222222222222"
REQUEST_ID_3 = "33333333-3333-4333-8333-333333333333"
REQUEST_ID_4 = "44444444-4444-4444-8444-444444444444"


def _query_total_result() -> dict:
    """A `finance.query_expenses` total result in the real output-schema shape."""
    return {
        "status": "ok",
        "view": "total",
        "filters_applied": {
            "date_range": {"start": "2026-01-01", "end": "2026-12-31"},
            "categories": ["网球"],
            "name_contains": [],
            "is_family_expense": "all",
            "personal_amount_cny": None,
        },
        "metric": "personal_spend_total_cny",
        "record_count": 3,
        "personal_spend_total_cny": "1200.00",
        "source_system": "feishu_bitable",
        "evidence": {
            "kind": "aggregate_query",
            "query_id": "qry_1",
            "config_checksum": "cfg",
            "schema_snapshot_checksum": "schema",
            "scanned_pages": 1,
            "matched_count": 3,
            "started_at": "2026-08-12T00:00:00Z",
            "completed_at": "2026-08-12T00:00:01Z",
        },
    }


class FakeInterpreter:
    def __init__(self, result) -> None:
        self.result = result

    def interpret(self, *, envelope):
        self.envelope = envelope
        return self.result


class FakeDispatcher:
    def __init__(self, *, resolve=None, commit=None) -> None:
        self._resolve = resolve
        self._commit = commit
        self.commit_calls: list[dict] = []

    def resolve(self, *, tool, model_args, idempotency_key=None):
        return self._resolve

    def commit(self, *, intent, idempotency_key, duplicate_override):
        self.commit_calls.append(
            {
                "idempotency_key": idempotency_key,
                "override": duplicate_override,
                # Recorded so a route that resolves its own intent -- the
                # category correction -- can be checked on what it actually
                # dispatched rather than only on what it answered.
                "intent": intent,
            }
        )
        return self._commit


@pytest.fixture()
def token_ring() -> TokenKeyRing:
    private = ec.generate_private_key(ec.SECP256R1())
    return TokenKeyRing(
        active=SigningKey("tok-2026", private, private.public_key())
    )


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent",
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
                scopes='["finance.write"]',
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        # `CAP-001`: a single-user deployment has exactly one canonical
        # Timeline. Seeding it as `c1` keeps these tests reading naturally while
        # making them exercise the real resolution path -- `c2` is then a
        # genuinely unknown id, not a second Timeline.
        session.add(
            Conversation(
                conversation_id="c1",
                created_at=NOW,
                next_sequence=1,
                is_canonical=True,
            )
        )
        session.commit()
    yield engine
    engine.dispose()


def _token(
    token_ring: TokenKeyRing,
    *,
    device_id: str = "dev-1",
    thumbprint: str = "THUMB",
) -> str:
    return issue_access_token(
        token_ring,
        device_id=device_id,
        device_key_thumbprint=thumbprint,
        scopes=["finance.write"],
        allowed_tools_version="v1",
        now=NOW,
    )


def _client(
    engine,
    token_ring,
    keyring,
    *,
    interpreter,
    dispatcher,
    dispatcher_traces=None,
    sync_wait_seconds=30.0,
    ledger_url=None,
    compact_session=None,
    recorder=None,
    session_manager=None,
) -> TestClient:
    def build_dispatcher(auth, trace_id):
        if dispatcher_traces is not None:
            dispatcher_traces.append(trace_id)
        return dispatcher

    deps = AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=token_ring,
        keyring=keyring,
        identifier_key=IDENTIFIER_KEY,
        cursor_key=CURSOR_KEY,
        build_interpreter=lambda auth: interpreter,
        build_envelope=envelope_factory(keyring),
        build_dispatcher=build_dispatcher,
        build_authorizer=lambda auth: (lambda *, tool, model_args: dict(model_args)),
        capabilities=lambda auth: [{"alias": "finance.log_expense"}],
        now=lambda: NOW,
        sync_wait_seconds=sync_wait_seconds,
        ledger_url=ledger_url,
        compact_session=compact_session,
        **({"session_manager": session_manager} if session_manager is not None else {}),
        **({"recorder": recorder} if recorder is not None else {}),
    )
    return TestClient(build_app(deps))


def _auth(token_ring, key=REQUEST_ID_1) -> dict:
    return {"Authorization": f"Bearer {_token(token_ring)}", "Idempotency-Key": key}


class _BoundaryClassifier:
    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls = []

    def classify(self, request):
        self.calls.append(request)
        return self.answer


class _BoundaryStateProvider:
    def compact_state(self, db, *, session):
        return CompactSessionState(
            topic_summary="已完成的旧话题",
            domain="chat",
            task_state="completed",
        )


def _boundary_manager(classifier: _BoundaryClassifier) -> SessionManager:
    return SessionManager(
        default_context_config(),
        classifier=classifier,
        state_provider=_BoundaryStateProvider(),
    )


def test_async_classifier_reassigns_a_completed_new_topic_turn(
    engine, token_ring, keyring
) -> None:
    classifier = _BoundaryClassifier(
        {
            "decision": "open_new_session",
            "reason": "task_boundary",
            "confidence_band": "high",
        }
    )
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(DirectAnswer("收到")),
        dispatcher=FakeDispatcher(),
        session_manager=_boundary_manager(classifier),
    )

    first = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "整理上周的项目复盘"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    second = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "帮我规划周末爬山"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    asyncio.run(client.app.state.drain_background_tasks())
    assert len(classifier.calls) == 1

    with session_factory(engine)() as session:
        sessions = (
            session.query(ContextSession)
            .order_by(ContextSession.opened_at, ContextSession.session_id)
            .all()
        )
        assert len(sessions) == 2
        moved = (
            session.query(ConversationEvent)
            .filter(ConversationEvent.operation_id == second.json()["operation_id"])
            .all()
        )
        assert moved
        moved_session_ids = {event.session_id for event in moved}
        assert len(moved_session_ids) == 1
        new = next(item for item in sessions if item.session_id in moved_session_ids)
        old = next(item for item in sessions if item.session_id != new.session_id)
        assert old.status == "closed"
        assert new.status == "open"
        assert new.boundary_reason == "task_boundary"
        original = (
            session.query(ConversationEvent)
            .filter(ConversationEvent.operation_id == first.json()["operation_id"])
            .all()
        )
        assert original and {event.session_id for event in original} == {old.session_id}
    assert len(classifier.calls) == 1


def test_async_classifier_never_splits_a_parked_clarification(
    engine, token_ring, keyring
) -> None:
    classifier = _BoundaryClassifier(
        {
            "decision": "open_new_session",
            "reason": "task_boundary",
            "confidence_band": "high",
        }
    )

    class SequencedInterpreter:
        def __init__(self) -> None:
            self.results = [DirectAnswer("收到"), Clarification("请确认分类")]

        def interpret(self, *, envelope):
            return self.results.pop(0)

    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=SequencedInterpreter(),
        dispatcher=FakeDispatcher(),
        session_manager=_boundary_manager(classifier),
    )
    client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "整理上周的项目复盘"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "帮我规划周末爬山"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert parked.json()["state"] == "waiting_for_clarification"
    asyncio.run(client.app.state.drain_background_tasks())

    with session_factory(engine)() as session:
        sessions = session.query(ContextSession).all()
        assert len(sessions) == 1
        assert sessions[0].status == "open"
    assert len(classifier.calls) == 1


# --- auth --------------------------------------------------------------------


def test_the_persisted_operation_trace_reaches_the_dispatcher(
    engine, token_ring, keyring
) -> None:
    traces: list[str] = []
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
        dispatcher_traces=traces,
    )
    response = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers=_auth(token_ring),
    )
    assert response.status_code == 200
    with session_factory(engine)() as session:
        operation = session.query(Operation).one()
        assert traces == [operation.trace_id]


def test_a_request_without_a_token_is_unauthenticated(engine, token_ring, keyring):
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers={"Idempotency-Key": REQUEST_ID_1},
    )
    assert resp.status_code == 401


def test_a_missing_idempotency_key_is_a_400(engine, token_ring, keyring) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert resp.status_code == 400


def test_a_non_uuidv4_idempotency_key_is_a_400(engine, token_ring, keyring) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers=_auth(token_ring, key="req-1"),
    )
    assert resp.status_code == 400


def test_chat_requires_application_json(engine, token_ring, keyring) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    resp = client.post(
        "/v1/chat/messages",
        content=b'{"conversation_id":"c1","text":"hi"}',
        headers={
            **_auth(token_ring),
            "Content-Type": "text/plain",
        },
    )
    assert resp.status_code == 400


def test_chat_rejects_a_body_over_64_kib(engine, token_ring, keyring) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "x" * (64 * 1024)},
        headers=_auth(token_ring),
    )
    assert resp.status_code == 400


# --- chat + idempotency ------------------------------------------------------


def test_a_direct_answer_returns_a_succeeded_operation(engine, token_ring, keyring):
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("你好")),
        dispatcher=FakeDispatcher(),
    )
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers=_auth(token_ring),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "succeeded"
    assert body["answer"] == "你好"
    assert body["record_id"] is None

    polled = client.get(
        f"/v1/operations/{body['operation_id']}",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert polled.json()["answer"] == "你好"
    assert polled.json()["record_id"] is None


def test_a_resolved_write_returns_the_record_id(engine, token_ring, keyring) -> None:
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    dispatcher = FakeDispatcher(resolve=Resolved(intent), commit=Written("recABC"))
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=dispatcher,
    )
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring),
    )
    assert resp.status_code == 200
    assert resp.json()["record_id"] == "recABC"


def test_replaying_the_same_message_does_not_write_twice(engine, token_ring, keyring):
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    dispatcher = FakeDispatcher(resolve=Resolved(intent), commit=Written("recABC"))
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=dispatcher,
    )
    body = {"conversation_id": "c1", "text": "午饭 45"}
    first = client.post("/v1/chat/messages", json=body, headers=_auth(token_ring))
    second = client.post("/v1/chat/messages", json=body, headers=_auth(token_ring))
    assert first.json()["operation_id"] == second.json()["operation_id"]
    # The replay returned the stored operation without a second Finance write.
    assert len(dispatcher.commit_calls) == 1


def test_the_same_key_with_a_different_body_conflicts(engine, token_ring, keyring):
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("ok")),
        dispatcher=FakeDispatcher(),
    )
    client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "one"},
        headers=_auth(token_ring),
    )
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "two"},
        headers=_auth(token_ring),
    )
    assert resp.status_code == 409


# --- the parts boundary (§3.1) ----------------------------------------------
#
# Text and parts are two forms of one request, and the boundary decides which
# one it is before anything is persisted. A refusal here has to leave nothing
# behind: §3.1's "非法 part 不落事件、不创建 operation" is what separates a
# rejected request from a half-accepted one.


def _parts_client(engine, token_ring, keyring):
    return _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("ok")),
        dispatcher=FakeDispatcher(),
    )


def _chat_rows(engine) -> tuple[int, int]:
    with session_factory(engine)() as session:
        return (
            session.query(ApiRequest).count(),
            session.query(Operation).count(),
        )


def test_a_parts_request_is_refused_while_the_chain_is_incomplete(
    engine, token_ring, keyring
) -> None:
    """A photo the model never receives must be refused, not acknowledged.

    Two halves are landed -- the model-input chain (#12) and §8's composed
    switch (#13) -- and the switch's verdict is read, not assumed: it refuses
    here because this deployment has no scanner exemption and no media surface,
    not because a constant in the guard says so. What is still missing is §6's
    authorized read, so an accepted request would anchor an image that nothing
    can turn into a model input. §3.1 requires the refusal, and it must happen
    "在任何模型调用前" -- so nothing is persisted either.

    The refusal names both reasons, which is the point of composing the detail
    from the switch rather than from a build-time list.
    """
    client = _parts_client(engine, token_ring, keyring)
    resp = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "parts": [
                {"type": "text", "text": "这张账单记一下"},
                {"type": "image_ref", "media_id": "media_1"},
            ],
        },
        headers=_auth(token_ring),
    )
    assert resp.status_code != 200
    assert resp.json()["error"]["code"] == "UNSUPPORTED_OPERATION"
    assert _chat_rows(engine) == (0, 0)


def test_capabilities_reports_images_off_when_nothing_composed_them(
    engine, token_ring, keyring
) -> None:
    """§8's same-source rule, from the client's side of it.

    `_client` builds `AgentApiDeps` by hand, so what this pins is the default:
    a service that composed no provider, no media and no approvals must not
    advertise images. It also pins the field's shape, which the iOS side reads
    as the sole authority for whether to offer the photo button.
    """
    client = _parts_client(engine, token_ring, keyring)

    resp = client.get("/v1/capabilities", headers=_auth(token_ring))

    assert resp.status_code == 200
    assert resp.json()["images"] == {"enabled": False}


def test_a_structurally_bad_part_is_not_reported_as_not_ready(
    engine, token_ring, keyring
) -> None:
    """The two refusals answer different questions and must stay apart.

    Reporting a malformed part as "not available yet" would tell a client to
    retry later something that can never succeed, and would hide the typo that
    caused it.
    """
    client = _parts_client(engine, token_ring, keyring)
    resp = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "parts": [{"type": "image_ref", "media_id": "media_1", "data": "AAA"}],
        },
        headers=_auth(token_ring),
    )
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert _chat_rows(engine) == (0, 0)


def test_text_and_parts_together_are_refused(engine, token_ring, keyring) -> None:
    client = _parts_client(engine, token_ring, keyring)
    resp = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "这张账单记一下",
            "parts": [{"type": "image_ref", "media_id": "media_1"}],
        },
        headers=_auth(token_ring),
    )
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert _chat_rows(engine) == (0, 0)


def test_confirmed_new_topic_cancels_a_parked_operation_and_writes_a_divider(
    engine, token_ring, keyring
) -> None:
    interpreter = FakeInterpreter(Clarification("个人还是家庭支出？"))
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=interpreter,
        dispatcher=FakeDispatcher(),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert parked.status_code == 202
    assert parked.json()["state"] == "waiting_for_clarification"

    interpreter.result = DirectAnswer("新的话题已开始")
    reset = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "帮我规划周末",
            "start_new_session": True,
        },
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert reset.status_code == 200
    assert reset.json()["state"] == "succeeded"

    with session_factory(engine)() as session:
        parked_operation = session.get(Operation, parked.json()["operation_id"])
        assert parked_operation is not None
        assert parked_operation.state == "cancelled_pre_submit"
        sessions = (
            session.query(ContextSession)
            .all()
        )
        assert len(sessions) == 2
        closed = next(row for row in sessions if row.status == "closed")
        opened = next(row for row in sessions if row.status == "open")
        divider = (
            session.query(ConversationEvent)
            .filter(ConversationEvent.event_type == "session_divider")
            .one()
        )
        assert divider.session_id == opened.session_id
        reset_event = (
            session.query(ConversationEvent)
            .filter(ConversationEvent.operation_id == reset.json()["operation_id"])
            .filter(ConversationEvent.event_type == "user_message")
            .one()
        )
        assert reset_event.session_id == opened.session_id
        assert closed.session_id != opened.session_id


def test_new_topic_refuses_to_cross_an_operation_that_may_be_submitted(
    engine, token_ring, keyring
) -> None:
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(Clarification("个人还是家庭支出？")),
        dispatcher=FakeDispatcher(),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    with session_factory(engine)() as session:
        operation = session.get(Operation, parked.json()["operation_id"])
        assert operation is not None
        operation.state = "source_in_progress"
        operation.state_version += 1
        session.commit()

    refused = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "帮我规划周末",
            "start_new_session": True,
        },
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "PENDING_OPERATION_NOT_CANCELLABLE"
    with session_factory(engine)() as session:
        assert session.query(Operation).count() == 1
        assert session.query(ContextSession).count() == 1


def test_a_terminal_turn_rebuilds_an_invalid_checkpoint_in_the_background(
    engine, token_ring, keyring
) -> None:
    rebuilt: list[str] = []
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(DirectAnswer("ok")),
        dispatcher=FakeDispatcher(),
        compact_session=lambda _session, session_id: rebuilt.append(session_id),
    )
    first = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "先说一件事"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert first.status_code == 200
    with session_factory(engine)() as session:
        anchor = (
            session.query(ConversationEvent)
            .filter(ConversationEvent.operation_id == first.json()["operation_id"])
            .filter(ConversationEvent.event_type == "user_message")
            .one()
        )
        checkpoint_id = "ckpt-invalid-test"
        session.add(
            ContextCheckpoint(
                checkpoint_id=checkpoint_id,
                session_id=anchor.session_id,
                parent_checkpoint_id=None,
                status="invalid",
                covered_from_sequence=1,
                covered_through_sequence=1,
                encrypted_payload=keyring.encrypt(
                    b"{}",
                    table="context_checkpoints",
                    column="encrypted_payload",
                    row_id=checkpoint_id,
                ),
                source_hash="invalidated-source",
                schema_version="context_checkpoint_v1",
                compactor_version="test",
                estimated_tokens=0,
                created_at=NOW,
            )
        )
        session.commit()

    second = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "然后说另一件事"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert second.status_code == 200
    asyncio.run(client.app.state.drain_background_tasks())
    assert rebuilt == [anchor.session_id]


def test_repeated_clarification_is_exact_budgeted_and_not_duplicated(
    engine, token_ring, keyring
) -> None:
    class SequencedInterpreter:
        def __init__(self):
            self.calls = []
            self.results = [
                Clarification("个人还是家庭支出？"),
                Clarification("现金还是刷卡？"),
                ToolCall("finance.log_expense", {"name": "午饭"}),
            ]

        def interpret(self, *, envelope):
            self.calls.append(envelope)
            return self.results.pop(0)

    interpreter = SequencedInterpreter()
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=interpreter,
        dispatcher=FakeDispatcher(
            resolve=Resolved(intent), commit=Written("recCLARIFY")
        ),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert parked.status_code == 202
    assert parked.json()["state"] == "waiting_for_clarification"
    assert parked.json()["clarification"] == "个人还是家庭支出？"

    clarified_once = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "个人支出",
            "clarification_of": parked.json()["operation_id"],
        },
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert clarified_once.status_code == 202
    assert clarified_once.json()["clarification"] == "现金还是刷卡？"

    resumed = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "刷卡",
            "clarification_of": clarified_once.json()["operation_id"],
        },
        headers=_auth(token_ring, key=REQUEST_ID_3),
    )
    assert resumed.status_code == 200
    assert resumed.json()["record_id"] == "recCLARIFY"
    continuation = interpreter.calls[2]
    context = "\n".join(
        continuation.texts_of(ComponentKind.CLARIFICATION_CONTEXT)
    )
    history = "\n".join(continuation.texts_of(ComponentKind.RAW_EVENT))
    pending = "\n".join(
        continuation.texts_of(ComponentKind.PENDING_STATE)
    )
    assert continuation.user_text == "刷卡"
    assert context.count("午饭 45") == 1
    assert context.count("个人还是家庭支出？") == 1
    assert context.count("个人支出") == 1
    assert context.count("现金还是刷卡？") == 1
    assert "午饭 45" not in history
    assert "个人还是家庭支出？" not in history
    assert "个人支出" not in history
    assert "现金还是刷卡？" not in history
    assert resumed.json()["operation_id"] in pending

    old = client.get(
        f"/v1/operations/{parked.json()['operation_id']}",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert old.json()["state"] == "cancelled_pre_submit"
    assert old.json()["record_id"] is None


def test_finance_commit_question_is_persisted_for_the_next_continuation(
    engine, token_ring, keyring
) -> None:
    class SequencedInterpreter:
        def __init__(self):
            self.calls = []

        def interpret(self, *, envelope):
            self.calls.append(envelope)
            return ToolCall("finance.log_expense", {"name": "午饭"})

    class SequencedDispatcher:
        def __init__(self):
            self.commits = [
                CommitClarificationZeroWrite("这笔支出属于哪个分类？"),
                Written("recCOMMITCLARIFICATION"),
            ]

        def resolve(self, *, tool, model_args, idempotency_key=None):
            return Resolved(WriteIntent(tool, model_args))

        def commit(self, *, intent, idempotency_key, duplicate_override):
            return self.commits.pop(0)

    interpreter = SequencedInterpreter()
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=interpreter,
        dispatcher=SequencedDispatcher(),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert parked.json()["state"] == "waiting_for_clarification"
    assert parked.json()["clarification"] == "这笔支出属于哪个分类？"

    resumed = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "餐饮",
            "clarification_of": parked.json()["operation_id"],
        },
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )

    assert resumed.json()["record_id"] == "recCOMMITCLARIFICATION"
    context = "\n".join(
        interpreter.calls[1].texts_of(ComponentKind.CLARIFICATION_CONTEXT)
    )
    assert "这笔支出属于哪个分类？" in context
    assert interpreter.calls[1].finance_intent_required is True


def test_a_repeated_finance_commit_question_fails_safe_instead_of_reparking(
    engine, token_ring, keyring
) -> None:
    class Interpreter:
        def interpret(self, *, envelope):
            return ToolCall("finance.log_expense", {"name": "午饭"})

    class Dispatcher:
        def __init__(self):
            self.commits = [
                CommitClarificationZeroWrite("这笔支出属于哪个分类？"),
                CommitClarificationZeroWrite("这笔支出属于哪个分类？"),
            ]

        def resolve(self, *, tool, model_args, idempotency_key=None):
            return Resolved(WriteIntent(tool, model_args))

        def commit(self, *, intent, idempotency_key, duplicate_override):
            return self.commits.pop(0)

    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=Interpreter(),
        dispatcher=Dispatcher(),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    repeated = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "餐饮",
            "clarification_of": parked.json()["operation_id"],
        },
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )

    assert repeated.json()["state"] == "failed_safe"
    assert repeated.json()["failure_reason"] == "CLARIFICATION_REPEATED"


def test_clarified_explicit_family_expense_keeps_the_expense_tool_requirement(
    engine, token_ring, keyring
) -> None:
    class SequencedInterpreter:
        def __init__(self):
            self.calls = []
            self.results = [
                Clarification("这笔是昨天发生，还是很久以前发生？", reason="date"),
                ToolCall(
                    "finance.log_expense",
                    {
                        "name": "晚饭",
                        "input_amount": "283.99",
                        "input_currency": "CNY",
                        "occurred_on": "2026-08-25",
                        "is_family_expense": True,
                        "entry_kind": "expense",
                        "category": "餐饮",
                    },
                ),
            ]

        def interpret(self, *, envelope):
            self.calls.append(envelope)
            return self.results.pop(0)

    interpreter = SequencedInterpreter()
    intent = WriteIntent("finance.log_expense", {"name": "晚饭"})
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=interpreter,
        dispatcher=FakeDispatcher(
            resolve=Resolved(intent), commit=Written("recCLARIFIED_EXPENSE")
        ),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "昨天晚饭很久以前 283.99 家庭支出",
        },
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert parked.json()["state"] == "waiting_for_clarification"
    assert interpreter.calls[0].finance_clarification_required is True

    resumed = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "昨天",
            "clarification_of": parked.json()["operation_id"],
        },
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )

    assert resumed.json()["record_id"] == "recCLARIFIED_EXPENSE"
    continuation = interpreter.calls[1]
    assert continuation.finance_intent_required is True
    assert continuation.finance_required_tool == "finance.log_expense"
    assert continuation.finance_clarification_required is False
    context = "\n".join(
        continuation.texts_of(ComponentKind.CLARIFICATION_CONTEXT)
    )
    assert "昨天晚饭很久以前 283.99 家庭支出" in context
    assert "这笔是昨天发生，还是很久以前发生？" in context
    assert continuation.user_text == "昨天"


def test_explicit_retry_binds_the_latest_zero_write_finance_failure(
    engine, token_ring, keyring
) -> None:
    class SequencedInterpreter:
        def __init__(self):
            self.calls = []
            self.results = [
                InterpreterError(
                    "provider timed out",
                    failure_reason=ModelFailureReason.PROVIDER_TIMEOUT.value,
                ),
                DirectAnswer("Session 说明"),
                ToolCall("finance.log_expense", {"name": "午饭"}),
                DirectAnswer("不能再次重放"),
            ]

        def interpret(self, *, envelope):
            self.calls.append(envelope)
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    interpreter = SequencedInterpreter()
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=interpreter,
        dispatcher=FakeDispatcher(
            resolve=Resolved(intent), commit=Written("recRETRY")
        ),
    )
    failed = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "示例午餐 20 元"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert failed.json()["state"] == "failed_safe"
    assert failed.json()["failure_reason"] == ModelFailureReason.PROVIDER_TIMEOUT.value

    # Unrelated conversation may intervene; retry resolution is based on the
    # latest eligible operation, not adjacency in the raw transcript.
    meta = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "为什么没有新 session"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert meta.json()["state"] == "succeeded"

    retried = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "重新记"},
        headers=_auth(token_ring, key=REQUEST_ID_3),
    )
    assert retried.json()["record_id"] == "recRETRY"
    retry_envelope = interpreter.calls[2]
    exact = "\n".join(
        retry_envelope.texts_of(ComponentKind.CLARIFICATION_CONTEXT)
    )
    assert retry_envelope.finance_intent_required is True
    assert "finance_retry_context" in exact
    assert "示例午餐 20 元" in exact

    with session_factory(engine)() as session:
        source = session.get(Operation, failed.json()["operation_id"])
        retry = session.get(Operation, retried.json()["operation_id"])
        assert source is not None and retry is not None
        assert source.tool is None
        assert retry.retry_of_operation_id == source.operation_id

    # The same source is consumed once. A second vague retry has no sealed
    # accounting facts to replay and therefore fails closed as a Finance turn.
    second = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "重新记"},
        headers=_auth(token_ring, key=REQUEST_ID_4),
    )
    assert second.json()["state"] == "failed_safe"
    assert second.json()["failure_reason"] == "BOOKKEEPING_TOOL_REQUIRED"
    with session_factory(engine)() as session:
        second_op = session.get(Operation, second.json()["operation_id"])
        assert second_op is not None
        assert second_op.retry_of_operation_id is None


def test_more_than_twenty_five_ordinary_turns_do_not_hide_a_safe_retry(
    engine, token_ring, keyring
) -> None:
    class Interpreter:
        def __init__(self):
            self.first = True

        def interpret(self, *, envelope):
            if self.first:
                self.first = False
                raise InterpreterError("model down")
            if envelope.user_text == "重新记":
                return ToolCall("finance.log_expense", {"name": "午饭"})
            return DirectAnswer("普通聊天")

    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=Interpreter(),
        dispatcher=FakeDispatcher(
            resolve=Resolved(intent), commit=Written("rec-after-chat")
        ),
    )
    failed = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "示例午餐 20 元"},
        headers=_auth(token_ring, key=str(uuid.uuid4())),
    )
    assert failed.json()["failure_reason"] == "model_unavailable"

    for index in range(30):
        ordinary = client.post(
            "/v1/chat/messages",
            json={"conversation_id": "c1", "text": f"普通聊天第 {index} 条"},
            headers=_auth(token_ring, key=str(uuid.uuid4())),
        )
        assert ordinary.json()["state"] == "succeeded"

    retried = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "重新记"},
        headers=_auth(token_ring, key=str(uuid.uuid4())),
    )
    assert retried.json()["record_id"] == "rec-after-chat"
    with session_factory(engine)() as session:
        operation = session.get(Operation, retried.json()["operation_id"])
        assert operation is not None
        assert operation.retry_of_operation_id == failed.json()["operation_id"]


def test_two_concurrent_retries_fail_closed_without_a_server_error(
    engine, token_ring, keyring, monkeypatch
) -> None:
    source_client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=type(
            "Unavailable",
            (),
            {
                "interpret": lambda self, **_kwargs: (_ for _ in ()).throw(
                    InterpreterError("model down")
                )
            },
        )(),
        dispatcher=FakeDispatcher(),
    )
    failed = source_client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "示例午餐 20 元"},
        headers=_auth(token_ring, key=str(uuid.uuid4())),
    )
    assert failed.json()["failure_reason"] == "model_unavailable"

    original = agent_app._eligible_finance_retry
    lock = threading.Lock()
    calls = 0

    def race_once(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        with lock:
            calls += 1
            is_first = calls == 1
        # Keep the first anchor transaction open briefly so the other request
        # actually contends for SQLite's writer slot. The second must then
        # restart from fresh state and observe that the source was consumed.
        if is_first:
            time.sleep(0.2)
        return result

    monkeypatch.setattr(agent_app, "_eligible_finance_retry", race_once)
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    dispatcher = FakeDispatcher(
        resolve=Resolved(intent), commit=Written("rec-concurrent")
    )
    retry_client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(
            ToolCall("finance.log_expense", {"name": "午饭"})
        ),
        dispatcher=dispatcher,
    )

    start = threading.Barrier(2)

    def retry():
        start.wait(timeout=5)
        return retry_client.post(
            "/v1/chat/messages",
            json={"conversation_id": "c1", "text": "重新记"},
            headers=_auth(token_ring, key=str(uuid.uuid4())),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: retry(), range(2)))

    assert all(response.status_code < 500 for response in responses)
    bodies = [response.json() for response in responses]
    assert sum(body.get("record_id") == "rec-concurrent" for body in bodies) == 1
    assert sum(
        body.get("failure_reason") == "BOOKKEEPING_TOOL_REQUIRED"
        for body in bodies
    ) == 1
    with session_factory(engine)() as session:
        consumers = (
            session.query(Operation)
            .filter(
                Operation.retry_of_operation_id == failed.json()["operation_id"]
            )
            .all()
        )
        assert len(consumers) == 1


def test_a_later_finance_success_blocks_retrying_an_older_failure(
    engine, token_ring, keyring
) -> None:
    class SequencedInterpreter:
        def __init__(self):
            self.results = [
                InterpreterError("model down"),
                ToolCall("finance.log_expense", {"name": "午饭"}),
                DirectAnswer("不能重放旧失败"),
            ]

        def interpret(self, *, envelope):
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=SequencedInterpreter(),
        dispatcher=FakeDispatcher(
            resolve=Resolved(intent), commit=Written("recMANUALRETRY")
        ),
    )
    failed = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "示例午餐 20 元"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    succeeded = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "示例午餐 20 元"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert failed.json()["state"] == "failed_safe"
    assert succeeded.json()["record_id"] == "recMANUALRETRY"

    blocked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "重新记"},
        headers=_auth(token_ring, key=REQUEST_ID_3),
    )
    assert blocked.json()["state"] == "failed_safe"
    with session_factory(engine)() as session:
        operation = session.get(Operation, blocked.json()["operation_id"])
        assert operation is not None
        assert operation.retry_of_operation_id is None


def test_two_answers_to_one_clarification_return_a_stable_client_error(
    engine, token_ring, keyring, monkeypatch
) -> None:
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(Clarification("个人还是家庭支出？")),
        dispatcher=FakeDispatcher(),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert parked.status_code == 202

    def lose_source_cas(*args, **kwargs):
        raise StaleOperationVersionError("another answer won")

    monkeypatch.setattr(
        "personal_agent.api.app.transition_operation",
        lose_source_cas,
    )
    losing = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c1",
            "text": "个人",
            "clarification_of": parked.json()["operation_id"],
        },
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )

    assert losing.status_code == 400
    assert losing.json()["error"]["code"] == "INVALID_ARGUMENT"
    with session_factory(engine)() as session:
        assert session.query(Operation).count() == 1


def test_a_clarification_naming_an_unknown_timeline_is_refused(
    engine, token_ring, keyring
) -> None:
    # Under `CAP-001` there is one canonical Timeline, so "another
    # conversation" is now simply an id that resolves to nothing: a stable
    # `TIMELINE_MISMATCH`, and never a second Timeline created on the way past.
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(Clarification("个人还是家庭？")),
        dispatcher=FakeDispatcher(),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    response = client.post(
        "/v1/chat/messages",
        json={
            "conversation_id": "c2",
            "text": "个人",
            "clarification_of": parked.json()["operation_id"],
        },
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TIMELINE_MISMATCH"
    with session_factory(engine)() as session:
        assert session.query(Conversation).count() == 1


def test_slow_model_returns_202_and_finishes_in_the_worker(
    engine, token_ring, keyring, tmp_path
) -> None:
    interpreting = threading.Event()
    release_model = threading.Event()
    model_finished = threading.Event()

    class SlowInterpreter:
        def interpret(self, *, envelope):
            interpreting.set()
            assert release_model.wait(10), "HTTP response waited for the blocked model"
            model_finished.set()
            return DirectAnswer("完成")

    recorder = TranscriptRecorder(
        tmp_path / "transcripts", service="api", now=lambda: NOW
    )
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=SlowInterpreter(),
        dispatcher=FakeDispatcher(),
        sync_wait_seconds=0.01,
        recorder=recorder,
    )
    with client:
        try:
            response = client.post(
                "/v1/chat/messages",
                json={"conversation_id": "c1", "text": "hi"},
                headers=_auth(token_ring),
            )
            assert response.status_code == 202
            assert interpreting.wait(5)
            # Prove detachment from model completion, independent of machine load.
            assert not model_finished.is_set()
        finally:
            release_model.set()

        operation_id = response.json()["operation_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            polled = client.get(
                f"/v1/operations/{operation_id}",
                headers={"Authorization": f"Bearer {_token(token_ring)}"},
            )
            if polled.json()["state"] == "succeeded":
                break
            time.sleep(0.01)
        assert polled.status_code == 200
        assert polled.json()["answer"] == "完成"

    responses = [
        json.loads(line)
        for path in recorder.directory.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "api_response"
    ]
    assert any(
        item["payload"]["delivery"] == "chat_detached"
        and item["payload"]["status_code"] == 202
        for item in responses
    )
    assert any(
        item["payload"]["delivery"] == "operation_poll"
        and item["payload"]["body"]["state"] == "succeeded"
        for item in responses
    )
    assert not any(
        item["payload"]["delivery"] == "chat_sync" for item in responses
    )


# --- poll, cancel, capabilities, events --------------------------------------


def test_capabilities_lists_the_device_tools(engine, token_ring, keyring) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    resp = client.get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert resp.status_code == 200
    assert resp.json()["tools"] == [{"alias": "finance.log_expense"}]


def test_capabilities_names_the_ledger_url_only_when_composed(
    engine, token_ring, keyring
) -> None:
    """`DEV-031`: the client never invents the ledger URL.

    A composition that knows it returns it; one that does not omits the field
    entirely, so the app can tell "the service named no ledger" apart from a
    placeholder and hide the jump rather than open an invented address.
    """
    with_url = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
        ledger_url="https://example.feishu.cn/base/APP_TOKEN",
    )
    resp = with_url.get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert resp.status_code == 200
    assert resp.json()["ledger_url"] == "https://example.feishu.cn/base/APP_TOKEN"

    without = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    resp = without.get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert resp.status_code == 200
    assert "ledger_url" not in resp.json()


def test_an_active_device_cannot_poll_or_cancel_another_devices_operation(
    engine, token_ring, keyring
) -> None:
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    created = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers=_auth(token_ring),
    )
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id="dev-2",
                display_name="Second iPhone",
                public_key="K2",
                device_key_thumbprint="THUMB2",
                status="active",
                scopes='["finance.write"]',
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.commit()
    other_auth = {
        "Authorization": (
            "Bearer "
            + _token(
                token_ring,
                device_id="dev-2",
                thumbprint="THUMB2",
            )
        )
    }
    operation_id = created.json()["operation_id"]
    assert client.get(
        f"/v1/operations/{operation_id}", headers=other_auth
    ).status_code == 400
    assert client.delete(
        f"/v1/operations/{operation_id}", headers=other_auth
    ).status_code == 400


def test_events_list_the_conversation_timeline(engine, token_ring, keyring) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("你好")),
        dispatcher=FakeDispatcher(),
    )
    client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers=_auth(token_ring),
    )
    resp = client.get(
        "/v1/conversations/c1/events",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert resp.status_code == 200
    kinds = [e["event_type"] for e in resp.json()["events"]]
    assert kinds == ["user_message", "operation_result"]


def test_a_query_receipt_and_its_timeline_event_carry_the_same_facts(
    engine, token_ring, keyring
) -> None:
    """The immediate projection and the reloaded event must agree exactly."""
    projection = decode_finance_query_projection(_query_total_result())
    dispatcher = FakeDispatcher(
        resolve=ReadCompleted(
            result=canonical_projection_json(projection),
            projection=projection,
            answer=summarise_query_projection(projection),
        )
    )
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(
            ToolCall("finance.query_expenses", {"view": "total"})
        ),
        dispatcher=dispatcher,
    )
    response = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "查一下我今年打网球花了多少钱"},
        headers=_auth(token_ring),
    )
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["state"] == "succeeded"
    assert receipt["tool"] == "finance.query_expenses"
    assert receipt["query_result"]["view"] == "total"
    assert receipt["answer"] == "共 3 条记录，个人支出合计 ¥1200.00"
    # A raw JSON dump must never appear as the answer.
    assert not receipt["answer"].startswith("{")

    timeline = client.get(
        "/v1/conversations/c1/events",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    result_events = [
        event
        for event in timeline.json()["events"]
        if event["event_type"] == "operation_result"
    ]
    assert len(result_events) == 1
    content = result_events[0]["content"]
    # The Timeline event carries the same tool and the same projection, so a
    # history reload renders the identical card.
    assert content["tool"] == receipt["tool"]
    assert content["query_result"] == receipt["query_result"]
    assert content["answer"] == receipt["answer"]


# --- the duplicate decision over HTTP ----------------------------------------


def test_duplicate_then_write_anyway_carries_the_override(engine, token_ring, keyring):
    intent = WriteIntent("finance.log_expense", {"name": "午饭", "amount": "45"})
    dispatcher = FakeDispatcher(
        resolve=PossibleDuplicate("dup-1", intent, "午饭 ¥45 餐饮"),
        commit=Written("recDUP"),
    )
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=dispatcher,
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert parked.status_code == 202
    body = parked.json()
    assert body["state"] == "waiting_for_duplicate_decision"
    assert body["duplicate_check_id"] == "dup-1"
    assert body["duplicate_existing"] == "午饭 ¥45 餐饮"
    polled = client.get(
        f"/v1/operations/{body['operation_id']}",
        headers=_auth(token_ring),
    )
    assert polled.status_code == 202
    assert polled.json()["duplicate_existing"] == "午饭 ¥45 餐饮"

    decision = client.post(
        "/v1/duplicate-checks/dup-1/decision",
        json={"decision": "write_anyway"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert decision.status_code == 200
    assert decision.json()["record_id"] == "recDUP"
    # The write past the duplicate carried the Host-bound override.
    assert dispatcher.commit_calls[0]["override"] == "dup-1"
    timeline = client.get(
        "/v1/conversations/c1/events",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    projected = timeline.json()["events"]
    assert [event["event_type"] for event in projected] == [
        "user_message",
        "operation_result",
        "duplicate_decision",
        "operation_result",
    ]
    assert projected[2]["content"] == {
        "decision": "write_anyway",
        "duplicate_check_id": "dup-1",
    }
    assert projected[3]["content"]["state"] == "succeeded"
    assert projected[3]["content"]["record_id"] == "recDUP"


def test_a_write_anyway_override_is_recorded_under_its_own_operation(
    engine, token_ring, keyring, tmp_path
) -> None:
    """The confirmed duplicate write is a real ledger write, and is recorded.

    It reaches `run_operation` from the decision endpoint rather than from the
    chat worker, so it is the one write that can miss the transcript scope
    entirely -- leaving the tool records the wrapped dispatcher still writes
    with no operation to group them under.
    """
    recorder = TranscriptRecorder(
        tmp_path / "transcripts", service="api", now=lambda: NOW
    )
    intent = WriteIntent("finance.log_expense", {"name": "午饭", "amount": "45"})
    # Wrapped exactly as `agent_service` wraps every dispatcher it composes.
    dispatcher = RecordingDispatcher(
        FakeDispatcher(
            resolve=PossibleDuplicate("dup-1", intent, "午饭 ¥45 餐饮"),
            commit=Written("recDUP"),
        ),
        recorder,
    )
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=dispatcher,
        recorder=recorder,
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert parked.json()["state"] == "waiting_for_duplicate_decision"

    decision = client.post(
        "/v1/duplicate-checks/dup-1/decision",
        json={"decision": "write_anyway"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert decision.status_code == 200
    override_id = decision.json()["operation_id"]

    records = [
        json.loads(line)
        for path in sorted(recorder.directory.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    # No orphans anywhere in the file: every record belongs to some message.
    assert all(record["turn"] is not None for record in records)
    override = [
        record
        for record in records
        if record["turn"]["operation_id"] == override_id
    ]
    kinds = {record["kind"] for record in override}
    assert {"tool_call", "tool_result", "turn_result"} <= kinds
    (result,) = [
        record for record in override if record["kind"] == "turn_result"
    ]
    assert result["payload"]["state"] == "succeeded"
    assert result["payload"]["result"]["record_id"] == "recDUP"
    # The override joins the source message's turn, so both operations share
    # one Timeline turn while keeping separate operation ids.
    source_id = parked.json()["operation_id"]
    turns = {record["turn"]["turn_id"] for record in records}
    assert len(turns) == 1
    assert {record["turn"]["operation_id"] for record in records} == {
        source_id,
        override_id,
    }


def test_dismiss_decision_replays_and_rejects_a_different_check(
    engine, token_ring, keyring
) -> None:
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(
            ToolCall("finance.log_expense", {"name": "午饭"})
        ),
        dispatcher=FakeDispatcher(
            resolve=PossibleDuplicate("dup-dismiss", intent, "午饭 ¥45"),
        ),
    )
    client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    headers = _auth(token_ring, key=REQUEST_ID_3)
    first = client.post(
        "/v1/duplicate-checks/dup-dismiss/decision",
        json={"decision": "dismiss"},
        headers=headers,
    )
    second = client.post(
        "/v1/duplicate-checks/dup-dismiss/decision",
        json={"decision": "dismiss"},
        headers=headers,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["operation_id"] == second.json()["operation_id"]
    assert second.json()["state"] == "cancelled_pre_submit"
    timeline = client.get(
        "/v1/conversations/c1/events",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    projected = timeline.json()["events"]
    # The HTTP replay is also a Timeline replay: one permanent choice marker
    # and one terminal result, never a second pair.
    assert [event["event_type"] for event in projected] == [
        "user_message",
        "operation_result",
        "duplicate_decision",
        "operation_result",
    ]
    assert projected[2]["content"] == {
        "decision": "dismiss",
        "duplicate_check_id": "dup-dismiss",
    }
    assert projected[3]["content"]["state"] == "cancelled_pre_submit"

    conflict = client.post(
        "/v1/duplicate-checks/another-check/decision",
        json={"decision": "dismiss"},
        headers=headers,
    )
    assert conflict.status_code == 409


def test_cancelling_a_parked_duplicate_is_a_clean_cancellation(
    engine, token_ring, keyring
) -> None:
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(
            resolve=PossibleDuplicate("dup-9", intent, "午饭 ¥45"),
        ),
    )
    parked = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    operation_id = parked.json()["operation_id"]
    resp = client.delete(
        f"/v1/operations/{operation_id}",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "cancelled_pre_submit"
    assert resp.json()["cancel_requested"] is True


# --- 2026-08-03: nothing leaves as a bare 500 with a null body ---------------
# Live evidence: docs/evidence/DEV038_线上半_2026-08-03.md §2.1-2.2. An
# unhandled `database is locked` reached the client as HTTP 500 / `null`, so a
# write that had in fact succeeded in Feishu left nothing to poll.


class ExplodingInterpreter:
    """Fails with the injected task-level exception after the operation is anchored."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def interpret(self, *, envelope):
        raise self.error


@pytest.mark.parametrize(
    "worker_error",
    [
        RuntimeError(
            "database is locked at /var/lib/personal-agent-api/agent.sqlite"
        ),
        AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail="an anchored worker raised a stable AppError",
        ),
    ],
    ids=["ordinary-exception", "app-error"],
)
def test_a_worker_failure_still_returns_the_durable_operation_id(
    engine, token_ring, keyring, worker_error
) -> None:
    """The client must never lose the id, because the id is the route to truth.

    The operation is anchored before any model work, so even when the worker
    dies the durable row exists and can be reported. Its state is whatever the
    database says -- this claims nothing about whether anything was written, and
    an operation still in flight correctly comes back in flight for recovery to
    resolve.
    """
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=ExplodingInterpreter(worker_error),
        dispatcher=FakeDispatcher(),
    )

    response = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "咖啡 18 个人支出"},
        headers=_auth(token_ring),
    )

    body = response.json()
    assert body is not None, "a null body is what left the client with nothing"
    operation_id = body.get("operation_id")
    assert operation_id, body
    # And it is genuinely pollable, which is the whole point of returning it.
    polled = client.get(
        f"/v1/operations/{operation_id}",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert polled.status_code in (200, 202)
    assert polled.json()["operation_id"] == operation_id
    # Nothing was claimed about the outcome.
    assert polled.json()["record_id"] is None


def test_an_unexpected_error_never_reaches_the_client_as_a_bare_500(
    engine, token_ring, keyring
) -> None:
    """The catch-all envelope, on a route with no operation to fall back to.

    Also pins the redaction: the exception text names a filesystem path, and the
    envelope is fixed-text, so none of it can travel.
    """
    deps = AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=token_ring,
        keyring=keyring,
        identifier_key=IDENTIFIER_KEY,
        cursor_key=CURSOR_KEY,
        build_interpreter=lambda auth: FakeInterpreter(DirectAnswer("hi")),
        build_envelope=envelope_factory(keyring),
        build_dispatcher=lambda auth, trace_id: FakeDispatcher(),
        build_authorizer=lambda auth: (lambda *, tool, model_args: dict(model_args)),
        capabilities=_exploding_capabilities,
        now=lambda: NOW,
    )
    client = TestClient(build_app(deps), raise_server_exceptions=False)

    response = client.get(
        "/v1/capabilities",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )

    body = response.json()
    assert body is not None
    assert body["error"]["code"] == "INTERNAL_ERROR"
    serialised = json.dumps(body, ensure_ascii=False)
    assert "/var/lib" not in serialised
    assert "agent.sqlite" not in serialised


def _exploding_capabilities(auth):
    raise RuntimeError("secret path /var/lib/personal-agent-api/agent.sqlite")


# --- DEV-040 §13.2 option B: closing the loop on needs_manual_review ----------
#
# `needs_manual_review` is where an operation lands when the system cannot
# establish what happened. Option A made the *automatic* half self-heal, so what
# reaches a human now is the residue: Finance genuinely has no execution, and the
# only remaining fact source is the ledger. Until this endpoint existed there was
# nothing a person could do with that card -- the push run on 2026-08-04 found
# the dead end, and the breakpoint drill stranded four rows in it.


def _stranded_operation(client, token_ring, key=REQUEST_ID_1) -> str:
    """Drive one chat message to a terminal `needs_manual_review`."""
    response = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=key),
    )
    body = response.json()
    return body["operation_id"]


def _manual_review_client(engine, token_ring, keyring):
    """A write whose verified record id never arrives: terminal manual review."""
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    return _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        # An empty verified record id is the server's own "succeeded without
        # evidence" path, which resolves to terminal needs_manual_review.
        dispatcher=FakeDispatcher(resolve=Resolved(intent), commit=Written("   ")),
    )


def test_a_person_can_close_a_stranded_manual_review(
    engine, token_ring, keyring
) -> None:
    client = _manual_review_client(engine, token_ring, keyring)
    operation_id = _stranded_operation(client, token_ring)

    resolved = client.post(
        f"/v1/operations/{operation_id}/resolution",
        json={"resolution": "confirmed_not_written"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["manual_resolution"] == "confirmed_not_written"
    assert body["recorded"] is True
    # The accounting outcome is untouched. A person reporting what they saw is
    # not evidence that the system verified anything, and the state is the only
    # thing that ever means that.
    assert body["state"] == "needs_manual_review"


def test_resolving_twice_the_same_way_is_a_replay_not_a_conflict(
    engine, token_ring, keyring
) -> None:
    client = _manual_review_client(engine, token_ring, keyring)
    operation_id = _stranded_operation(client, token_ring)
    headers = _auth(token_ring, key=REQUEST_ID_2)
    first = client.post(
        f"/v1/operations/{operation_id}/resolution",
        json={"resolution": "confirmed_written"},
        headers=headers,
    )
    second = client.post(
        f"/v1/operations/{operation_id}/resolution",
        json={"resolution": "confirmed_written"},
        headers=headers,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["recorded"] is True
    assert second.json()["recorded"] is False


def test_a_contradicting_second_reading_is_refused(
    engine, token_ring, keyring
) -> None:
    """Two different answers about one ledger is a discrepancy, not an edit."""
    client = _manual_review_client(engine, token_ring, keyring)
    operation_id = _stranded_operation(client, token_ring)
    client.post(
        f"/v1/operations/{operation_id}/resolution",
        json={"resolution": "confirmed_written"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    conflict = client.post(
        f"/v1/operations/{operation_id}/resolution",
        json={"resolution": "confirmed_not_written"},
        headers=_auth(token_ring, key=REQUEST_ID_3),
    )
    assert conflict.status_code != 200
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_an_operation_that_is_not_under_review_cannot_be_resolved(
    engine, token_ring, keyring
) -> None:
    """A succeeded write must not acquire a human 'verdict' after the fact."""
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=FakeDispatcher(resolve=Resolved(intent), commit=Written("recABC")),
    )
    operation_id = _stranded_operation(client, token_ring)
    refused = client.post(
        f"/v1/operations/{operation_id}/resolution",
        json={"resolution": "confirmed_written"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert refused.status_code != 200
    assert refused.json()["error"]["code"] == "UNSUPPORTED_OPERATION"


def test_an_unknown_resolution_value_is_refused(
    engine, token_ring, keyring
) -> None:
    client = _manual_review_client(engine, token_ring, keyring)
    operation_id = _stranded_operation(client, token_ring)
    refused = client.post(
        f"/v1/operations/{operation_id}/resolution",
        json={"resolution": "probably_fine"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert refused.status_code != 200
    assert refused.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_resolving_an_unknown_operation_is_refused_without_leaking_existence(
    engine, token_ring, keyring
) -> None:
    client = _manual_review_client(engine, token_ring, keyring)
    refused = client.post(
        "/v1/operations/op_does_not_exist/resolution",
        json={"resolution": "confirmed_written"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert refused.status_code != 200
    assert refused.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_resolving_requires_authentication(engine, token_ring, keyring) -> None:
    client = _manual_review_client(engine, token_ring, keyring)
    operation_id = _stranded_operation(client, token_ring)
    refused = client.post(
        f"/v1/operations/{operation_id}/resolution",
        json={"resolution": "confirmed_written"},
    )
    assert refused.status_code == 401


def test_the_resolution_lands_on_the_timeline_exactly_once(
    engine, token_ring, keyring
) -> None:
    client = _manual_review_client(engine, token_ring, keyring)
    operation_id = _stranded_operation(client, token_ring)
    headers = _auth(token_ring, key=REQUEST_ID_2)
    for _ in range(2):
        client.post(
            f"/v1/operations/{operation_id}/resolution",
            json={"resolution": "confirmed_not_written"},
            headers=headers,
        )
    timeline = client.get(
        "/v1/conversations/c1/events",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    markers = [
        event
        for event in timeline.json()["events"]
        if event["event_type"] == "manual_review_resolved"
    ]
    assert len(markers) == 1
    assert markers[0]["content"]["resolution"] == "confirmed_not_written"
    # The domain travels with it, derived from the tool's own IR contract rather
    # than a second list: the client's history line chooses its words by this
    # value, and a marker appended without one can never be corrected.
    assert markers[0]["content"]["domain"] == "finance"


# --- `G1`: the category correction route -------------------------------------


def _corrected(category: str = "购物") -> FinanceExpenseRecord:
    return FinanceExpenseRecord(
        name="午饭",
        amount_cny="38.50",
        occurred_on="2026-07-24",
        is_family_expense=False,
        category=category,
        personal_spend_cny=None,
        category_updated_at="2026-07-24T07:00:00Z",
    )


class _ReceiptThenRefusingInterpreter:
    """Create the source receipt once; any second model call fails the test."""

    def __init__(self) -> None:
        self.calls = 0

    def interpret(self, *, envelope):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("the category correction must never ask a model")
        return ToolCall("finance.log_expense", {"name": "午饭"})


def _client_with_expense_receipt(
    engine,
    token_ring,
    keyring,
    *,
    correction,
    original_category: str | None = "餐饮",
    recorder=None,
):
    """Build the real Timeline owner a receipt-card correction requires."""
    interpreter = _ReceiptThenRefusingInterpreter()
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    original = FinanceExpenseRecord(
        name="午饭",
        amount_cny="38.50",
        occurred_on="2026-07-24",
        is_family_expense=False,
        category=original_category,
        personal_spend_cny="38.50",
    )
    dispatcher = FakeDispatcher(
        resolve=Resolved(intent),
        commit=Written("rec-1", record=original),
    )
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=interpreter,
        dispatcher=dispatcher,
        **({} if recorder is None else {"recorder": recorder}),
    )
    seeded = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 38.50 个人支出"},
        headers=_auth(token_ring, key=REQUEST_ID_4),
    )
    assert seeded.status_code == 200
    assert seeded.json()["record_id"] == "rec-1"
    dispatcher._commit = correction
    dispatcher.commit_calls.clear()
    return client, dispatcher, interpreter


def test_a_category_correction_never_reaches_the_model(
    engine, token_ring, keyring
) -> None:
    """The picker's tap is the decision; there is nothing to interpret.

    The interpreter here raises if it is consulted, which is the point: this
    route resolves its own intent, and the tool it dispatches is
    `model_callable=False` in the IR precisely so no model turn can produce it.
    """
    client, dispatcher, interpreter = _client_with_expense_receipt(
        engine,
        token_ring,
        keyring,
        correction=Written("rec-1", record=_corrected()),
    )

    reply = client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物", "expected_current_category": "餐饮"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )

    assert reply.status_code == 200
    body = reply.json()
    assert body["state"] == "succeeded"
    assert body["record_id"] == "rec-1"
    assert body["record"]["category"] == "购物"
    assert body["record"]["category_updated_at"] == "2026-07-24T07:00:00Z"
    # Dispatched exactly the correction, under the client's own key.
    assert len(dispatcher.commit_calls) == 1
    call = dispatcher.commit_calls[0]
    assert call["idempotency_key"] == REQUEST_ID_1
    assert call["override"] is None
    assert call["intent"].tool == "finance.update_expense_category"
    assert call["intent"].model_args == {
        "record_id": "rec-1",
        "category": "购物",
        "expected_current_category": "餐饮",
    }
    assert interpreter.calls == 1
    timeline = client.get(
        "/v1/conversations/c1/events",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    ).json()["events"]
    marker = [
        event
        for event in timeline
        if event["event_type"] == "expense_category_corrected"
    ]
    assert len(marker) == 1
    assert marker[0]["operation_id"] == body["operation_id"]
    assert marker[0]["content"]["record_id"] == "rec-1"
    assert marker[0]["content"]["record"]["category"] == "购物"


def test_a_category_correction_requires_an_anchored_expense_receipt(
    engine, token_ring, keyring
) -> None:
    """A guessed ledger id cannot create an unowned, unreplayable correction."""
    interpreter = _ReceiptThenRefusingInterpreter()
    dispatcher = FakeDispatcher(commit=Written("rec-1", record=_corrected()))
    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=interpreter,
        dispatcher=dispatcher,
    )

    reply = client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物", "expected_current_category": "餐饮"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )

    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert dispatcher.commit_calls == []
    assert interpreter.calls == 0


def test_polling_a_correction_records_its_transcript_without_an_anchor(
    engine, token_ring, keyring, tmp_path: Path
) -> None:
    """A card action has no user message, and that is not a wiring error.

    Specifically a **failed** one. A correction that succeeds writes its
    `expense_category_corrected` marker, and that marker is itself an anchoring
    event — which is why this went unnoticed until the 2026-08-16 acceptance
    run, where Feishu refused the update and the marker was therefore never
    written. Every poll of that operation then raised inside the transcript
    recorder and dropped the record. Nothing broke — the recorder never changes
    API behaviour — but a transcript that logs a traceback instead of the
    response is the opposite of a transcript, and the failing path is exactly
    the one whose transcript is worth having.
    """
    recorder = TranscriptRecorder(
        tmp_path / "transcripts", service="api", now=lambda: NOW
    )
    client, _dispatcher, _interpreter = _client_with_expense_receipt(
        engine,
        token_ring,
        keyring,
        correction=CommitFailedSafe("SOURCE_UNAVAILABLE"),
        recorder=recorder,
    )
    created = client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物", "expected_current_category": "餐饮"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    assert created.status_code == 200
    operation_id = created.json()["operation_id"]

    polled = client.get(
        f"/v1/operations/{operation_id}", headers=_auth(token_ring)
    )

    assert polled.status_code == 200
    # The response was recorded, under an identity that names the operation
    # even though it names no turn.
    records = [
        json.loads(line)
        for path in sorted(recorder.directory.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    responses = [
        record
        for record in records
        if record.get("kind") == transcript.API_RESPONSE
        and record.get("turn", {}).get("operation_id") == operation_id
    ]
    assert responses, "the poll's transcript was dropped"
    # Identified by the operation, anchored to no turn — which is the honest
    # shape for an action that was never a conversation turn.
    assert responses[-1]["turn"]["turn_id"] is None
    assert responses[-1]["turn"]["conversation_id"] is None
    assert responses[-1]["turn"]["device_id"]


def test_a_lost_correction_marker_never_fails_a_completed_write(
    engine, token_ring, keyring, monkeypatch
) -> None:
    """The marker is presentation; the ledger row is the fact.

    Raising here would report a governed write that already happened as a 500
    and invite a retry for it.
    """
    client, dispatcher, _interpreter = _client_with_expense_receipt(
        engine,
        token_ring,
        keyring,
        correction=Written("rec-1", record=_corrected()),
    )
    import personal_agent.api.app as app_module

    def explode(*args, **kwargs):
        raise RuntimeError("timeline append failed")

    monkeypatch.setattr(
        app_module, "_append_expense_category_corrected", explode
    )

    reply = client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物", "expected_current_category": "餐饮"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )

    assert reply.status_code == 200
    assert reply.json()["state"] == "succeeded"
    assert reply.json()["record"]["category"] == "购物"
    assert len(dispatcher.commit_calls) == 1


def test_a_correction_without_its_expectation_is_refused(
    engine, token_ring, keyring
) -> None:
    """Omitting the compare-and-swap is not a request for a blind overwrite.

    It is an out-of-date client, and reading the omission as "expect nothing"
    would silently turn every stale card into an overwrite of someone else's
    edit.
    """
    client, dispatcher, _ = _client_with_expense_receipt(
        engine,
        token_ring,
        keyring,
        correction=Written("rec-1"),
        original_category=None,
    )

    reply = client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )

    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert dispatcher.commit_calls == []


def test_a_null_expectation_is_a_value_not_an_omission(
    engine, token_ring, keyring
) -> None:
    """A refund legitimately has no category, and saying so must be possible."""
    client, dispatcher, _ = _client_with_expense_receipt(
        engine,
        token_ring,
        keyring,
        correction=Written("rec-1", record=_corrected()),
        original_category=None,
    )

    reply = client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物", "expected_current_category": None},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )

    assert reply.status_code == 200
    assert (
        dispatcher.commit_calls[0]["intent"].model_args[
            "expected_current_category"
        ]
        is None
    )


def test_replaying_a_correction_key_dispatches_once(
    engine, token_ring, keyring
) -> None:
    client, dispatcher, _ = _client_with_expense_receipt(
        engine,
        token_ring,
        keyring,
        correction=Written("rec-1", record=_corrected()),
    )
    body = {"category": "购物", "expected_current_category": "餐饮"}

    first = client.post(
        "/v1/expense-records/rec-1/category",
        json=body,
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    second = client.post(
        "/v1/expense-records/rec-1/category",
        json=body,
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )

    assert first.json()["operation_id"] == second.json()["operation_id"]
    assert len(dispatcher.commit_calls) == 1
    timeline = client.get(
        "/v1/conversations/c1/events",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    ).json()["events"]
    assert sum(
        event["event_type"] == "expense_category_corrected"
        for event in timeline
    ) == 1


def test_the_same_key_under_a_different_correction_is_a_conflict(
    engine, token_ring, keyring
) -> None:
    """Two corrections of the same row from different believed starting points
    are different requests: one is working from a stale view, and sharing a key
    would let the stale one replay as the fresh one's success."""
    client, dispatcher, _ = _client_with_expense_receipt(
        engine,
        token_ring,
        keyring,
        correction=Written("rec-1", record=_corrected()),
    )

    client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物", "expected_current_category": "餐饮"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )
    clash = client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物", "expected_current_category": "旅行"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )

    assert clash.status_code == 409
    assert len(dispatcher.commit_calls) == 1


def test_a_failed_correction_carries_no_business_fields(
    engine, token_ring, keyring
) -> None:
    """A correction that did not reach the ledger must not repaint the card.

    `record` travels only with a proven write, so a safe failure leaves the
    client with nothing to overlay and the row keeps the ledger's value.
    """
    client, dispatcher, _ = _client_with_expense_receipt(
        engine,
        token_ring,
        keyring,
        correction=CommitFailedSafe("SCOPE_DENIED"),
    )

    reply = client.post(
        "/v1/expense-records/rec-1/category",
        json={"category": "购物", "expected_current_category": "餐饮"},
        headers=_auth(token_ring, key=REQUEST_ID_1),
    )

    body = reply.json()
    assert body["state"] == "failed_safe"
    assert body["record_id"] is None
    assert "record" not in body
    timeline = client.get(
        "/v1/conversations/c1/events",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    ).json()["events"]
    assert not any(
        event["event_type"] == "expense_category_corrected"
        for event in timeline
    )


# --- GET /v1/operations/by-key/{idempotency_key} -------------------------------
#
# The chat POST can hold the client for up to 30 seconds before handing back the
# operation id, so the progress trail polls by the idempotency key it already
# holds. Everything here is read-only: the endpoint projects the same operation
# the by-id poll projects, and an unanchored key is a distinguishable 400, never
# a 404 that could be read as "the key is free".


def test_by_key_poll_returns_the_same_projection_as_by_id(
    engine, token_ring, keyring
) -> None:
    class SlowInterpreter:
        def interpret(self, *, envelope):
            time.sleep(0.05)
            return DirectAnswer("你好")

    client = _client(
        engine, token_ring, keyring,
        interpreter=SlowInterpreter(),
        dispatcher=FakeDispatcher(),
        sync_wait_seconds=0.01,
    )
    key = REQUEST_ID_3
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers=_auth(token_ring, key=key),
    )
    assert resp.status_code == 202

    polled = client.get(
        f"/v1/operations/by-key/{key}",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert polled.status_code == 200
    by_key = polled.json()
    operation_id = by_key["operation_id"]

    by_id = client.get(
        f"/v1/operations/{operation_id}",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert by_id.status_code == 200
    assert by_id.json() == by_key


def test_by_key_poll_before_anchor_is_operation_not_anchored(
    engine, token_ring, keyring
) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    never_sent = REQUEST_ID_2
    resp = client.get(
        f"/v1/operations/by-key/{never_sent}",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "OPERATION_NOT_ANCHORED"


def test_by_key_poll_of_another_devices_key_is_refused(
    engine, token_ring, keyring
) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    key = REQUEST_ID_3
    created = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "hi"},
        headers=_auth(token_ring, key=key),
    )
    assert created.status_code == 200

    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id="dev-2",
                display_name="Second iPhone",
                public_key="K2",
                device_key_thumbprint="THUMB2",
                status="active",
                scopes='["finance.write"]',
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.commit()
    other_auth = {
        "Authorization": (
            "Bearer "
            + _token(token_ring, device_id="dev-2", thumbprint="THUMB2")
        )
    }
    resp = client.get(
        f"/v1/operations/by-key/{key}", headers=other_auth
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "OPERATION_NOT_ANCHORED"


def test_by_key_poll_requires_a_canonical_uuid_key(
    engine, token_ring, keyring
) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    for bad in ("not-a-uuid", REQUEST_ID_2.upper() + "-x", REQUEST_ID_2.upper()):
        resp = client.get(
            f"/v1/operations/by-key/{bad}",
            headers={"Authorization": f"Bearer {_token(token_ring)}"},
        )
        assert resp.status_code == 400


def test_by_key_poll_requires_authentication(engine, token_ring, keyring) -> None:
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(DirectAnswer("hi")),
        dispatcher=FakeDispatcher(),
    )
    resp = client.get(f"/v1/operations/by-key/{REQUEST_ID_2}")
    assert resp.status_code == 401


def test_by_key_poll_projection_carries_the_tool_fact(
    engine, token_ring, keyring
) -> None:
    """The trail reads `tool` straight from the dispatching transition."""
    intent = WriteIntent("finance.log_expense", {"name": "午饭"})
    dispatcher = FakeDispatcher(resolve=Resolved(intent), commit=Written("recABC"))
    client = _client(
        engine, token_ring, keyring,
        interpreter=FakeInterpreter(ToolCall("finance.log_expense", {"name": "午饭"})),
        dispatcher=dispatcher,
    )
    key = REQUEST_ID_3
    resp = client.post(
        "/v1/chat/messages",
        json={"conversation_id": "c1", "text": "午饭 45"},
        headers=_auth(token_ring, key=key),
    )
    assert resp.status_code == 200

    polled = client.get(
        f"/v1/operations/by-key/{key}",
        headers={"Authorization": f"Bearer {_token(token_ring)}"},
    )
    assert polled.status_code == 200
    body = polled.json()
    assert body["tool"] == "finance.log_expense"
    assert body["record_id"] == "recABC"
