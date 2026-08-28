"""DAL-R08c: the minimal operator console CLI, exercised against the real app.

The CLI runs in-process against a live `TestClient` server (the same
composition the CLI would face over TLS), covering: whoami (decodes identity
without leaking the token), list/show/checkpoints reads, cancel with its
state binding, terminal-job refusal, token-file permission enforcement
(0600 required), and error-envelope surfacing. The token file mode check is
the same rule the worker CLI uses; a world-readable token file is a refusal.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from uvicorn import Config as UvicornConfig
from uvicorn import Server

from personal_agent_dal.service import operator_cli
from personal_agent_dal.service.app import create_app
from personal_agent_dal.service.operator_tokens import issue_operator_token
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.worker import queue

SERVICE_KEY = b"test-service-key"
ENROLLMENT_SECRET = b"test-enrollment-secret"


@pytest.fixture()
def live_server(tmp_path: Path):
    """A real uvicorn server on loopback, so the CLI exercises real HTTP."""
    engine = create_database_engine(tmp_path / "console.db")
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


def _seed(engine, *, state: str = "pending") -> str:
    job_id = queue.enqueue_job(
        engine,
        feature_id="feat-console",
        repository_id="repo-1",
        base_sha="0" * 40,
        branch_name="codex/feature-feat-console",
        toolchain_ref="toolchain/1",
    )
    if state == "leased":
        assert queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60) == job_id
    return job_id


def _run(live_server, token_file: Path, *argv: str) -> tuple[int, str, str]:
    base_url, _ = live_server
    import io
    import contextlib

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


def test_whoami_shows_identity_without_token_value(live_server, token_file, capsys) -> None:
    code, out, _ = _run(live_server, token_file, "whoami")
    assert code == 0
    assert "operator_id=henson" in out
    assert "expires_in=" in out
    token_value = token_file.read_text().strip()
    assert token_value not in out


def test_list_and_show_jobs(live_server, token_file) -> None:
    engine = live_server[1]
    job_id = _seed(engine)
    code, out, _ = _run(live_server, token_file, "list")
    assert code == 0
    assert job_id in out
    assert "total=1" in out
    code, out, _ = _run(live_server, token_file, "show", job_id)
    assert code == 0
    detail = json.loads(out)
    assert detail["state"] == "pending"


def test_cancel_pending_job_with_confirmation(live_server, token_file, monkeypatch) -> None:
    engine = live_server[1]
    job_id = _seed(engine)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    code, out, _ = _run(live_server, token_file, "cancel", job_id)
    assert code == 0
    assert queue.get_job(engine, job_id=job_id).state == "cancelled"


def test_cancel_aborted_by_user_keeps_job(live_server, token_file, monkeypatch) -> None:
    engine = live_server[1]
    job_id = _seed(engine)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    code, _, _ = _run(live_server, token_file, "cancel", job_id)
    assert code == 1
    assert queue.get_job(engine, job_id=job_id).state == "pending"


def test_cancel_terminal_job_refuses_before_request(live_server, token_file) -> None:
    engine = live_server[1]
    job_id = _seed(engine)
    assert queue.cancel_job(engine, job_id=job_id)
    with pytest.raises(SystemExit) as excinfo:
        _run(live_server, token_file, "cancel", "--yes", job_id)
    assert "terminal" in str(excinfo.value)


def test_show_unknown_job_surfaces_error_envelope(live_server, token_file) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(live_server, token_file, "show", "missing-job")
    assert "job_not_found" in str(excinfo.value)


def test_wrong_token_is_refused(live_server, token_file) -> None:
    import hashlib

    # Tamper with the signature.
    token = token_file.read_text().strip()
    token_file.write_text(token[:-4] + "0000")
    with pytest.raises(SystemExit) as excinfo:
        _run(live_server, token_file, "list")
    assert "token_invalid" in str(excinfo.value)


def test_read_capability_missing_refuses_list(live_server, token_file, tmp_path: Path) -> None:
    token = issue_operator_token(
        operator_id="henson",
        capabilities=["control"],
        expires_at_epoch=int(time.time()) + 600,
        key=SERVICE_KEY,
    )
    read_only_lacking = tmp_path / "control-only-token"
    read_only_lacking.write_text(token + "\n")
    read_only_lacking.chmod(0o600)
    with pytest.raises(SystemExit) as excinfo:
        _run(live_server, read_only_lacking, "list")
    assert "capability_missing" in str(excinfo.value)


def test_token_file_permissions_are_enforced(live_server, tmp_path: Path) -> None:
    token = issue_operator_token(
        operator_id="henson",
        capabilities=["read"],
        expires_at_epoch=int(time.time()) + 600,
        key=SERVICE_KEY,
    )
    loose = tmp_path / "loose-token"
    loose.write_text(token + "\n")
    loose.chmod(0o644)
    with pytest.raises(SystemExit) as excinfo:
        _run(live_server, loose, "list")
    assert "0600" in str(excinfo.value)
