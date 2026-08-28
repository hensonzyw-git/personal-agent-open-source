"""DAL-R08: the operator plane of the Dev Workflow Service (first slice).

Covers the operator identity boundary (separate token schema; a worker token at
an operator endpoint is a refusal, and vice versa), the read endpoints
(pagination bounds, unknown job 404), the single mutation (`cancel`) with its
state binding (stale projection 409, race loss 409, kill switch 503), the
contract-declared 501 for actions awaiting the controller dispatch executor,
the body-digest fence on the mutating POST, and envelope schema separation.

The DB is built by the migration chain (`db.upgrade`) so the revision chain is
exercised, not just `create_all`.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from personal_agent_dal.service.app import (
    BODY_DIGEST_HEADER,
    create_app,
)
from personal_agent_dal.service.operator_tokens import issue_operator_token
from personal_agent_dal.service.tokens import issue_token
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.worker import queue

SERVICE_KEY = b"test-service-key"
ENROLLMENT_SECRET = b"test-enrollment-secret"
OPERATOR_SCHEMA_VERSION = "dal.operator-transport/1.0"
WORKER_SCHEMA_VERSION = "dal.worker-transport/1.0"


@pytest.fixture()
def engine(tmp_path: Path):
    eng = create_database_engine(tmp_path / "operator.db")
    db.upgrade(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine):
    app = create_app(
        engine,
        service_key=SERVICE_KEY,
        enrollment_secret=ENROLLMENT_SECRET,
    )
    return TestClient(app)


def _operator_token(capabilities: list[str] | None = None, *, expired: bool = False) -> str:
    return issue_operator_token(
        operator_id="henson",
        capabilities=capabilities or ["read", "control"],
        expires_at_epoch=int(time.time()) + (-10 if expired else 600),
        key=SERVICE_KEY,
    )


def _read_headers(token: str | None = None) -> dict[str, str]:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _post_json(
    client: TestClient, url: str, payload: dict, token: str | None = None
) -> object:
    """POST exact bytes with the matching body digest, like a real operator CLI."""
    body = json.dumps(payload).encode("utf-8")
    headers = _read_headers(token)
    headers["Content-Type"] = "application/json"
    headers[BODY_DIGEST_HEADER] = hashlib.sha256(body).hexdigest()
    return client.post(url, content=body, headers=headers)


def _worker_token() -> str:
    return issue_token(
        worker_id="w1",
        capabilities=["coding", "verification", "checkpoint"],
        expires_at_epoch=int(time.time()) + 600,
        key=SERVICE_KEY,
    )


def _enroll_worker(client: TestClient) -> None:
    """Register w1 through the operator-gated endpoint, as production does."""
    body = json.dumps(
        {
            "schema_version": WORKER_SCHEMA_VERSION,
            "request_id": "req-enroll-1",
            "worker_id": "w1",
            "machine_id": "mac-1",
            "capabilities": ["coding", "verification", "checkpoint"],
        }
    ).encode("utf-8")
    response = client.post(
        "/enroll",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Enrollment-Secret": ENROLLMENT_SECRET.decode(),
            BODY_DIGEST_HEADER: hashlib.sha256(body).hexdigest(),
        },
    )
    assert response.status_code == 200, response.text


def _seed_job(engine, *, state: str = "pending") -> str:
    job_id = queue.enqueue_job(
        engine,
        feature_id="feat-1",
        repository_id="repo-1",
        base_sha="0" * 40,
        branch_name="codex/feature-feat-1",
        toolchain_ref="toolchain/1",
    )
    if state == "leased":
        claimed = queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60)
        assert claimed == job_id
    return job_id


# --- identity boundary -------------------------------------------------------


def test_operator_endpoints_reject_missing_token(client) -> None:
    response = client.get("/operator/jobs")
    assert response.status_code == 401
    assert response.json()["code"] == "missing_bearer_token"


def test_worker_token_is_refused_at_operator_endpoint(client, engine) -> None:
    worker_token = issue_token(
        worker_id="w1",
        capabilities=["coding"],
        expires_at_epoch=int(time.time()) + 600,
        key=SERVICE_KEY,
    )
    response = client.get("/operator/jobs", headers=_read_headers(worker_token))
    assert response.status_code == 401
    assert response.json()["code"] == "token_invalid"


def test_operator_token_is_refused_at_worker_endpoint(client, engine) -> None:
    worker_token = issue_token(
        worker_id="w1", capabilities=["coding"],
        expires_at_epoch=int(time.time()) + 600, key=SERVICE_KEY,
    )
    operator_token = _operator_token()
    body = {"schema_version": WORKER_SCHEMA_VERSION, "request_id": "r1", "worker_id": "w1"}
    assert (
        _post_json(client, "/jobs/claim", body, operator_token).status_code == 401
    )
    assert (
        _post_json(client, "/jobs/claim", body, worker_token).status_code == 403
    )


def test_expired_operator_token_is_refused(client) -> None:
    response = client.get("/operator/jobs", headers=_read_headers(_operator_token(expired=True)))
    assert response.status_code == 401


def test_tampered_operator_token_is_refused(client) -> None:
    token = _operator_token()
    tampered = token[:-2] + ("00" if token[-2:] != "00" else "11")
    response = client.get("/operator/jobs", headers=_read_headers(tampered))
    assert response.status_code == 401


def test_read_capability_is_required_for_reads(client) -> None:
    token = _operator_token(capabilities=["control"])
    response = client.get("/operator/jobs", headers=_read_headers(token))
    assert response.status_code == 403
    assert response.json()["code"] == "capability_missing"


def test_control_capability_is_required_for_mutation(client, engine) -> None:
    token = _operator_token(capabilities=["read"])
    job_id = _seed_job(engine)
    payload = {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r1",
        "job_id": job_id,
        "action": "cancel",
        "expected_state": "pending",
    }
    response = _post_json(client, f"/operator/jobs/{job_id}/cancel", payload, token)
    assert response.status_code == 403
    assert response.json()["code"] == "capability_missing"


def test_unknown_operator_capability_never_issues(client) -> None:
    with pytest.raises(ValueError):
        issue_operator_token(
            operator_id="henson",
            capabilities=["sudo"],
            expires_at_epoch=int(time.time()) + 600,
            key=SERVICE_KEY,
        )


# --- reads -------------------------------------------------------------------


def test_list_jobs_is_paginated_and_ordered(client, engine) -> None:
    for index in range(3):
        queue.enqueue_job(
            engine,
            feature_id=f"feat-{index}",
            repository_id="repo-1",
            base_sha="0" * 40,
            branch_name=f"codex/feature-feat-{index}",
            toolchain_ref="toolchain/1",
        )
    page = client.get("/operator/jobs?limit=2", headers=_read_headers(_operator_token())).json()
    assert page["schema_version"] == OPERATOR_SCHEMA_VERSION
    assert page["total"] == 3
    assert page["limit"] == 2
    assert [j["feature_id"] for j in page["jobs"]] == ["feat-2", "feat-1"]
    next_page = client.get(
        "/operator/jobs?limit=2&offset=2", headers=_read_headers(_operator_token())
    ).json()
    assert [j["feature_id"] for j in next_page["jobs"]] == ["feat-0"]


def test_list_jobs_rejects_out_of_bounds_pagination(client) -> None:
    response = client.get(
        "/operator/jobs?limit=0", headers=_read_headers(_operator_token())
    )
    assert response.status_code == 400
    response = client.get(
        "/operator/jobs?limit=101", headers=_read_headers(_operator_token())
    )
    assert response.status_code == 400
    response = client.get(
        "/operator/jobs?offset=-1", headers=_read_headers(_operator_token())
    )
    assert response.status_code == 400


def test_job_detail_and_unknown_job(client, engine) -> None:
    job_id = _seed_job(engine, state="leased")
    detail = client.get(
        f"/operator/jobs/{job_id}", headers=_read_headers(_operator_token())
    ).json()
    assert detail["state"] == "leased"
    assert detail["worker_id"] == "w1"
    missing = client.get(
        "/operator/jobs/nope", headers=_read_headers(_operator_token())
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "job_not_found"


def test_checkpoints_listing_and_unknown_job(client, engine) -> None:
    job_id = _seed_job(engine, state="leased")
    _enroll_worker(client)
    # A checkpoint reaches the DB through the worker transport, exactly as in
    # production; the operator plane only ever reads it.
    ck_body = {
        "schema_version": WORKER_SCHEMA_VERSION,
        "request_id": "req-ck-1",
        "job_id": job_id,
        "worker_id": "w1",
        "lease_epoch": 1,  # claim_job advances the epoch to 1
        "sequence": 0,
        "artifact_sha256": "a" * 64,
        "artifact_size_bytes": 10,
        "changed_files": ["src/x.py"],
        "sensitivity": "diff",
    }
    assert _post_json(client, f"/jobs/{job_id}/checkpoint", ck_body, _worker_token()).status_code == 200
    listed = client.get(
        f"/operator/jobs/{job_id}/checkpoints", headers=_read_headers(_operator_token())
    ).json()
    assert listed["checkpoints"][0]["artifact_sha256"] == "a" * 64
    assert listed["checkpoints"][0]["changed_files"] == ["src/x.py"]
    missing = client.get(
        "/operator/jobs/nope/checkpoints", headers=_read_headers(_operator_token())
    )
    assert missing.status_code == 404


# --- mutation: cancel --------------------------------------------------------


def test_cancel_pending_job_with_matching_state(client, engine) -> None:
    job_id = _seed_job(engine)
    payload = {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r1",
        "job_id": job_id,
        "action": "cancel",
        "expected_state": "pending",
    }
    response = _post_json(client, f"/operator/jobs/{job_id}/cancel", payload, _operator_token())
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert queue.get_job(engine, job_id=job_id).state == "cancelled"


def test_cancel_with_stale_expected_state_is_409_zero_write(client, engine) -> None:
    job_id = _seed_job(engine)
    payload = {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r1",
        "job_id": job_id,
        "action": "cancel",
        "expected_state": "running",  # view said running, job is pending
    }
    response = _post_json(client, f"/operator/jobs/{job_id}/cancel", payload, _operator_token())
    assert response.status_code == 409
    assert response.json()["code"] == "state_mismatch"
    assert queue.get_job(engine, job_id=job_id).state == "pending"


def test_cancel_terminal_job_is_409(client, engine) -> None:
    job_id = _seed_job(engine)
    assert queue.cancel_job(engine, job_id=job_id)
    payload = {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r1",
        "job_id": job_id,
        "action": "cancel",
        "expected_state": "pending",
    }
    response = _post_json(client, f"/operator/jobs/{job_id}/cancel", payload, _operator_token())
    assert response.status_code == 409


def test_cancel_unknown_job_is_404(client) -> None:
    payload = {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r1",
        "job_id": "nope",
        "action": "cancel",
        "expected_state": "pending",
    }
    response = _post_json(client, "/operator/jobs/nope/cancel", payload, _operator_token())
    assert response.status_code == 404


def test_cancel_body_digest_is_enforced(client, engine) -> None:
    job_id = _seed_job(engine)
    payload = {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r1",
        "job_id": job_id,
        "action": "cancel",
        "expected_state": "pending",
    }
    body = json.dumps(payload).encode("utf-8")
    response = client.post(
        f"/operator/jobs/{job_id}/cancel",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_operator_token()}",
            BODY_DIGEST_HEADER: "0" * 64,  # wrong digest
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "body_digest"
    assert queue.get_job(engine, job_id=job_id).state == "pending"


def test_non_closed_operator_body_is_invalid(client, engine) -> None:
    job_id = _seed_job(engine)
    payload = {
        "schema_version": "dal.operator-transport/9.9",  # wrong schema version
        "request_id": "r1",
        "job_id": job_id,
        "action": "cancel",
        "expected_state": "pending",
    }
    response = _post_json(client, f"/operator/jobs/{job_id}/cancel", payload, _operator_token())
    assert response.status_code == 400
    assert response.json()["code"] == "invalid"
    # Envelope carries the operator schema, not the worker one.
    assert response.json()["schema_version"] == OPERATOR_SCHEMA_VERSION


def test_pause_and_resume_are_declared_unavailable(client, engine) -> None:
    job_id = _seed_job(engine)
    for action in ("pause", "resume", "request-human", "accept-result"):
        payload = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "job_id": job_id,
            "action": "cancel",  # payload literal is closed; path selects action
            "expected_state": "pending",
        }
        response = _post_json(
            client, f"/operator/jobs/{job_id}/{action}", payload, _operator_token(capabilities=["read", "control"])
        )
        assert response.status_code == 501
        assert response.json()["code"] == "operator_action_not_available"
    assert queue.get_job(engine, job_id=job_id).state == "pending"


def test_kill_switch_blocks_operator_mutation(client, engine, tmp_path: Path) -> None:
    kill_switch = tmp_path / "kill"
    app = create_app(
        engine,
        service_key=SERVICE_KEY,
        enrollment_secret=ENROLLMENT_SECRET,
        kill_switch_path=kill_switch,
    )
    with TestClient(app) as client:
        job_id = _seed_job(engine)
        kill_switch.write_text("stop")
        payload = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "job_id": job_id,
            "action": "cancel",
            "expected_state": "pending",
        }
        response = _post_json(
            client, f"/operator/jobs/{job_id}/cancel", payload, _operator_token()
        )
        assert response.status_code == 503
        assert response.json()["code"] == "kill_switch_active"
        assert queue.get_job(engine, job_id=job_id).state == "pending"


def test_operator_cancel_survives_restart(client, tmp_path: Path) -> None:
    """Durability: the cancelled terminal state is a committed fact."""
    database = tmp_path / "restart.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    app = create_app(
        engine, service_key=SERVICE_KEY, enrollment_secret=ENROLLMENT_SECRET
    )
    with TestClient(app) as client:
        job_id = _seed_job(engine)
        payload = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "job_id": job_id,
            "action": "cancel",
            "expected_state": "pending",
        }
        assert (
            _post_json(client, f"/operator/jobs/{job_id}/cancel", payload, _operator_token()).status_code
            == 200
        )
    engine.dispose()
    engine = create_database_engine(database)
    try:
        assert queue.get_job(engine, job_id=job_id).state == "cancelled"
    finally:
        engine.dispose()
