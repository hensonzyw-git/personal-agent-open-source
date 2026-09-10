"""「仍要创建」: the calendar override endpoint, end to end over HTTP.

The phone reports `duplicate` when its own lookup finds the event already
there, and the receipt offers the user one way forward. That decision reaches
the server through `POST /v1/device-actions/{action_id}/override`, which is the
narrowest endpoint in the API: an empty closed body (there is no decision to
submit -- *which* action is named is the whole request) and one precondition
(a device-executed calendar create that settled `succeeded` on a `duplicate`
report).

What the shapes here pin is idempotency by derivation (design 3.3). The
derived operation's key is `uuid5(namespace, "<operation_id>:calendar-override")`
rather than anything the caller sends, so a double tap, a retry whose response
was lost, and two concurrent taps must all land on the *same* derived
operation and issue *one* action. A second event on the user's calendar is the
failure this endpoint's shape exists to prevent, and it is not detectable
afterwards: two real EKEvents look exactly like two intended ones.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from cap001_fixtures import CURSOR_KEY, IDENTIFIER_KEY
from envelope_factory import envelope_factory
from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.api.calendar_issue import override_key
from personal_agent.api.device_action_projection import seal_device_action
from personal_agent.api.intent import WriteIntent
from personal_agent.api.operation_request import seal_operation_request
from personal_agent.api.operation_store import transition_operation
from personal_agent.api.orchestrator import DeviceActionIssued
from personal_agent.auth.tokens import SigningKey, TokenKeyRing, issue_access_token
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import ApiRequest, Device, Operation
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.host_context import HOST_ONLY_FIELDS
from personal_agent_core.tool_ir import TOOL_CONTRACTS


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
ACTION_KEY = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "EK-1"
CAL_ARGS = {
    "title": "网球",
    "start": "2026-09-12T15:00:00+08:00",
    "end": "2026-09-12T16:30:00+08:00",
    "all_day": False,
    "calendar": "日常安排",
}
#: The version `calendar.create_event` declares. The override's *response* is
#: a projection of a freshly-issued action, so the delivery gate applies to it
#: exactly as it does to the original issue.
CLIENT_WIRE_V2 = 2


def _device(device_id: str = "device-1") -> Device:
    return Device(
        device_id=device_id,
        display_name="iPhone",
        public_key="K",
        device_key_thumbprint="THUMB",
        status="active",
        scopes='["calendar.event.write", "calendar.event.read"]',
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


class RecordingDispatcher:
    """The device fork, recorded: `resolve` issues instead of writing.

    Every call is kept, including `skip_local_dedup`, because "the phone was
    told not to skip its own check" and "the phone was told to skip it" are the
    two outcomes this endpoint's whole distinction rests on -- and against a
    fake that only counted calls they would look identical.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def resolve(self, *, tool, model_args, idempotency_key=None, skip_local_dedup=False):
        self.calls.append(
            {
                "tool": tool,
                "model_args": dict(model_args),
                "key": idempotency_key,
                "skip_local_dedup": skip_local_dedup,
            }
        )
        return DeviceActionIssued(
            action_id=idempotency_key,
            tool=tool,
            wire_version=CLIENT_WIRE_V2,
            event_fields={**model_args, "calendar_identifier": "uuid-ri-chang"},
        )

    def commit(self, *, intent, idempotency_key, duplicate_override):
        raise AssertionError("a device-executed tool must never reach commit")


def _client(engine, token_ring, keyring, dispatcher) -> TestClient:
    deps = AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=token_ring,
        keyring=keyring,
        identifier_key=IDENTIFIER_KEY,
        cursor_key=CURSOR_KEY,
        build_interpreter=lambda auth: None,
        build_envelope=envelope_factory(keyring),
        build_dispatcher=lambda auth, trace_id: dispatcher,
        build_authorizer=lambda auth: (lambda *, tool, model_args: dict(model_args)),
        capabilities=lambda auth: [],
        now=lambda: NOW,
        sync_wait_seconds=30.0,
        action_keyring=keyring,
    )
    return TestClient(build_app(deps))


def _headers(token_ring, *, key: str = ACTION_KEY, wire: int | None = CLIENT_WIRE_V2):
    headers = {
        "Authorization": f"Bearer {_token(token_ring)}",
        "Idempotency-Key": key,
        "Content-Type": "application/json",
    }
    if wire is not None:
        headers["X-Client-Wire-Version"] = str(wire)
    return headers


def _token(token_ring: TokenKeyRing, device_id: str = "device-1") -> str:
    return issue_access_token(
        token_ring,
        device_id=device_id,
        device_key_thumbprint="THUMB",
        scopes=["calendar.event.write", "calendar.event.read"],
        allowed_tools_version="v1",
        now=NOW,
    )


