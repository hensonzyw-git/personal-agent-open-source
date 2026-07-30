"""DEV-026 E: the FastAPI Client API contract, end to end over HTTP.

A real Agent SQLite database, a real access-token ring and Agent key ring, and
fakes for the model interpreter and the Finance dispatcher. The FastAPI
`TestClient` drives the actual routes, so idempotency, auth, cancellation and the
duplicate decision are tested as the wire sees them.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from cap001_fixtures import CURSOR_KEY, IDENTIFIER_KEY
from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.api.orchestrator import (
    Clarification,
    DirectAnswer,
    PossibleDuplicate,
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
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from envelope_factory import envelope_factory
from personal_agent.storage.models import Conversation, Device, Operation
from personal_agent_core.crypto import KeyRing, generate_key


NOW = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
REQUEST_ID_1 = "11111111-1111-4111-8111-111111111111"
REQUEST_ID_2 = "22222222-2222-4222-8222-222222222222"
REQUEST_ID_3 = "33333333-3333-4333-8333-333333333333"


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
    )
    return TestClient(build_app(deps))


def _auth(token_ring, key=REQUEST_ID_1) -> dict:
    return {"Authorization": f"Bearer {_token(token_ring)}", "Idempotency-Key": key}


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
    engine, token_ring, keyring
) -> None:
    class SlowInterpreter:
        def interpret(self, *, envelope):
            time.sleep(0.1)
            return DirectAnswer("完成")

    client = _client(
        engine,
        token_ring,
        keyring,
        interpreter=SlowInterpreter(),
        dispatcher=FakeDispatcher(),
        sync_wait_seconds=0.01,
    )
    with client:
        started = time.monotonic()
        response = client.post(
            "/v1/chat/messages",
            json={"conversation_id": "c1", "text": "hi"},
            headers=_auth(token_ring),
        )
        assert response.status_code == 202
        assert time.monotonic() - started < 0.08

        operation_id = response.json()["operation_id"]
        for _ in range(30):
            polled = client.get(
                f"/v1/operations/{operation_id}",
                headers={"Authorization": f"Bearer {_token(token_ring)}"},
            )
            if polled.json()["state"] == "succeeded":
                break
            time.sleep(0.01)
        assert polled.status_code == 200
        assert polled.json()["answer"] == "完成"


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
