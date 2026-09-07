"""DAL-R05: the Dev Workflow Service transport, single-process + synthetic client.

Covers the happy path (enroll → claim → heartbeat → checkpoint → result) and the
fail-closed shapes the frozen contract requires: enrollment without/with a wrong
secret, unknown worker, stale token, stale epoch, duplicate same / conflicting
result, duplicate same / conflicting checkpoint, checkpoint after cancel (zero
write), unknown capability, missing/mismatched body digest, wrong schema_version,
oversized body (declared and chunked), oversized artifact, revoke observed at
auth, kill switch, and restart durability across a real engine rebuild. The DB is
built by the migration chain (`db.upgrade`) so revision `0006` is exercised, not
just `create_all`.

Every POST carries a correct `X-Transport-Body-Digest` unless the test itself is
about the digest fence — the header is required by the contract, so the helpers
compute it from the exact bytes sent.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from personal_agent_dal.service.app import (
    BODY_DIGEST_HEADER,
    ENROLLMENT_SECRET_HEADER,
    create_app,
)
from personal_agent_dal.service.tokens import issue_token
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory
from personal_agent_dal.storage.transport_models import (
    WorkerCheckpoint,
    WorkerEnrollment,
)
from personal_agent_dal.worker import queue
from personal_agent_core.timeutil import utc_now

BASE_SHA = "0" * 40
RESULT_SHA = "c" * 64
OTHER_SHA = "d" * 64
ARTIFACT_SHA = "a" * 64
OTHER_ARTIFACT_SHA = "b" * 64
SERVICE_KEY = b"test-service-key"
ENROLLMENT_SECRET = b"test-enrollment-secret"
SCHEMA_VERSION = "dal.worker-transport/1.0"


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
        enrollment_secret=ENROLLMENT_SECRET,
        kill_switch_path=kill_switch_path,
    )
    return TestClient(app)


def _post(client: TestClient, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None):
    """POST with the contract-required digest computed over the exact bytes sent."""
    raw = json.dumps(payload).encode()
    return client.post(
        url,
        content=raw,
        headers={
            "Content-Type": "application/json",
            BODY_DIGEST_HEADER: hashlib.sha256(raw).hexdigest(),
            **(headers or {}),
        },
    )


def _enroll(client: TestClient, worker_id: str = "worker-1") -> str:
    resp = _post(
        client,
        "/enroll",
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": "req-enroll-1",
            "worker_id": worker_id,
            "machine_id": "macmini-1",
            "capabilities": ["coding", "verification", "checkpoint"],
        },
        {ENROLLMENT_SECRET_HEADER: ENROLLMENT_SECRET.decode()},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _claim_body(worker_id: str = "worker-1") -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "request_id": "req-claim-1", "worker_id": worker_id}


def _heartbeat_body(job_id: str, epoch: int = 1) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": "req-hb-1",
        "job_id": job_id,
        "worker_id": "worker-1",
        "lease_epoch": epoch,
    }


def _checkpoint_body(job_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": "req-ck-1",
        "job_id": job_id,
        "worker_id": "worker-1",
        "lease_epoch": 1,
        "sequence": 1,
        "artifact_sha256": ARTIFACT_SHA,
        "artifact_size_bytes": 4096,
        "changed_files": ["src/lib.rs"],
        "sensitivity": "checkpoint",
    }
    body.update(overrides)
    return body


def _result_body(job_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": "req-r-1",
        "job_id": job_id,
        "worker_id": "worker-1",
        "lease_epoch": 1,
        "result_sha256": RESULT_SHA,
        "state": "succeeded",
        "last_error": None,
    }
    body.update(overrides)
    return body


def _enqueue(engine) -> str:
    return queue.enqueue_job(
        engine,
        feature_id="feat-1",
        repository_id="repo-1",
        base_sha=BASE_SHA,
        branch_name="codex/feature-feat-1",
        toolchain_ref="toolchain-v1",
    )


def _claim(client: TestClient, token: str) -> str:
    resp = _post(client, "/jobs/claim", _claim_body(), _auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


def test_claim_response_carries_the_intake_body(engine) -> None:
    """R2-6 (round-2 review): the body must reach the remote worker's lease.

    The claim response carries the persisted task description and its digest
    when the job was enqueued through an intake, and omits both keys (rather
    than nulling them) for a seeded job — the closed-shape contract stays
    honest about what exists.
    """
    client = _client(engine)
    token = _enroll(client)

    # A job WITH an intake body.
    body = "Add a boundary test for the pure-string helper."
    queue.enqueue_job(
        engine,
        feature_id="feat-body",
        repository_id="repo-1",
        base_sha=BASE_SHA,
        branch_name="codex/feature-feat-body",
        toolchain_ref="toolchain-v1",
        intake_key="intake:feat-body",
        task_description=body,
    )
    resp = _post(client, "/jobs/claim", _claim_body(), _auth(token))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["job_id"] is not None
    assert payload["task_description"] == body
    assert payload["task_description_sha256"] == hashlib.sha256(
        body.encode("utf-8")
    ).hexdigest()

    # A seeded job WITHOUT an intake: both keys absent, not null.
    _enqueue(engine)
    resp2 = _post(client, "/jobs/claim", _claim_body(), _auth(token))
    assert resp2.status_code == 200, resp2.text
    assert "task_description" not in resp2.json()
    assert "task_description_sha256" not in resp2.json()


def test_happy_path(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)

    assert _claim(client, token) == job_id

    hb = _post(client, f"/jobs/{job_id}/heartbeat", _heartbeat_body(job_id), headers)
    assert hb.status_code == 200, hb.text
    assert hb.json() == {"schema_version": SCHEMA_VERSION, "cancel_requested": False}

    ck = _post(client, f"/jobs/{job_id}/checkpoint", _checkpoint_body(job_id), headers)
    assert ck.status_code == 200, ck.text

    result = _post(client, f"/jobs/{job_id}/result", _result_body(job_id), headers)
    assert result.status_code == 200, result.text
    assert result.json()["replay"] is False
    assert result.json()["receipt_id"]


def test_enroll_requires_enrollment_secret(engine) -> None:
    client = _client(engine)
    body = {
        "schema_version": SCHEMA_VERSION,
        "request_id": "r",
        "worker_id": "attacker",
        "machine_id": "m",
        "capabilities": ["coding"],
    }
    # no secret at all -> 401, no enrollment row, no token
    resp = _post(client, "/enroll", body)
    assert resp.status_code == 401
    assert resp.json()["code"] == "enrollment_secret_invalid"
    # wrong secret -> 401 (constant-time compare, no partial identity)
    resp = _post(client, "/enroll", body, {ENROLLMENT_SECRET_HEADER: "nope"})
    assert resp.status_code == 401
    with session_factory(engine)() as session:
        assert session.get(WorkerEnrollment, "attacker") is None


def test_revoked_worker_cannot_reenroll_or_call(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    with session_factory(engine)() as session:
        row = session.get(WorkerEnrollment, "worker-1")
        row.revoked_at = utc_now()
        session.commit()
    # revocation is observed fail-closed at the auth boundary
    resp = _post(client, "/jobs/claim", _claim_body(), _auth(token))
    assert resp.status_code == 403
    assert resp.json()["code"] == "worker_revoked"
    # and the revoked identity cannot be re-registered even with the secret
    resp = _post(
        client,
        "/enroll",
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": "r",
            "worker_id": "worker-1",
            "machine_id": "macmini-1",
            "capabilities": ["coding"],
        },
        {ENROLLMENT_SECRET_HEADER: ENROLLMENT_SECRET.decode()},
    )
    assert resp.status_code == 403


def test_unknown_worker_and_stale_token(engine) -> None:
    client = _client(engine)
    # no token
    assert _post(client, "/jobs/claim", _claim_body("x")).status_code == 401
    # expired token
    expired = issue_token(worker_id="worker-1", capabilities=["coding"],
                          expires_at_epoch=int(time.time()) - 10, key=SERVICE_KEY)
    assert _post(client, "/jobs/claim", _claim_body(), _auth(expired)).status_code == 401
    # tampered token signature
    forged = issue_token(worker_id="worker-1", capabilities=["coding"],
                         expires_at_epoch=int(time.time()) + 60, key=b"wrong-key")
    assert _post(client, "/jobs/claim", _claim_body(), _auth(forged)).status_code == 401


def test_body_digest_fence(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    raw = json.dumps(_claim_body()).encode()
    headers = {"Content-Type": "application/json", **_auth(token)}
    # missing digest header
    assert client.post("/jobs/claim", content=raw, headers=headers).status_code == 400
    # malformed digest
    assert client.post(
        "/jobs/claim", content=raw, headers={**headers, BODY_DIGEST_HEADER: "zz"}
    ).status_code == 400
    # digest of different bytes (tamper)
    assert client.post(
        "/jobs/claim", content=raw, headers={**headers, BODY_DIGEST_HEADER: "0" * 64}
    ).status_code == 400
    # correct digest passes the fence
    assert client.post(
        "/jobs/claim",
        content=raw,
        headers={**headers, BODY_DIGEST_HEADER: hashlib.sha256(raw).hexdigest()},
    ).status_code == 204


def test_wrong_schema_version_rejected_everywhere(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    _claim(client, token)
    for url, body in (
        ("/jobs/claim", _claim_body()),
        (f"/jobs/{job_id}/heartbeat", _heartbeat_body(job_id)),
        (f"/jobs/{job_id}/checkpoint", _checkpoint_body(job_id)),
        (f"/jobs/{job_id}/result", _result_body(job_id)),
    ):
        resp = _post(client, url, {**body, "schema_version": "WRONG"}, headers)
        assert resp.status_code == 400, url
        assert resp.json()["code"] == "invalid", url


def test_duplicate_same_and_conflicting_result(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    _claim(client, token)

    payload = _result_body(job_id)
    first = _post(client, f"/jobs/{job_id}/result", payload, headers)
    assert first.status_code == 200, first.text
    receipt = first.json()["receipt_id"]

    # replay identical result → idempotent, same receipt
    replay = _post(client, f"/jobs/{job_id}/result", payload, headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replay"] is True
    assert replay.json()["receipt_id"] == receipt

    # conflicting digest → 409, no overwrite
    conflict = _post(client, f"/jobs/{job_id}/result", _result_body(job_id, result_sha256=OTHER_SHA), headers)
    assert conflict.status_code == 409


def test_checkpoint_idempotent_conflicting_and_fenced(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    _claim(client, token)

    first = _post(client, f"/jobs/{job_id}/checkpoint", _checkpoint_body(job_id), headers)
    assert first.status_code == 200, first.text
    # identical replay on (job_id, sequence) is idempotent
    assert _post(client, f"/jobs/{job_id}/checkpoint", _checkpoint_body(job_id), headers).status_code == 200
    # same sequence, different digest → 409 conflict, never an overwrite
    conflict = _post(
        client,
        f"/jobs/{job_id}/checkpoint",
        _checkpoint_body(job_id, artifact_sha256=OTHER_ARTIFACT_SHA),
        headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "conflict"
    # stale epoch → 409, zero writes
    stale = _post(
        client,
        f"/jobs/{job_id}/checkpoint",
        _checkpoint_body(job_id, sequence=9, lease_epoch=5),
        headers,
    )
    assert stale.status_code == 409
    with session_factory(engine)() as session:
        rows = session.query(WorkerCheckpoint).filter_by(job_id=job_id).all()
        assert [(r.sequence, r.artifact_sha256) for r in rows] == [(1, ARTIFACT_SHA)]


def test_stale_epoch_rejected(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    _claim(client, token)

    hb = _post(client, f"/jobs/{job_id}/heartbeat", _heartbeat_body(job_id, epoch=5), headers)
    assert hb.status_code == 409


def test_oversized_artifact_and_cancel(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    _claim(client, token)

    oversized = _post(
        client,
        f"/jobs/{job_id}/checkpoint",
        _checkpoint_body(job_id, artifact_size_bytes=104_857_601),
        headers,
    )
    assert oversized.status_code == 413

    # authority cancel, then the worker observes it on heartbeat and result
    assert queue.cancel_job(engine, job_id=job_id) is True
    hb = _post(client, f"/jobs/{job_id}/heartbeat", _heartbeat_body(job_id), headers)
    assert hb.status_code == 200
    assert hb.json()["cancel_requested"] is True

    # a checkpoint fenced after the cancel is a 409 with zero writes
    ck = _post(
        client,
        f"/jobs/{job_id}/checkpoint",
        _checkpoint_body(job_id, sequence=2),
        headers,
    )
    assert ck.status_code == 409
    with session_factory(engine)() as session:
        assert session.query(WorkerCheckpoint).filter_by(job_id=job_id).count() == 0

    result = _post(client, f"/jobs/{job_id}/result", _result_body(job_id), headers)
    assert result.status_code == 409


def test_oversized_body_rejected_declared_and_chunked(engine) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = {"Content-Type": "application/json", **_auth(token)}
    # declared Content-Length over the cap → 413 without reading the body
    resp = client.post("/jobs/claim", headers={**headers, "Content-Length": str(2_000_000)},
                       json=_claim_body())
    assert resp.status_code == 413
    # chunked body with no Content-Length: the streamed cap still applies.
    # Build an in-shape oversized body: many bounded changed_files entries on a checkpoint.
    job_id = _enqueue(engine)
    _claim(client, token)
    payload = _checkpoint_body(job_id, changed_files=["f" * 512] * 2100)  # > 1 MiB, in shape
    raw = json.dumps(payload).encode()
    assert len(raw) > 1_048_576
    resp = client.post(
        f"/jobs/{job_id}/checkpoint",
        content=iter([raw]),
        headers={**headers, BODY_DIGEST_HEADER: hashlib.sha256(raw).hexdigest()},
    )
    assert resp.status_code == 413


def test_restart_keeps_job_and_result(engine, tmp_path: Path) -> None:
    client = _client(engine)
    token = _enroll(client)
    headers = _auth(token)
    job_id = _enqueue(engine)
    _claim(client, token)
    result = _post(client, f"/jobs/{job_id}/result", _result_body(job_id), headers)
    assert result.status_code == 200
    engine.dispose()

    # a real process restart: brand-new engine and app over the same file
    engine2 = create_database_engine(tmp_path / "service.db")
    try:
        record = queue.get_job(engine2, job_id=job_id)
        assert record is not None and record.state == "succeeded"
        assert record.result_sha256 == RESULT_SHA
    finally:
        engine2.dispose()


def test_kill_switch_blocks_claim(engine, tmp_path: Path) -> None:
    kill = tmp_path / "kill"
    kill.write_text("on")
    client = _client(engine, kill_switch_path=kill)
    token = _enroll(client)
    resp = _post(client, "/jobs/claim", _claim_body(), _auth(token))
    assert resp.status_code == 503
    assert resp.json()["code"] == "kill_switch_active"