def _seed_issued(engine, keyring) -> str:
    """One issued device action, parked where the phone's report finds it.

    Seeded through the same two transitions the orchestrator runs, with the
    same two seals, so what the endpoint later opens is a real envelope bound
    to a real operation -- a hand-built stand-in would prove the endpoint can
    read its own fixture.
    """
    intent = WriteIntent(tool="calendar.create_event", model_args=CAL_ARGS)
    with session_factory(engine)() as session:
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
            # The state the issuance transition leaves behind; the seed picks
            # up from there rather than replaying the model turn that led to it.
            state="dispatching",
            state_version=1,
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
            encrypted_request=seal_operation_request(
                keyring, operation_id=operation.operation_id, intent=intent
            ),
            encrypted_device_action=seal_device_action(
                keyring,
                operation_id=operation.operation_id,
                action={
                    "action_id": ACTION_KEY,
                    "tool": "calendar.create_event",
                    "wire_version": CLIENT_WIRE_V2,
                    "event": dict(CAL_ARGS),
                },
            ),
        )
        session.commit()
        return operation.operation_id


def _settle_duplicate(client, token_ring) -> None:
    """The phone's `duplicate` report, which is what makes an override legal."""
    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json={"result": "duplicate", "event_id": EVENT_ID},
        headers=_headers(token_ring),
    )
    assert response.status_code == 200, response.text


def _operations(engine) -> list[Operation]:
    with session_factory(engine)() as session:
        return session.query(Operation).order_by(Operation.created_at).all()


def _override(client, token_ring, **kwargs):
    return client.post(
        f"/v1/device-actions/{ACTION_KEY}/override",
        json={},
        headers=_headers(token_ring, **kwargs),
    )


# --- the derived operation ----------------------------------------------------


def test_a_double_tap_derives_exactly_one_operation(
    engine, token_ring, keyring
) -> None:
    """Two taps are one decision. The key is derived from the action, not sent
    by the client, so the second request reads the first one's operation back
    instead of creating a second -- and the phone is told to skip its lookup
    only because the user has already seen what that lookup found."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    _settle_duplicate(client, token_ring)

    first = _override(client, token_ring)
    second = _override(client, token_ring)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["operation_id"] == second.json()["operation_id"]
    assert first.json()["operation_id"] != "op_1"
    # One action reached the phone, and it was issued to be written even
    # though the device's own check would find the duplicate again.
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["skip_local_dedup"] is True
    assert dispatcher.calls[0]["tool"] == "calendar.create_event"
    assert len(_operations(engine)) == 2


def test_the_derived_operation_names_its_source(engine, token_ring, keyring) -> None:
    """The audit shows two operations, not one silent pass: the write that
    found the duplicate and the deliberate second one, linked by lineage."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    _settle_duplicate(client, token_ring)
    derived_id = _override(client, token_ring).json()["operation_id"]

    with session_factory(engine)() as session:
        derived = session.get(Operation, derived_id)
        assert derived.parent_operation_id == "op_1"


def test_the_key_is_derived_from_the_action_and_never_by_the_caller(
    engine, token_ring, keyring
) -> None:
    """The endpoint mints the key itself. A caller-supplied `Idempotency-Key`
    is ignored -- if it were honoured, two taps carrying different keys would
    write two events."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    _settle_duplicate(client, token_ring)

    first = _override(client, token_ring)
    second = _override(client, token_ring, key="33333333-3333-4333-8333-333333333333")

    assert first.json()["operation_id"] == second.json()["operation_id"]
    derived = [op for op in _operations(engine) if op.operation_id != "op_1"]
    assert [op.idempotency_key for op in derived] == [override_key("op_1")]


def test_two_concurrent_overrides_issue_one_action(
    engine, token_ring, keyring
) -> None:
    """The race the derivation exists for: two taps arriving together must
    converge on one operation. The loser reads the winner's row back — the
    unique index on the derived key is what makes that true, not timing."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    _settle_duplicate(client, token_ring)
    barrier = threading.Barrier(2)
    responses: list = []

    def tap() -> None:
        barrier.wait()
        responses.append(_override(client, token_ring))

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Both taps must *succeed*. Reading then writing means the loser's snapshot
    # is refused the moment the winner commits, and a refusal surfacing as a 500
    # would leave the user staring at a broken card for a decision the server
    # handled correctly -- the retrying commit is what turns that race into the
    # same answer twice.
    assert len(responses) == 2, responses
    assert [response.status_code for response in responses] == [202, 202], [
        response.text for response in responses
    ]
    ids = {response.json()["operation_id"] for response in responses}
    assert len(ids) == 1, responses
    assert len(_operations(engine)) == 2
    # Exactly one action reached the phone. Two would be two events on the
    # calendar, and the phone cannot tell them apart from two intended ones.
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["skip_local_dedup"] is True
    assert dispatcher.calls[0]["key"] == override_key("op_1")


