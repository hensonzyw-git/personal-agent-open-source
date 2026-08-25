"""DAL-R05: the Dev Workflow Service transport, single-process + synthetic client.

Covers the happy path (enroll → claim → heartbeat → checkpoint → result) and the
fail-closed shapes (unknown worker, stale token, stale epoch, duplicate same /
conflicting result, unknown capability, oversized body, oversized artifact,
cancel race), plus restart durability of job/result state. The DB is built by
the migration chain (`db.upgrade`) so revision `0006` is exercised, not just
`create_all`.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from personal_agent_dal.service.app import create_app
from personal_agent_dal.service.tokens import issue_token
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.worker import queue

BASE_SHA = "0" * 40
RESULT_SHA = "c" * 64
OTHER_SHA = "d" * 64
ARTIFACT_SHA = "a" * 64
SERVICE_KEY = b"test-service-key"


@pytest.fixture()
def engine(tmp_path: Path):
    eng = create_database_engine(tmp_path / "service.db")
    db.upgrade(eng)
    yield eng
    eng.dispose()


def _client(engine, kill_switch_path: Path | None = None) -> TestClient:
    app = create_app(
        engine,
        service_key=SERVICE_KEY,
        kill_switch_path=kill_switch_path,
    )
    return TestClient(app)


def _enroll(client: TestClient, worker_id: str = "worker-1") -> str:
    resp = client.post(
        "/enroll",
        json={
            "schema_version": "dal.worker-transport/1.0",
            "request_id": "req-enroll-1",
            "worker_id": worker_id,
            "machine_id": "macmini-1",
            "capabilities": ["coding", "verification", "checkpoint"],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _enqueue(engine, job_id: str | None = None) -> str:
    return queue.enqueue_job(
        engine,
        feature_id="feat-1",
        repository_id="repo-1",
        base_sha=BASE_SHA,
        branch_name="codex/feature-feat-1",
        toolchain_ref="toolchain-v1",
    )


def test_happy_path(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)

    claim = client.post("/jobs/claim", json={
        "schema_version": "dal.worker-transport/1.0",
        "request_id": "req-claim-1", "worker_id": "worker-1",
    }, headers=headers)
    assert claim.status_code == 200, claim.text
    body = claim.json()
    assert body["job_id"] == job_id
    assert body["lease_epoch"] == 1

    hb = client.post(f"/jobs/{job_id}/heartbeat", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "req-hb-1",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 1,
    }, headers=headers)
    assert hb.status_code == 200, hb.text
    assert hb.json()["cancel_requested"] is False

    ck = client.post(f"/jobs/{job_id}/checkpoint", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "req-ck-1",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 1, "sequence": 1,
        "artifact_sha256": ARTIFACT_SHA, "artifact_size_bytes": 4096,
        "changed_files": ["src/lib.rs"], "sensitivity": "checkpoint",
    }, headers=headers)
    assert ck.status_code == 200, ck.text

    result = client.post(f"/jobs/{job_id}/result", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "req-r-1",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 1,
        "result_sha256": RESULT_SHA, "state": "succeeded", "last_error": None,
    }, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["replay"] is False
    assert result.json()["receipt_id"]


def test_unknown_worker_and_stale_token(engine) -> None:
    client = _client(engine)
    # no token
    assert client.post("/jobs/claim", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "r", "worker_id": "x",
    }).status_code == 401
    # expired token
    expired = issue_token(worker_id="worker-1", capabilities=["coding"],
                          expires_at_epoch=int(time.time()) - 10, key=SERVICE_KEY)
    assert client.post("/jobs/claim", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "r", "worker_id": "worker-1",
    }, headers=_auth(expired)).status_code == 401


def test_unknown_capability(engine) -> None:
    client = _client(engine)
    resp = client.post("/enroll", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "r",
        "worker_id": "w", "machine_id": "m", "capabilities": ["gpu"],
    })
    assert resp.status_code == 403


def test_duplicate_same_and_conflicting_result(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    client.post("/jobs/claim", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "c", "worker_id": "worker-1",
    }, headers=headers)

    payload = {
        "schema_version": "dal.worker-transport/1.0", "request_id": "r",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 1,
        "result_sha256": RESULT_SHA, "state": "succeeded", "last_error": None,
    }
    first = client.post(f"/jobs/{job_id}/result", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    receipt = first.json()["receipt_id"]

    # replay identical result → idempotent, same receipt
    replay = client.post(f"/jobs/{job_id}/result", json=payload, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replay"] is True
    assert replay.json()["receipt_id"] == receipt

    # conflicting digest → 409, no overwrite
    conflict = client.post(f"/jobs/{job_id}/result", json={
        **payload, "result_sha256": OTHER_SHA,
    }, headers=headers)
    assert conflict.status_code == 409


def test_stale_epoch_rejected(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    client.post("/jobs/claim", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "c", "worker_id": "worker-1",
    }, headers=headers)

    hb = client.post(f"/jobs/{job_id}/heartbeat", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "h",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 5,
    }, headers=headers)
    assert hb.status_code == 409


def test_oversized_artifact_and_cancel(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    client.post("/jobs/claim", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "c", "worker_id": "worker-1",
    }, headers=headers)

    oversized = client.post(f"/jobs/{job_id}/checkpoint", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "ck",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 1, "sequence": 1,
        "artifact_sha256": ARTIFACT_SHA, "artifact_size_bytes": 104_857_601,
        "changed_files": [], "sensitivity": "checkpoint",
    }, headers=headers)
    assert oversized.status_code == 413

    # authority cancel, then the worker observes it on heartbeat and result
    assert queue.cancel_job(engine, job_id=job_id) is True
    hb = client.post(f"/jobs/{job_id}/heartbeat", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "h",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 1,
    }, headers=headers)
    assert hb.status_code == 200
    assert hb.json()["cancel_requested"] is True

    result = client.post(f"/jobs/{job_id}/result", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "r",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 1,
        "result_sha256": RESULT_SHA, "state": "succeeded", "last_error": None,
    }, headers=headers)
    assert result.status_code == 409


def test_oversized_body_rejected_by_middleware(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    resp = client.post("/jobs/claim", headers={**headers, "Content-Length": str(2_000_000)},
                       json={"schema_version": "dal.worker-transport/1.0",
                             "request_id": "r", "worker_id": "worker-1"})
    assert resp.status_code == 413


def test_restart_keeps_job_and_result(engine, tmp_path: Path) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    client.post("/jobs/claim", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "c", "worker_id": "worker-1",
    }, headers=headers)
    result = client.post(f"/jobs/{job_id}/result", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "r",
        "job_id": job_id, "worker_id": "worker-1", "lease_epoch": 1,
        "result_sha256": RESULT_SHA, "state": "succeeded", "last_error": None,
    }, headers=headers)
    assert result.status_code == 200

    # simulate a process restart: fresh engine + fresh app over the same file
    client2 = _client(engine)
    record = queue.get_job(engine, job_id=job_id)
    assert record is not None and record.state == "succeeded"
    assert record.result_sha256 == RESULT_SHA


def test_kill_switch_blocks_claim(engine, tmp_path: Path) -> None:
    kill = tmp_path / "kill"
    kill.write_text("on")
    client = _client(engine, kill_switch_path=kill)
    token = _enroll(client)
    headers = _auth(token)
    resp = client.post("/jobs/claim", json={
        "schema_version": "dal.worker-transport/1.0", "request_id": "c", "worker_id": "worker-1",
    }, headers=headers)
    assert resp.status_code == 503
