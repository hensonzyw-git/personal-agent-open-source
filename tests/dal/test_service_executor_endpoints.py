"""The F5 executor surface on the operator plane of the DWS.

Pins the production composition of the durable GitHub dispatch executor:

- the wake endpoint accepts exactly the binding (closed schema) — an extra
  field (a payload, an idempotency key, an action) is a 400 invalid, never
  an accepted hint;
- the endpoints are declared 501 when no GitHub adapter is composed, not
  silently absent or 500;
- with an adapter, a wake derives everything from persistence: the outcome
  reflects the effect row's real state, and a forged/duplicate field has no
  path in;
- the state/version binding is enforced at the HTTP face (409), and the
  kill switch blocks the mutation (503);
- the reconcile sweep answers from the persistence-driven pass and issues
  only reads.

The service-level composition is additionally pinned from the CLI side in
``test_service_cli_github_composition.py``.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from personal_agent_dal.github.adapter import PushOutcome
from personal_agent_dal.github.adapter_controller import fingerprint_for
from personal_agent_dal.github.executor import record_effect_target
from personal_agent_dal.service.app import BODY_DIGEST_HEADER, create_app
from personal_agent_dal.service.operator_tokens import issue_operator_token
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory
from personal_agent_core.timeutil import utc_now

from tests.dal.factories import external_effect_row, feature_row

SERVICE_KEY = b"test-service-key"
ENROLLMENT_SECRET = b"test-enrollment-secret"
OPERATOR_SCHEMA_VERSION = "dal.operator-transport/1.0"
BRANCH = "dal/feat-1"
HEAD = "a" * 40


class StubAdapter:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def push_feature_branch(self, **_: object) -> object:
        self.calls.append("push")
        return self.outcome

    def create_pull_request(self, **_: object) -> object:
        self.calls.append("pr")
        return self.outcome

    def write_check_run(self, **_: object) -> object:
        self.calls.append("check")
        return self.outcome

    def read_feature_branch(self, **_: object) -> object:
        self.calls.append("read_branch")
        return self.outcome

    def list_open_pull_requests(self, **_: object) -> object:
        self.calls.append("read_prs")
        return self.outcome

    def read_check_run(self, **_: object) -> object:
        self.calls.append("read_check")
        return self.outcome


CONFIRMED_PUSH = PushOutcome(
    repository_id="example-owner/dal-sandbox", branch=BRANCH, head_sha=HEAD
)


@pytest.fixture()
def engine(tmp_path: Path):
    eng = create_database_engine(tmp_path / "executor-svc.db")
    db.upgrade(eng)
    yield eng
    eng.dispose()


def _client(engine, adapter: object | None) -> TestClient:
    app = create_app(
        engine,
        service_key=SERVICE_KEY,
        enrollment_secret=ENROLLMENT_SECRET,
        github_adapter=adapter,
    )
    return TestClient(app)


def _operator_token() -> str:
    return issue_operator_token(
        operator_id="henson",
        capabilities=["read", "control"],
        expires_at_epoch=int(time.time()) + 600,
        key=SERVICE_KEY,
    )


def _post_json(client: TestClient, url: str, payload: dict) -> object:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {_operator_token()}",
        "Content-Type": "application/json",
        BODY_DIGEST_HEADER: hashlib.sha256(body).hexdigest(),
    }
    return client.post(url, content=body, headers=headers)


def seed_wakeable(engine, *, effect_id: str = "effect-svc-1",
                  feature_id: str = "feature-svc-1") -> tuple[str, str]:
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        session.add(feature_row(
            feature_id=feature_id, version=3, state="awaiting_merge", now=now
        ))
        session.add(external_effect_row(
            effect_id=effect_id, owner_id=feature_id, version=1,
            state="intent_recorded", now=now,
        ))
        from personal_agent_dal.storage.machine_models import ExternalEffect

        row = session.get(ExternalEffect, effect_id)
        assert row is not None
        row.target_fingerprint = fingerprint_for(
            "push_branch", {"branch": BRANCH, "head_sha": HEAD}
        )
    with session_factory(engine)() as session, session.begin():
        record_effect_target(
            session, effect_id=effect_id, action="push_branch",
            payload={"branch": BRANCH, "head_sha": HEAD}, now=utc_now(),
        )
    return feature_id, effect_id


def effect_state(engine, effect_id: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT state FROM external_effects WHERE effect_id = :e")
            .bindparams(e=effect_id)
        ).scalar_one()


def test_wake_endpoint_composes_the_real_executor(engine) -> None:
    _feature_id, effect_id = seed_wakeable(engine)
    adapter = StubAdapter(CONFIRMED_PUSH)
    client = _client(engine, adapter)
    response = _post_json(
        client,
        f"/operator/effects/{effect_id}/wake",
        {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "effect_id": effect_id,
            "expected_state": "intent_recorded",
            "expected_version": 1,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effect_state"] == "dispatch_started"
    assert body["refusal"] is None
    assert adapter.calls == ["push"]
    assert effect_state(engine, effect_id) == "dispatch_started"


def test_wake_body_cannot_carry_a_target(engine) -> None:
    """The frozen scope, enforced by the closed schema: any extra field is
    400 invalid — a payload or key has nowhere to ride in."""
    _feature_id, effect_id = seed_wakeable(engine)
    client = _client(engine, StubAdapter(CONFIRMED_PUSH))
    for extra in (
        {"action": "push_branch"},
        {"payload": {"branch": "dal/evil", "head_sha": "b" * 40}},
        {"idempotency_key": "forged-key"},
        {"branch": "dal/evil"},
    ):
        payload = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "effect_id": effect_id,
            "expected_state": "intent_recorded",
            "expected_version": 1,
            **extra,
        }
        response = _post_json(client, f"/operator/effects/{effect_id}/wake", payload)
        assert response.status_code == 400, extra
        assert response.json()["code"] == "invalid"
    assert effect_state(engine, effect_id) == "intent_recorded"


def test_wake_state_version_binding_is_enforced_at_http(engine) -> None:
    _feature_id, effect_id = seed_wakeable(engine)
    client = _client(engine, StubAdapter(CONFIRMED_PUSH))
    for field, value in (("expected_state", "claimed"), ("expected_version", 7)):
        payload = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "effect_id": effect_id,
            "expected_state": "intent_recorded",
            "expected_version": 1,
        }
        payload[field] = value
        response = _post_json(client, f"/operator/effects/{effect_id}/wake", payload)
        assert response.status_code == 409, field
        assert response.json()["code"] in ("state_mismatch", "version_mismatch")
    assert effect_state(engine, effect_id) == "intent_recorded"


def test_wake_of_unknown_effect_is_404(engine) -> None:
    client = _client(engine, StubAdapter(CONFIRMED_PUSH))
    response = _post_json(
        client,
        "/operator/effects/effect-absent/wake",
        {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "effect_id": "effect-absent",
            "expected_state": "intent_recorded",
            "expected_version": 1,
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "effect_not_found"


def test_wake_path_body_mismatch_is_400(engine) -> None:
    _feature_id, effect_id = seed_wakeable(engine)
    client = _client(engine, StubAdapter(CONFIRMED_PUSH))
    response = _post_json(
        client,
        f"/operator/effects/{effect_id}/wake",
        {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "effect_id": "effect-svc-OTHER",
            "expected_state": "intent_recorded",
            "expected_version": 1,
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "effect_mismatch"


def test_endpoints_answer_501_without_a_composed_adapter(engine) -> None:
    """Without the GitHub adapter the surface is declared unavailable — the
    contract's 501, never a silent absence or a 500."""
    client = _client(engine, None)
    get = client.get(
        "/operator/effects",
        headers={"Authorization": f"Bearer {_operator_token()}"},
    )
    assert get.status_code == 501
    assert get.json()["code"] == "executor_not_composed"
    post = _post_json(
        client,
        "/operator/effects/effect-svc-1/wake",
        {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "effect_id": "effect-svc-1",
            "expected_state": "intent_recorded",
            "expected_version": 1,
        },
    )
    assert post.status_code == 501
    sweep = _post_json(client, "/operator/effects/reconcile-sweep", {})
    assert sweep.status_code == 501