def test_a_retry_after_a_lost_response_answers_the_same_projection(
    engine, token_ring, keyring
) -> None:
    """The app never saw the first answer. The retry must land on the same
    derived operation and hand over the *same* action id -- a fresh action id
    would be a second write wearing one decision."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    _settle_duplicate(client, token_ring)

    first = _override(client, token_ring).json()
    retry = _override(client, token_ring).json()

    assert first["operation_id"] == retry["operation_id"]
    assert first["device_actions"] == retry["device_actions"]
    # The phone executes and reports against the `action_id` it was handed,
    # which is the derived operation's own key -- the one this decision may
    # ever be issued under.
    assert first["device_actions"][0]["action_id"] == override_key("op_1")


def test_a_replay_after_settlement_returns_the_current_projection(
    engine, token_ring, keyring
) -> None:
    """Once the phone reports on the *derived* action, a further tap answers
    what happened rather than issuing anything: the derived key is one
    operation for its whole life, exactly like any other."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    _settle_duplicate(client, token_ring)
    issued = _override(client, token_ring).json()
    # The phone holds the action id, never the operation id: it reports on what
    # it was handed, and the server resolves that back to the derived row.
    assert issued["device_actions"][0]["action_id"] == override_key("op_1")

    report = client.post(
        f"/v1/device-actions/{issued['device_actions'][0]['action_id']}/result",
        json={"result": "created", "event_id": "EK-2"},
        headers=_headers(token_ring),
    )
    assert report.status_code == 200, report.text
    assert report.json()["state"] == "succeeded"

    replay = _override(client, token_ring)
    assert replay.status_code == 200, replay.text
    assert replay.json()["state"] == "succeeded"
    assert replay.json()["record_id"] == "EK-2"
    assert "device_actions" not in replay.json()
    assert len(dispatcher.calls) == 1


# --- who may be overridden ----------------------------------------------------


def test_an_operation_still_waiting_for_its_report_refuses_override(
    engine, token_ring, keyring
) -> None:
    """No report has arrived, so nothing says the event was a duplicate. An
    override here would write a second copy of an event nobody has looked for."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)

    response = _override(client, token_ring)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert dispatcher.calls == []
    assert len(_operations(engine)) == 1


@pytest.mark.parametrize(
    ("reported", "detail"),
    [("created", None), ("denied", "access denied by user")],
)
def test_a_settled_operation_that_was_not_a_duplicate_refuses_override(
    engine, token_ring, keyring, reported, detail
) -> None:
    """`created` and `denied` settle like `duplicate` does; only the report
    itself separates them. Offering 「仍要创建」 for a write that already
    happened -- or for one the device refused -- writes a second event for a
    decision the user never made."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    body = {"result": reported}
    if detail:
        body["detail"] = detail
    else:
        body["event_id"] = EVENT_ID
    settled = client.post(
        f"/v1/device-actions/{ACTION_KEY}/result",
        json=body,
        headers=_headers(token_ring),
    )
    assert settled.status_code == 200, settled.text

    response = _override(client, token_ring)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert dispatcher.calls == []
    assert len(_operations(engine)) == 1


def test_another_devices_action_is_refused(engine, token_ring, keyring) -> None:
    """A valid token for another device must not write through this one's
    decision. The lookup is by action id *under the caller's device*, so the
    refusal is the same opaque one a missing action gets."""
    _seed_issued(engine, keyring)
    with session_factory(engine)() as session:
        session.add(_device("device-2"))
        session.commit()
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    _settle_duplicate(client, token_ring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/override",
        json={},
        headers={
            "Authorization": f"Bearer {_token(token_ring, device_id='device-2')}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400, response.text
    assert dispatcher.calls == []


def test_an_override_carries_no_body(engine, token_ring, keyring) -> None:
    """The closed body is the contract: the decision is *which* action, and a
    payload would be a second, unvalidated channel into an already-decided
    write."""
    _seed_issued(engine, keyring)
    dispatcher = RecordingDispatcher()
    client = _client(engine, token_ring, keyring, dispatcher)
    _settle_duplicate(client, token_ring)

    response = client.post(
        f"/v1/device-actions/{ACTION_KEY}/override",
        json={"decision": "override"},
        headers=_headers(token_ring),
    )

    assert response.status_code == 400, response.text
    assert dispatcher.calls == []


# --- the model's channel never carries the instruction ------------------------


def test_an_ordinary_create_carrying_skip_local_dedup_is_refused() -> None:
    """`skip_local_dedup` is a Host-bound instruction, never an argument. It
    is deliberately *not* in `HOST_ONLY_FIELDS`: that set is stripped from
    model output, and stripping here would silently drop the one field whose
    presence the whole override depends on. Instead the tool's own input schema
    refuses it, so a model that emits it gets a hard rejection."""
    contract = next(
        item for item in TOOL_CONTRACTS if item.name == "calendar.create_event"
    )
    assert "skip_local_dedup" not in HOST_ONLY_FIELDS
    assert "skip_local_dedup" not in contract.model_input_schema["properties"]

    validator = Draft202012Validator(contract.model_input_schema)
    validator.validate(CAL_ARGS)
    with pytest.raises(ValidationError):
        validator.validate({**CAL_ARGS, "skip_local_dedup": True})
