"""DEV-026 E: the FastAPI Client API contract, end to end over HTTP.

A real Agent SQLite database, a real access-token ring and Agent key ring, and
fakes for the model interpreter and the Finance dispatcher. The FastAPI
`TestClient` drives the actual routes, so idempotency, auth, cancellation and the
duplicate decision are tested as the wire sees them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from personal_agent.api.app import AgentApiDeps, AuthContext, build_app
from personal_agent.api.orchestrator import (
    DirectAnswer,
    PossibleDuplicate,
    Resolved,
    ToolCall,
    Written,
)
from personal_agent.api.intent import WriteIntent
from personal_agent.auth.tokens import (
    SigningKey,
    TokenKeyRing,
    issue_access_token,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Device
from personal_agent_core.crypto import KeyRing, generate_key


NOW = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
REQUEST_ID_1 = "11111111-1111-4111-8111-111111111111"
REQUEST_ID_2 = "22222222-2222-4222-8222-222222222222"
REQUEST_ID_3 = "33333333-3333-4333-8333-333333333333"


class FakeInterpreter:
    def __init__(self, result) -> None:
        self.result = result

    def interpret(self, *, text: str, conversation_id: str):
        return self.result


class FakeDispatcher:
    def __init__(self, *, resolve=None, commit=None) -> None:
        self._resolve = resolve
        self._commit = commit
        self.commit_calls: list[dict] = []

    def resolve(self, *, tool, model_args):
        return self._resolve

    def commit(self, *, intent, idempotency_key, duplicate_override):
        self.commit_calls.append(
            {"idempotency_key": idempotency_key, "override": duplicate_override}
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
        session.commit()
    yield engine
    engine.dispose()


def _token(token_ring: TokenKeyRing) -> str:
    return issue_access_token(
        token_ring,
        device_id="dev-1",
        device_key_thumbprint="THUMB",
        scopes=["finance.write"],
        allowed_tools_version="v1",
        now=NOW,
    )


def _client(engine, token_ring, keyring, *, interpreter, dispatcher) -> TestClient:
    deps = AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=token_ring,
        keyring=keyring,
        interpreter=interpreter,
        dispatcher=dispatcher,
        build_authorizer=lambda auth: (lambda *, tool, model_args: dict(model_args)),
        capabilities=lambda auth: [{"alias": "finance.log_expense"}],
        now=lambda: NOW,
    )
    return TestClient(build_app(deps))


def _auth(token_ring, key=REQUEST_ID_1) -> dict:
    return {"Authorization": f"Bearer {_token(token_ring)}", "Idempotency-Key": key}


# --- auth --------------------------------------------------------------------


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

    decision = client.post(
        "/v1/duplicate-checks/dup-1/decision",
        json={"decision": "write_anyway"},
        headers=_auth(token_ring, key=REQUEST_ID_2),
    )
    assert decision.status_code == 200
    assert decision.json()["record_id"] == "recDUP"
    # The write past the duplicate carried the Host-bound override.
    assert dispatcher.commit_calls[0]["override"] == "dup-1"


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
