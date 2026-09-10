"""The device-action settlement surface, end to end over HTTP.

`calendar.create_event` is issued to the phone in the chat response and the
operation parks at `source_in_progress`. Two things settle it:

- **`POST /v1/device-actions/{action_id}/result`** — the phone's report. The
  failure shapes pinned here are the ones the device boundary opens up: a
  forged report from another device must be refused even though the caller
  holds a *valid token*; a settled operation replays as its current projection,
  never a second transition (the CAS is the one-shot guarantee); `created` and
  `duplicate` are both success evidence (the event exists); `denied` and
  `failed` are the device's own zero-write testimony (it is the fact source);
  and two concurrent reports race a CAS that exactly one wins.
- **`POST /v1/calendar/sync`** — the mirror upload. The batch crosses into the
  governed MCP bridge with a signed Host Context naming the *calling* device,
  and one malformed event refuses the whole batch rather than being silently
  triaged (§5.1).
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from cap001_fixtures import CURSOR_KEY, IDENTIFIER_KEY
from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.api.finance_dispatcher import (
    DispatcherContext,
    McpFinanceDispatcher,
)
from personal_agent.auth.tokens import (
    SigningKey,
    TokenKeyRing,
    issue_access_token,
)
from personal_agent.policy.bridge import DeviceAuthorization
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from envelope_factory import envelope_factory
from personal_agent.storage.models import (
    Device,
    Operation,
)
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError, ErrorCode


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
ACTION_KEY = "11111111-1111-4111-8111-111111111111"
OTHER_KEY = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "EK-1"

CAL_ARGS = {
    "title": "网球",
    "start": "2026-09-12T15:00:00+08:00",
    "end": "2026-09-12T16:30:00+08:00",
    "all_day": False,
}


def _device(device_id: str = "device-1", scopes: str = '["calendar.event.write"]') -> Device:
    return Device(
        device_id=device_id,
        display_name="iPhone",
        public_key="K",
        device_key_thumbprint="THUMB",
        status="active",
        scopes=scopes,
        allowed_tools_version="v1",
        created_at=NOW,
    )


@pytest.fixture()
def token_ring() -> TokenKeyRing:
    private = ec.generate_private_key(ec.SECP256R1())
    return TokenKeyRing(active=SigningKey("tok-2026", private, private.public_key()))


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
        session.add(_device())
        session.commit()
    yield engine
    engine.dispose()


def _token(
    token_ring: TokenKeyRing,
    *,
    device_id: str = "device-1",
) -> str:
    return issue_access_token(
        token_ring,
        device_id=device_id,
        device_key_thumbprint="THUMB",
        scopes=["calendar.event.write", "calendar.event.read"],
        allowed_tools_version="v1",
        now=NOW,
    )


def _client(engine, token_ring, keyring, *, sync_ingest=None) -> TestClient:
    def build_dispatcher(auth, trace_id):
        return _fake_dispatcher()

    deps = AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=token_ring,
        keyring=keyring,
        identifier_key=IDENTIFIER_KEY,
        cursor_key=CURSOR_KEY,
        build_interpreter=lambda auth: None,
        build_envelope=envelope_factory(keyring),
        build_dispatcher=build_dispatcher,
        build_authorizer=lambda auth: (lambda *, tool, model_args: dict(model_args)),
        capabilities=lambda auth: [],
        now=lambda: NOW,
        sync_wait_seconds=30.0,
        # The sync route drives the real governed bridge through this seam; the
        # endpoint tests here inject a fake that records the signed context.
        sync_ingest=sync_ingest or (lambda auth, body: _sync_ingest_stub(body)),
    )
    return TestClient(build_app(deps))


class _FakeDispatcher:
    def resolve(self, *, tool, model_args, idempotency_key=None):
        raise AssertionError("not used in these tests")

    def commit(self, *, intent, idempotency_key, duplicate_override):
        raise AssertionError("not used in these tests")


def _seed_source_in_progress(engine) -> str:
    """One device-action operation parked where the phone's report finds it."""
    from personal_agent.api.operation_store import transition_operation

    with session_factory(engine)() as session:
        from personal_agent.storage.models import ApiRequest

        session.add(
            ApiRequest(
                request_id="req-1",
                device_id="device-1",
                client_request_id=ACTION_KEY,
                request_fingerprint="fp",
                received_at=NOW,
            )
        )
        session.flush()
        operation = Operation(
            operation_id="op_1",
            request_id="req-1",
            trace_id="trace-1",
            idempotency_key=ACTION_KEY,
            state="dispatching",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(operation)
        session.commit()
        session.refresh(operation)
        transition_operation(
            session,
            operation_id=operation.operation_id,
            current_state=operation.state,
            current_version=operation.state_version,
            target_state="source_in_progress",
            tool="calendar.create_event",
            now=NOW,
        )
        session.commit()
        return operation.operation_id


def _result(engine, operation_id: str) -> Operation:
    with session_factory(engine)() as session:
        operation = session.get(Operation, operation_id)
        session.refresh(operation)
        return operation


def _auth_headers(token_ring, key=ACTION_KEY) -> dict:
    return {
        "Authorization": f"Bearer {_token(token_ring)}",
        "Idempotency-Key": key,
        "Content-Type": "application/json",
    }


#: The one Session a message's turn is written into.
CONTEXT_SESSION = "ses-1"


def _timeline(engine) -> str:
    """The canonical Timeline and its one open Session, as a chat turn makes them.

    Created through `canonical_timeline_id` rather than by hand, so the row is
    the one the app itself would have resolved -- a hand-built stand-in could
    differ in exactly the way that makes an append land somewhere else.
    """
    from personal_agent.api import events
    from personal_agent.storage.models import ContextSession

    with session_factory(engine)() as session:
        conversation_id = events.canonical_timeline_id(session, now=NOW)
        session.add(
            ContextSession(
                session_id=CONTEXT_SESSION,
                conversation_id=conversation_id,
                status="open",
                relation_kind="new_topic",
                opened_at=NOW,
            )
        )
        session.commit()
        return conversation_id


def _seed_two_item_plan(engine, keyring) -> tuple[str, str, str]:
    """A frozen two-item plan in the state the phone's second report finds.

    Built out of the same two primitives the orchestrator uses (`open_operation`
    then `join_action_plan`, `open_plan_item`), so item 0 really is the message's
    own operation and item 1 really is a sibling -- not two rows hand-written to
    look like a plan. Item 0's result event is the one `_run_chat_turn` appends
    when it issues the list: the sibling is the item that has none, which is the
    defect this section pins.

    Returns `(head_operation_id, sibling_action_id, turn_id)`.
    """
    from personal_agent.api import events
    from personal_agent.api.operation_store import (
        join_action_plan,
        open_operation,
        open_plan_item,
        transition_operation,
    )

    conversation_id = _timeline(engine)
    turn_id = events.new_turn_id()
    with session_factory(engine)() as session:
        head = open_operation(
            session,
            device_id="device-1",
            client_request_id=ACTION_KEY,
            request_fingerprint="fp-head",
            now=NOW,
        ).operation
        session.flush()
        join_action_plan(
            session, operation_id=head.operation_id, plan_key=ACTION_KEY, now=NOW
        )
        sibling = open_plan_item(
            session,
            device_id="device-1",
            plan_key=ACTION_KEY,
            plan_index=1,
            request_fingerprint="fp-sibling",
            now=NOW,
        ).operation
        session.flush()
        # Item 0 is born `accepted` (it is the user's message) and item 1 is
        # born `dispatching` (its call was already decided when the plan was
        # frozen), so the two reach the parked state by different routes -- the
        # real ones.
        for row, path in ((head, ("interpreting", "dispatching")), (sibling, ())):
            for state in path:
                transition_operation(
                    session,
                    operation_id=row.operation_id,
                    current_state=row.state,
                    current_version=row.state_version,
                    target_state=state,
                    now=NOW,
                )
                session.refresh(row)
            transition_operation(
                session,
                operation_id=row.operation_id,
                current_state=row.state,
                current_version=row.state_version,
                target_state="source_in_progress",
                tool="calendar.create_event",
                now=NOW,
            )
            session.refresh(row)
        events.append_event(
            session,
            keyring,
            conversation_id=conversation_id,
            session_id=CONTEXT_SESSION,
            turn_id=turn_id,
            event_type=events.OPERATION_RESULT,
            content={"state": "source_in_progress", "tool": "calendar.create_event"},
            operation_id=head.operation_id,
            now=NOW,
        )
        sibling_action_id = sibling.idempotency_key
        session.commit()
        return head.operation_id, sibling_action_id, turn_id


def _result_events(engine, keyring) -> list:
    """Every `operation_result` on the Timeline, oldest first, decrypted."""
    from personal_agent.api import events

    with session_factory(engine)() as session:
        conversation_id = events.canonical_timeline_id(session, now=NOW)
        return [
            entry
            for entry in events.list_timeline(
                session, keyring, conversation_id=conversation_id
            )
            if entry.event_type == events.OPERATION_RESULT
        ]


_SYNC_CALLS: list[tuple[str, dict]] = []


@pytest.fixture(autouse=True)
def _fresh_sync_calls():
    """The spy list is module-level so the stub can append from any thread;
    each test starts from zero or its assertions would count the last test's
    traffic."""
    _SYNC_CALLS.clear()
    yield
    _SYNC_CALLS.clear()


def _sync_ingest_stub(body: dict) -> dict:
    """The fake governed bridge the sync route composes in endpoint tests."""
    _SYNC_CALLS.append((json.dumps(body, sort_keys=True), dict(body)))
    return {"status": "ok", "upserted": len(body["events"]), "skipped": 0, "marked_deleted": 0}


# --- the device report endpoint -----------------------------------------------


def test_created_report_settles_to_succeeded_with_event_id(
    engine, token_ring, keyring
) -> None:
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created", "event_id": EVENT_ID},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "succeeded"
    # The EventKit identifier is the receipt's record id: the one external
    # proof that the write exists.
    assert body["record_id"] == EVENT_ID
    assert body["tool"] == "calendar.create_event"
    operation = _result(engine, operation_id)
    assert operation.state == "succeeded"
    assert operation.safe_result == EVENT_ID


def test_duplicate_report_is_also_success_evidence(
    engine, token_ring, keyring
) -> None:
    """The device's local dedup may find the event already there. That is a
    *created* calendar from the user's point of view, so the operation settles
    to succeeded — not to a failure that would ask them to try again."""
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "duplicate", "event_id": EVENT_ID},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "succeeded"
    assert response.json()["record_id"] == EVENT_ID
    assert _result(engine, operation_id).safe_result == EVENT_ID