def test_kill_switch_blocks_wake(engine, tmp_path: Path) -> None:
    kill_switch = tmp_path / "kill"
    app = create_app(
        engine,
        service_key=SERVICE_KEY,
        enrollment_secret=ENROLLMENT_SECRET,
        kill_switch_path=kill_switch,
        github_adapter=StubAdapter(CONFIRMED_PUSH),
    )
    client = TestClient(app)
    _feature_id, effect_id = seed_wakeable(engine)
    kill_switch.write_text("stop")
    response = _post_json(
        client,
        f"/operator/effects/{effect_id}/wake",
        {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "effect_id": effect_id,
            "expected_state": "intent_recorded",
            "expected_version": 1,
        },
    )
    assert response.status_code == 503
    assert response.json()["code"] == "kill_switch_active"
    assert effect_state(engine, effect_id) == "intent_recorded"


def test_read_capability_cannot_wake(engine) -> None:
    _feature_id, effect_id = seed_wakeable(engine)
    client = _client(engine, StubAdapter(CONFIRMED_PUSH))
    token = issue_operator_token(
        operator_id="henson",
        capabilities=["read"],
        expires_at_epoch=int(time.time()) + 600,
        key=SERVICE_KEY,
    )
    body = json.dumps({
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r1",
        "effect_id": effect_id,
        "expected_state": "intent_recorded",
        "expected_version": 1,
    }).encode("utf-8")
    response = client.post(
        f"/operator/effects/{effect_id}/wake",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            BODY_DIGEST_HEADER: hashlib.sha256(body).hexdigest(),
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "capability_missing"


def test_listing_endpoint_shows_unknown_effects(engine) -> None:
    client = _client(engine, StubAdapter(CONFIRMED_PUSH))
    response = client.get(
        "/operator/effects?limit=5",
        headers={"Authorization": f"Bearer {_operator_token()}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["effects"] == []
    # F2 (2026-09-07 review): the additive `reconciling` key surfaces claims
    # in flight; a crashed reconciler is no longer invisible to the listing.
    assert response.json()["reconciling"] == []


def test_listing_endpoint_includes_reconciling_claims(engine) -> None:
    """A reconciler claim in flight appears under the `reconciling` key."""
    from tests.dal.factories import external_effect_row, feature_row
    from personal_agent_dal.storage.engine import session_factory
    from personal_agent_core.timeutil import utc_now

    with session_factory(engine)() as session, session.begin():
        session.add(
            feature_row(
                feature_id="feature-rec-1", version=4,
                state="reconciliation_required", now=utc_now(),
            )
        )
        session.add(
            external_effect_row(
                effect_id="effect-rec-1", owner_id="feature-rec-1",
                version=6, state="reconciling", now=utc_now(),
            )
        )
    with session_factory(engine)() as session, session.begin():
        from personal_agent_dal.storage.machine_models import ExternalEffect

        row = session.get(ExternalEffect, "effect-rec-1")
        assert row is not None
        row.executor_id = "reconciler"

    client = _client(engine, StubAdapter(CONFIRMED_PUSH))
    response = client.get(
        "/operator/effects?limit=5",
        headers={"Authorization": f"Bearer {_operator_token()}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effects"] == []
    assert [e["effect_id"] for e in body["reconciling"]] == ["effect-rec-1"]
    assert body["reconciling"][0]["version"] == 6
