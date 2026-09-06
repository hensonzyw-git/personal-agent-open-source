"""The intake producer: a task request -> Feature (intake) + pending Job.

Pins the source-agnostic core and its two entry points (the operator console
``intake`` verb and the ``POST /operator/intake`` endpoint) against an in-memory
real SQLite schema and the frozen ``SM-CREATE`` transition. Verified facts:

- a fresh intake creates a ``Feature`` at ``intake`` and one pending ``Job``
  linked by ``feature_id``, with the server-derived branch and idempotency key;
- a re-run is a replay (``duplicate=True``) that creates neither a second
  feature nor a second job;
- malformed input fails closed (no feature, no job);
- the endpoint is kill-switch gated (503), rejects an extra field (400), and is
  idempotent on request identity;
- the operator CLI ``intake`` verb talks to the endpoint and prints id.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path

import contextlib
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from uvicorn import Config as UvicornConfig
from uvicorn import Server

from personal_agent_dal.service import operator_cli
from personal_agent_dal.service.app import BODY_DIGEST_HEADER, create_app
from personal_agent_dal.service.intake import IntakeRefusal, intake_task
from personal_agent_dal.service.operator_tokens import issue_operator_token
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.worker import queue

SERVICE_KEY = b"test-service-key"
ENROLLMENT_SECRET = b"test-enrollment-secret"
OPERATOR_SCHEMA_VERSION = "dal.operator-transport/1.0"

REPO = "example-owner/dal-sandbox"
BASE_SHA = "a" * 40
TOOLCHAIN = ".personal-agent/toolchain.json"
DESC = "Add a boundary test for the pure-string helper."


@pytest.fixture()
def engine(tmp_path: Path):
    eng = create_database_engine(tmp_path / "intake.db")
    db.upgrade(eng)
    yield eng
    eng.dispose()


def _client(engine, *, kill_switch_path: Path | None = None) -> TestClient:
    app = create_app(
        engine,
        service_key=SERVICE_KEY,
        enrollment_secret=ENROLLMENT_SECRET,
        kill_switch_path=kill_switch_path,
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


# --- core unit --------------------------------------------------------------


def _row_count(engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(
            sa.text(f"SELECT count(*) FROM {table}")
        ).scalar_one()


def test_intake_creates_feature_at_intake_and_a_pending_job(engine) -> None:
    outcome = intake_task(
        engine,
        task_description=DESC,
        repository_id=REPO,
        base_sha=BASE_SHA,
        toolchain_ref=TOOLCHAIN,
    )

    assert outcome.feature_state == "intake"
    assert not outcome.duplicate
    assert outcome.feature_id
    assert outcome.job_id

    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT state FROM features WHERE feature_id = :f")
            .bindparams(f=outcome.feature_id)
        ).one()
        assert row.state == "intake"

    job = queue.get_job(engine, job_id=outcome.job_id)
    assert job is not None
    assert job.feature_id == outcome.feature_id
    assert job.repository_id == REPO
    assert job.base_sha == BASE_SHA
    assert job.toolchain_ref == TOOLCHAIN
    assert job.state == "pending"
    # The worker enforces this exact branch shape (poll_once.py expected_branch),
    # so the intake must produce it or the job is refused on claim.
    assert job.branch_name == f"codex/feature-{outcome.feature_id}"


def test_intake_replay_is_idempotent(engine) -> None:
    first = intake_task(
        engine, task_description=DESC, repository_id=REPO,
        base_sha=BASE_SHA, toolchain_ref=TOOLCHAIN,
    )
    second = intake_task(
        engine, task_description=DESC, repository_id=REPO,
        base_sha=BASE_SHA, toolchain_ref=TOOLCHAIN,
    )

    assert second.duplicate
    assert second.feature_id == first.feature_id
    assert second.job_id == first.job_id
    # One feature, one job — the replay added nothing.
    assert _row_count(engine, "features") == 1
    assert _row_count(engine, "worker_jobs") == 1


def test_intake_recovers_an_interrupted_enqueue(engine) -> None:
    """A first run that created the feature but never enqueued is repaired by the retry."""
    from personal_agent_dal.service.intake import intake_task as _intake

    # Simulate the partial failure: create the feature, then a retry must enqueue.
    # We cannot easily interrupt between apply_transition and enqueue, so drive
    # the recovery branch directly: pre-create the feature via a first intake,
    # delete the job, and confirm the next intake re-enqueues it.
    first = intake_task(
        engine, task_description=DESC, repository_id=REPO,
        base_sha=BASE_SHA, toolchain_ref=TOOLCHAIN,
    )
    with engine.connect() as connection:
        connection.execute(
            sa.text("DELETE FROM worker_jobs WHERE job_id = :j")
            .bindparams(j=first.job_id)
        )
        connection.commit()

    second = intake_task(
        engine, task_description=DESC, repository_id=REPO,
        base_sha=BASE_SHA, toolchain_ref=TOOLCHAIN,
    )
    # The feature is a replay, but the missing job is re-enqueued, not refused.
    assert second.duplicate
    assert second.job_id != first.job_id
    assert _row_count(engine, "features") == 1
    assert _row_count(engine, "worker_jobs") == 1


def test_intake_fails_closed_on_bad_base_sha(engine) -> None:
    with pytest.raises(IntakeRefusal) as exc:
        intake_task(
            engine, task_description=DESC, repository_id=REPO,
            base_sha="not-a-sha", toolchain_ref=TOOLCHAIN,
        )
    assert exc.value.code == "invalid"
    assert _row_count(engine, "features") == 0
    assert _row_count(engine, "worker_jobs") == 0


def test_intake_fails_closed_on_empty_description(engine) -> None:
    with pytest.raises(IntakeRefusal) as exc:
        intake_task(
            engine, task_description="   ", repository_id=REPO,
            base_sha=BASE_SHA, toolchain_ref=TOOLCHAIN,
        )
    assert exc.value.code == "invalid"
    assert _row_count(engine, "features") == 0


# --- HTTP endpoint ----------------------------------------------------------


def test_intake_endpoint_creates_feature_and_job(engine) -> None:
    client = _client(engine)
    response = _post_json(
        client,
        "/operator/intake",
        {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "repository_id": REPO,
            "base_sha": BASE_SHA,
            "toolchain_ref": TOOLCHAIN,
            "task_description": DESC,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["feature_state"] == "intake"
    assert body["duplicate"] is False
    assert body["feature_id"] and body["job_id"]
    assert _row_count(engine, "features") == 1
    assert _row_count(engine, "worker_jobs") == 1


def test_intake_endpoint_rejects_an_extra_field(engine) -> None:
    client = _client(engine)
    payload = {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r1",
        "repository_id": REPO,
        "base_sha": BASE_SHA,
        "toolchain_ref": TOOLCHAIN,
        "task_description": DESC,
        "branch_name": "operator-injected/branch",  # must not ride in
    }
    response = _post_json(client, "/operator/intake", payload)
    assert response.status_code == 400
    assert _row_count(engine, "features") == 0
    assert _row_count(engine, "worker_jobs") == 0


def test_intake_endpoint_is_kill_switch_gated(engine, tmp_path: Path) -> None:
    kill_switch = tmp_path / "kill"
    kill_switch.write_text("stop")
    client = _client(engine, kill_switch_path=kill_switch)
    response = _post_json(
        client,
        "/operator/intake",
        {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": "r1",
            "repository_id": REPO,
            "base_sha": BASE_SHA,
            "toolchain_ref": TOOLCHAIN,
            "task_description": DESC,
        },
    )
    assert response.status_code == 503
    assert response.json()["code"] == "kill_switch_active"
    assert _row_count(engine, "features") == 0
    assert _row_count(engine, "worker_jobs") == 0


def test_intake_endpoint_is_idempotent_on_request_identity(engine) -> None:
    client = _client(engine)
    body = {
        "schema_version": OPERATOR_SCHEMA_VERSION,
        "request_id": "r-idem",
        "repository_id": REPO,
        "base_sha": BASE_SHA,
        "toolchain_ref": TOOLCHAIN,
        "task_description": DESC,
    }
    first = _post_json(client, "/operator/intake", body)
    second = _post_json(client, "/operator/intake", body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["feature_id"] == first.json()["feature_id"]
    assert _row_count(engine, "features") == 1
    assert _row_count(engine, "worker_jobs") == 1


# --- operator CLI verb --------------------------------------------------------


@pytest.fixture()
def live_server(tmp_path: Path):
    """A real uvicorn server on loopback, so the CLI exercises real HTTP."""
    engine = create_database_engine(tmp_path / "intake-cli.db")
    db.upgrade(engine)
    app = create_app(engine, service_key=SERVICE_KEY, enrollment_secret=ENROLLMENT_SECRET)
    config = UvicornConfig(app, host="127.0.0.1", port=0, log_level="error")
    server = Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", engine
    server.should_exit = True
    thread.join(timeout=5)
    engine.dispose()


@pytest.fixture()
def token_file(tmp_path: Path) -> Path:
    token = issue_operator_token(
        operator_id="henson",
        capabilities=["read", "control"],
        expires_at_epoch=int(time.time()) + 600,
        key=SERVICE_KEY,
    )
    path = tmp_path / "operator-token"
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _run(live_server, token_file: Path, *argv: str) -> tuple[int, str, str]:
    base_url, _ = live_server
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = operator_cli.main(
            [
                "--base-url",
                f"{base_url}",
                "--token-file",
                str(token_file),
                *argv,
            ]
        )
    return code, stdout.getvalue(), stderr.getvalue()


def test_cli_intake_creates_a_pending_job(live_server, token_file) -> None:
    engine = live_server[1]
    code, out, _ = _run(
        live_server, token_file,
        "intake", "--yes",
        "--repository-id", REPO,
        "--base-sha", BASE_SHA,
        "--toolchain-ref", TOOLCHAIN,
        "--description", DESC,
    )
    assert code == 0, out
    assert "feature=" in out
    assert "state=intake" in out
    assert _row_count(engine, "features") == 1
    assert _row_count(engine, "worker_jobs") == 1


def test_cli_intake_requires_confirmation(live_server, token_file, monkeypatch) -> None:
    engine = live_server[1]
    monkeypatch.setattr("builtins.input", lambda _: "n")
    code, out, _ = _run(
        live_server, token_file,
        "intake",
        "--repository-id", REPO,
        "--base-sha", BASE_SHA,
        "--toolchain-ref", TOOLCHAIN,
        "--description", DESC,
    )
    assert code == 1
    assert "aborted" in out
    assert _row_count(engine, "features") == 0
    assert _row_count(engine, "worker_jobs") == 0


def test_cli_intake_reads_description_from_file(live_server, token_file, tmp_path) -> None:
    engine = live_server[1]
    desc_file = tmp_path / "task.md"
    desc_file.write_text(DESC, encoding="utf-8")
    code, out, _ = _run(
        live_server, token_file,
        "intake", "--yes",
        "--repository-id", REPO,
        "--base-sha", BASE_SHA,
        "--toolchain-ref", TOOLCHAIN,
        "--description-file", str(desc_file),
    )
    assert code == 0, out
    assert _row_count(engine, "features") == 1


def test_cli_intake_refuses_when_description_missing(live_server, token_file) -> None:
    """A missing description is a fail-closed argument error, not a request."""
    with pytest.raises(SystemExit) as excinfo:
        _run(
            live_server, token_file,
            "intake", "--yes",
            "--repository-id", REPO,
            "--base-sha", BASE_SHA,
            "--toolchain-ref", TOOLCHAIN,
        )
    assert "description is required" in str(excinfo.value)