@pytest.mark.parametrize(
    ("reported", "expected_reason"),
    [
        ("denied", "DEVICE_ACTION_DENIED"),
        ("failed", "DEVICE_EXECUTION_FAILED"),
    ],
)
def test_denied_or_failed_report_is_zero_write_testimony_from_the_device(
    engine, token_ring, keyring, reported, expected_reason
) -> None:
    """The phone is the fact source for its own write. A report that EventKit
    refused the save is *the* evidence nothing was written — the strongest
    evidence this domain can hold — so the operation settles failed_safe."""
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": reported, "detail": "access denied by user"},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "failed_safe"
    # The stable code leads the reason; the detail is the device's own words.
    assert body["failure_reason"] == f"{expected_reason}: access denied by user"
    assert body["record_id"] is None
    operation = _result(engine, operation_id)
    assert operation.state == "failed_safe"


def test_a_settled_report_replays_the_current_projection(
    engine, token_ring, keyring
) -> None:
    """The app retries its PATCH on next foreground. A settled operation must
    answer 200 with the state it already has — never a second transition, and
    never an error that would push the app into a crash loop."""
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)
    first = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created", "event_id": EVENT_ID},
        headers=_auth_headers(token_ring),
    )
    assert first.status_code == 200, first.text

    replay = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created", "event_id": "EK-OTHER"},
        headers=_auth_headers(token_ring),
    )

    assert replay.status_code == 200, replay.text
    assert replay.json()["state"] == "succeeded"
    # The first report won; the replay cannot overwrite its evidence.
    assert replay.json()["record_id"] == EVENT_ID
    assert _result(engine, operation_id).safe_result == EVENT_ID


def test_a_report_from_another_device_is_refused(
    engine, token_ring, keyring
) -> None:
    """A *valid token for device-2* must not settle device-1's action. The
    ownership rule is the same one every operation endpoint applies: the
    operation must belong to an api_request the caller's own device made."""
    operation_id = _seed_source_in_progress(engine)
    with session_factory(engine)() as session:
        session.add(
            _device(device_id="device-2", scopes='["calendar.event.write"]')
        )
        session.commit()
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created", "event_id": "EK-FORGED"},
        headers={
            "Authorization": f"Bearer {_token(token_ring, device_id='device-2')}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    # Nothing moved: the forged report neither settled nor fabricated a write.
    assert _result(engine, operation_id).state == "source_in_progress"


def test_a_created_report_without_an_event_id_is_refused(
    engine, token_ring, keyring
) -> None:
    """`event_id` is the external proof a `created`/`duplicate` report claims.
    A report of success that names no event proves nothing and must not settle
    the operation to succeeded on the caller's say-so."""
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created"},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 400, response.text
    assert _result(engine, operation_id).state == "source_in_progress"


def test_a_report_for_a_non_device_tool_is_refused(
    engine, token_ring, keyring
) -> None:
    """The report endpoint is the settlement surface *for device-executed
    actions*. A Finance operation parked by its own execution path may not be
    settled by a phone POST claiming a calendar event it never held — the
    ownership check alone does not ask what kind of operation this is. The
    tool must be one the IR marks `executor: "device"`, and the report must
    find the operation in a state that still expects the device's word."""
    from personal_agent.api.operation_store import transition_operation
    from personal_agent.storage.models import ApiRequest

    with session_factory(engine)() as session:
        session.add(
            ApiRequest(
                request_id="req-fin",
                device_id="device-1",
                client_request_id=ACTION_KEY,
                request_fingerprint="fp",
                received_at=NOW,
            )
        )
        session.flush()
        operation = Operation(
            operation_id="op_fin",
            request_id="req-fin",
            trace_id="trace-fin",
            idempotency_key=ACTION_KEY,
            state="dispatching",
            created_at=NOW,
            updated_at=NOW,
            tool="finance.log_expense",
        )
        session.add(operation)
        session.commit()
        session.refresh(operation)
        operation_id = operation.operation_id
        transition_operation(
            session,
            operation_id=operation.operation_id,
            current_state=operation.state,
            current_version=operation.state_version,
            target_state="source_in_progress",
            now=NOW,
        )
        session.commit()

    client = _client(engine, token_ring, keyring)
    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created", "event_id": "FAKE-EVENT"},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 400, response.text
    # The forged report neither settled the operation nor fabricated evidence.
    assert _result(engine, operation_id).state == "source_in_progress"
    assert _result(engine, operation_id).safe_result is None


def test_an_unknown_result_value_is_refused(engine, token_ring, keyring) -> None:
    """The result vocabulary is closed. Everything else — a server crash, a
    device bug, a probe — is silence about a write that may exist, and silence
    is the sweep's question, not this endpoint's answer."""
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "written_successfully_provably"},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 400, response.text
    assert _result(engine, operation_id).state == "source_in_progress"


def test_unexpected_body_fields_are_refused(engine, token_ring, keyring) -> None:
    """Closed body: a field the contract does not name is refused, so a client
    can never be told that an ignored field was meaningful."""
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created", "event_id": EVENT_ID, "event_title": "网球"},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 400, response.text
    assert _result(engine, operation_id).state == "source_in_progress"


def test_two_concurrent_reports_race_and_exactly_one_settles(
    engine, token_ring, keyring
) -> None:
    """A retry racing the first report is the normal case (flaky network on
    foreground). Both may read `source_in_progress`; the CAS lets exactly one
    win, and the loser sees the winner's settled projection."""
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)
    barrier = threading.Barrier(2)
    results: list = []

    def report(event_id: str) -> None:
        barrier.wait()
        results.append(
            client.post(
                f"/v1/device-actions/{ACTION_KEY}/result",
                json={"result": "created", "event_id": event_id},
                headers=_auth_headers(token_ring),
            ).json()
        )

    threads = [
        threading.Thread(target=report, args=("EK-A",)),
        threading.Thread(target=report, args=("EK-B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    states = sorted(r["state"] for r in results)
    assert states == ["succeeded", "succeeded"]
    ids = {r.get("record_id") for r in results}
    # One report's evidence won the CAS; both responses agree on it, because
    # the loser re-projects the winner's state rather than inventing its own.
    assert ids == {EVENT_ID} or len(ids) == 1
    assert _result(engine, operation_id).safe_result in {"EK-A", "EK-B"}


# --- a plan's later items get their own receipt --------------------------------
#
# One message with N actions is one plan (design 4.1) and its items are separate
# operations, each settling through its own report. The design says so in one
# line -- 各 operation 由各自的回报独立结算，**回执各一行** -- and a receipt line
# is a Timeline event. The message's own item had one written by the turn that
# issued it; the siblings had none, so a sibling that settled perfectly left the
# user nothing to look at: no card on the Timeline, no line in history after a
# restart, only the server's own row. These pin that each sibling's settlement
# writes its own line, in the same turn as the message it came from.


def test_a_settled_sibling_appends_its_own_result_event(
    engine, token_ring, keyring
) -> None:
    head_id, sibling_action, turn_id = _seed_two_item_plan(engine, keyring)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{sibling_action}/result",
        json={"result": "created", "event_id": EVENT_ID},
        headers=_auth_headers(token_ring, key=sibling_action),
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "succeeded"
    events = _result_events(engine, keyring)
    assert [event.operation_id for event in events] == [
        head_id,
        response.json()["operation_id"],
    ]
    receipt = events[-1]
    # The sibling's line joins the message's own turn rather than inventing
    # one: it is the same user request, continued.
    assert (receipt.session_id, receipt.turn_id) == (CONTEXT_SESSION, turn_id)
    assert receipt.content["state"] == "succeeded"
    assert receipt.content["record_id"] == EVENT_ID


def test_a_replayed_sibling_report_appends_nothing(
    engine, token_ring, keyring
) -> None:
    """The one-event-per-operation guard. The endpoint's CAS already makes a
    replay settle nothing; this is the same rule applied to the record of it,
    which is what keeps a flaky-network retry from stacking duplicate cards."""
    _, sibling_action, _ = _seed_two_item_plan(engine, keyring)
    client = _client(engine, token_ring, keyring)
    for _ in range(2):
        response = client.post(
            f"/v1/device-actions/{sibling_action}/result",
            json={"result": "created", "event_id": EVENT_ID},
            headers=_auth_headers(token_ring, key=sibling_action),
        )
        assert response.status_code == 200, response.text

    assert len(_result_events(engine, keyring)) == 2


def test_a_refused_sibling_also_gets_its_own_line(
    engine, token_ring, keyring
) -> None:
    """A sibling the phone refused is the item the user most needs to see: the
    message said "下周一10点牙医，下午3点理发" and only one of them happened."""
    _, sibling_action, _ = _seed_two_item_plan(engine, keyring)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{sibling_action}/result",
        json={"result": "denied", "detail": "用户拒绝了"},
        headers=_auth_headers(token_ring, key=sibling_action),
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "failed_safe"
    receipt = _result_events(engine, keyring)[-1]
    assert receipt.operation_id == response.json()["operation_id"]
    assert receipt.content["state"] == "failed_safe"


def test_the_messages_own_item_keeps_the_single_line_the_turn_wrote(
    engine, token_ring, keyring
) -> None:
    """Item 0's result event belongs to the turn that issued it. Reporting it
    settles the operation; it must not add a second line saying the same thing,
    or every ordinary single-action message would grow a duplicate card."""
    head_id, _, _ = _seed_two_item_plan(engine, keyring)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created", "event_id": EVENT_ID},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 200, response.text
    assert response.json()["operation_id"] == head_id
    events = _result_events(engine, keyring)
    assert [event.operation_id for event in events] == [head_id]
    # The line still says what the turn wrote; it is not rewritten in place.
    assert events[0].content["state"] == "source_in_progress"


def test_an_operation_with_no_turn_still_gets_no_fabricated_one(
    engine, token_ring, keyring
) -> None:
    """Not every settled device action is a conversation turn. This one was
    opened by a card-driven path, so it has no anchor -- and the honest answer
    is no Timeline line, not a made-up turn id."""
    operation_id = _seed_source_in_progress(engine)
    client = _client(engine, token_ring, keyring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "created", "event_id": EVENT_ID},
        headers=_auth_headers(token_ring),
    )

    assert response.status_code == 200, response.text
    assert response.json()["operation_id"] == operation_id
    assert _result_events(engine, keyring) == []


# --- the timeout sweep wiring ---------------------------------------------------


def test_recovery_scan_sweeps_a_timed_out_device_action(
    engine, token_ring, keyring
) -> None:
    """The sweep is wired into the composition's own recovery scan, not only
    tested beside it: an operation whose report never arrives must be parked
    by the same loop recovery runs on, at `needs_manual_review`."""
    from datetime import timedelta

    from personal_agent.api.composition import recover_at_startup

    operation_id = _seed_source_in_progress(engine)

    class _NoControl:
        async def get_execution(self, idempotency_key):
            return None

    # Sixteen quiet minutes: past the report timeout. The operation is also
    # past the recovery quiet period, but the Finance projection must leave it
    # alone (device executor) -- only the device sweep may act on it.
    recover_at_startup(
        session_factory(engine),
        _NoControl(),
        now=lambda: NOW + timedelta(minutes=16),
    )

    assert _result(engine, operation_id).state == "needs_manual_review"


def test_recovery_scan_does_not_finance_reconcile_a_fresh_device_action(
    engine, token_ring, keyring
) -> None:
    """A device action parked for only a minute is younger than both the
    report timeout and the recovery quiet period. Recovery must leave it
    exactly alone -- a control plane that answers "no execution" says nothing
    about a write Finance knows nothing about."""
    from datetime import timedelta

    from personal_agent.api.composition import recover_at_startup

    operation_id = _seed_source_in_progress(engine)

    class _NoControl:
        async def get_execution(self, idempotency_key):
            return None

    recover_at_startup(
        session_factory(engine),
        _NoControl(),
        now=lambda: NOW + timedelta(minutes=1),
    )

    assert _result(engine, operation_id).state == "source_in_progress"


# --- the calendar sync endpoint -------------------------------------------------


def test_sync_ingests_a_batch_through_the_governed_seam(
    engine, token_ring, keyring
) -> None:
    client = _client(engine, token_ring, keyring)
    batch = {
        "window_start": "2026-08-09T00:00:00+08:00",
        "window_end": "2026-09-07T00:00:00+08:00",
        "window_complete": True,
        "snapshot_as_of": "2026-09-07T07:30:00+00:00",
        "events": [
            {
                "event_identifier": "ev-1",
                "calendar_identifier": "cal-1",
                "title": "网球",
                "start": "2026-09-07T15:00:00+08:00",
                "end": "2026-09-07T16:30:00+08:00",
                "all_day": False,
                "last_modified": "2026-09-06T20:00:00+08:00",
            }
        ],
    }

    response = client.post(
        "/v1/calendar/sync",
        json=batch,
        headers=_auth_headers(token_ring, key=str(uuid.uuid4())),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ok",
        "upserted": 1,
        "skipped": 0,
        "marked_deleted": 0,
    }
    # The batch reached the governed seam — the only path to the mirror.
    assert len(_SYNC_CALLS) == 1


def test_sync_refuses_a_batch_with_one_malformed_event_whole(
    engine, token_ring, keyring
) -> None:
    """§5.1: no silent triage. One malformed event means the device and the
    server disagree about the snapshot's shape; the server must not guess
    which half to keep."""
    client = _client(engine, token_ring, keyring)
    batch = {
        "window_start": "2026-08-09T00:00:00+08:00",
        "window_end": "2026-09-07T00:00:00+08:00",
        "window_complete": True,
        "snapshot_as_of": "2026-09-07T07:30:00+00:00",
        "events": [
            {
                "event_identifier": "ev-good",
                "calendar_identifier": "cal-1",
                "start": "2026-09-07T15:00:00+08:00",
                "end": "2026-09-07T16:30:00+08:00",
                "all_day": False,
                "last_modified": "2026-09-06T20:00:00+08:00",
            },
            {
                "event_identifier": "ev-bad",
                # A missing `all_day` is a shape the device could not have
                # produced if it were healthy.
                "calendar_identifier": "cal-1",
                "start": "2026-09-07T17:00:00+08:00",
                "end": "2026-09-07T18:00:00+08:00",
                "last_modified": "2026-09-06T20:00:00+08:00",
            },
        ],
    }

    response = client.post(
        "/v1/calendar/sync",
        json=batch,
        headers=_auth_headers(token_ring, key=str(uuid.uuid4())),
    )

    assert response.status_code == 400, response.text
    # Nothing was ingested: no half batch may enter the mirror.
    assert _SYNC_CALLS == []


def _bulk_batch(count: int) -> dict:
    """A window of `count` legal events, each carrying a full-length note.

    Legal on every axis the mirror checks: the note is exactly at its 4096
    threshold, and the event count is under the 200-per-batch budget. Only the
    *bytes* are large, which is the point.
    """
    one = {
        "event_identifier": "ev-{}",
        "calendar_identifier": "cal-1",
        "start": "2026-09-07T15:00:00+08:00",
        "end": "2026-09-07T16:30:00+08:00",
        "all_day": False,
        "last_modified": "2026-09-06T20:00:00+08:00",
        "notes": "n" * 4096,
    }
    return {
        "window_start": "2026-08-09T00:00:00+08:00",
        "window_end": "2026-09-07T00:00:00+08:00",
        "window_complete": True,
        "snapshot_as_of": "2026-09-07T07:30:00+00:00",
        "events": [dict(one, event_identifier=f"ev-{i}") for i in range(count)],
    }


def test_sync_accepts_a_batch_above_the_global_body_cap(
    engine, token_ring, keyring
) -> None:
    """The device is *required* to send up to 128 KiB per chunk (design 6).
    The service's global 64 KiB body cap would have refused a batch the phone
    cannot legally shrink -- and refused it before any scope or schema check,
    so the mirror would simply never have filled past ~16 events.
    """
    client = _client(engine, token_ring, keyring)
    batch = _bulk_batch(20)  # ~84 KiB
    encoded = len(json.dumps(batch).encode())
    assert 64 * 1024 < encoded < 128 * 1024, encoded

    response = client.post(
        "/v1/calendar/sync",
        json=batch,
        headers=_auth_headers(token_ring, key=str(uuid.uuid4())),
    )

    assert response.status_code == 200, response.text
    assert response.json()["upserted"] == 20
    assert len(_SYNC_CALLS) == 1


def test_sync_refuses_a_batch_beyond_its_own_hard_cap(
    engine, token_ring, keyring
) -> None:
    """512 KiB is the route's own ceiling, not a device budget: a request past
    it is refused before the seam, so nothing reaches the mirror. The event
    count here is legal, so the byte cap is the only thing refusing it."""
    client = _client(engine, token_ring, keyring)
    batch = _bulk_batch(128)  # ~537 KiB, still under the 200-event budget
    assert len(json.dumps(batch).encode()) > 512 * 1024

    response = client.post(
        "/v1/calendar/sync",
        json=batch,
        headers=_auth_headers(token_ring, key=str(uuid.uuid4())),
    )

    assert response.status_code == 400, response.text
    assert _SYNC_CALLS == []


def test_sync_rejects_a_batch_beyond_the_size_cap(
    engine, token_ring, keyring
) -> None:
    client = _client(engine, token_ring, keyring)
    one = {
        "event_identifier": "ev-1",
        "calendar_identifier": "cal-1",
        "start": "2026-09-07T15:00:00+08:00",
        "end": "2026-09-07T16:30:00+08:00",
        "all_day": False,
        "last_modified": "2026-09-06T20:00:00+08:00",
    }
    batch = {
        "window_start": "2026-08-09T00:00:00+08:00",
        "window_end": "2026-09-07T00:00:00+08:00",
        "window_complete": False,
        "snapshot_as_of": "2026-09-07T07:30:00+00:00",
        "events": [one] * 201,
    }

    response = client.post(
        "/v1/calendar/sync",
        json=batch,
        headers=_auth_headers(token_ring, key=str(uuid.uuid4())),
    )

    assert response.status_code == 400, response.text
    assert _SYNC_CALLS == []


def test_sync_requires_a_scope_the_device_may_not_have(
    engine, token_ring, keyring, tmp_path: Path
) -> None:
    """A device enrolled without `calendar.event.read` gets 403 — the same
    opaque code every other governed refusal on this service uses."""
    with session_factory(engine)() as session:
        device = session.get(Device, "device-1")
        device.scopes = '["calendar.event.write"]'
        session.commit()
    client = _client(engine, token_ring, keyring)
    token = issue_access_token(
        token_ring,
        device_id="device-1",
        device_key_thumbprint="THUMB",
        scopes=["calendar.event.write"],
        allowed_tools_version="v1",
        now=NOW,
    )
    batch = {
        "window_start": "2026-08-09T00:00:00+08:00",
        "window_end": "2026-09-07T00:00:00+08:00",
        "window_complete": True,
        "snapshot_as_of": "2026-09-07T07:30:00+00:00",
        "events": [],
    }

    response = client.post(
        "/v1/calendar/sync",
        json=batch,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "SCOPE_DENIED"
    assert _SYNC_CALLS == []


def test_sync_refuses_a_device_enrolled_for_a_stale_manifest_version(
    engine, token_ring, keyring
) -> None:
    """A device re-enrolled against an old manifest has no tools at all; the
    mirror upload is governed like any other tool and must refuse rather than
    write a mirror for a device the policy would not trust.

    The version gate lives in the bridge's `authorize` (composition-owned),
    so the route-level seam here cannot prove it; that is the production
    composition test's job. This test proves the route does not *bypass* the
    seam: whatever it composes sees the device's real authorization inputs,
    here expressed by wiring a stub that applies the same stale-version
    refusal the bridge would."""
    with session_factory(engine)() as session:
        device = session.get(Device, "device-1")
        device.allowed_tools_version = "old"
        session.commit()
    client = _client(
        engine,
        token_ring,
        keyring,
        sync_ingest=lambda auth, body: (_ for _ in ()).throw(
            AppError(
                ErrorCode.SCOPE_DENIED,
                internal_detail="device carries a stale allowed_tools_version",
            )
        ),
    )
    token = issue_access_token(
        token_ring,
        device_id="device-1",
        device_key_thumbprint="THUMB",
        scopes=["calendar.event.read", "calendar.event.write"],
        allowed_tools_version="old",
        now=NOW,
    )
    batch = {
        "window_start": "2026-08-09T00:00:00+08:00",
        "window_end": "2026-09-07T00:00:00+08:00",
        "window_complete": True,
        "snapshot_as_of": "2026-09-07T07:30:00+00:00",
        "events": [],
    }

    response = client.post(
        "/v1/calendar/sync",
        json=batch,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 403, response.text
    assert _SYNC_CALLS == []
