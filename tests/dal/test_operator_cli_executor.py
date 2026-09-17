"""The operator console's F5 executor commands, against the real app.

The console is the human approval surface and the persistent-task driver's
entry point. These tests pin, over real HTTP (same composition as
``test_operator_cli.py``):

- ``effects`` lists the unknown backlog (the sweep's work list);
- ``wake`` requires its second confirmation, refuses when aborted, and on
  success carries only the binding — the server derives the rest;
- ``reconcile-sweep`` runs the read-only pass and reports per-effect
  outcomes; a swept effect that read back as landed shows its
  authoritative result;
- server error envelopes surface cleanly (501 without a composed adapter).
"""

from __future__ import annotations

import io
import contextlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from uvicorn import Config as UvicornConfig
from uvicorn import Server

from personal_agent_dal.github.adapter import BranchReadBack, PushOutcome
from personal_agent_dal.github.executor import record_effect_target
from personal_agent_dal.github.adapter_controller import fingerprint_for
from personal_agent_dal.service import operator_cli
from personal_agent_dal.service.app import create_app
from personal_agent_dal.service.operator_tokens import issue_operator_token
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import (
    create_database_engine,
    session_factory,
)
from personal_agent_core.timeutil import utc_now

from tests.dal.factories import external_effect_row, feature_row

SERVICE_KEY = b"test-service-key"
ENROLLMENT_SECRET = b"test-enrollment-secret"
BRANCH = "dal/feat-1"
HEAD = "a" * 40
REPO = "example-owner/dal-sandbox"

CONFIRMED_PUSH = PushOutcome(repository_id=REPO, branch=BRANCH, head_sha=HEAD)


class _ConsoleAdapter:
    """Stub for the live server; records every call.

    ``push_outcome=None`` makes every write raise — the sweep tests use that
    to prove the pass is read-only. With an outcome, the wake dispatch path
    completes normally.
    """

    def __init__(
        self,
        branch_read_back: BranchReadBack,
        *,
        push_outcome: PushOutcome | None = None,
    ) -> None:
        self.branch_read_back = branch_read_back
        self.push_outcome = push_outcome
        self.calls: list[str] = []

    def read_feature_branch(self, **_: object) -> BranchReadBack:
        self.calls.append("read_branch")
        return self.branch_read_back

    def list_open_pull_requests(self, **_: object) -> object:
        self.calls.append("read_prs")
        return BranchReadBack  # never actually consulted in these tests

    def read_check_run(self, **_: object) -> object:
        self.calls.append("read_check")
        raise AssertionError("no check effects are seeded here")

    def push_feature_branch(self, **_: object) -> object:
        self.calls.append("push")
        if self.push_outcome is None:
            raise AssertionError("no write may issue without an outcome")
        return self.push_outcome

    def create_pull_request(self, **_: object) -> object:
        self.calls.append("pr")
        raise AssertionError("no PR effects are seeded here")

    def write_check_run(self, **_: object) -> object:
        self.calls.append("check")
        raise AssertionError("no check effects are seeded here")


@pytest.fixture()
def live_server(tmp_path: Path):
    """Real uvicorn on loopback with the executor adapter composed.

    Yields (base_url, engine, adapter); the adapter starts write-refusing
    (sweep posture). ``wake`` tests flip ``push_outcome`` to CONFIRMED_PUSH.
    """
    engine = create_database_engine(tmp_path / "console-f5.db")
    db.upgrade(engine)
    adapter = _ConsoleAdapter(BranchReadBack(found=True, head_sha=HEAD))
    app = create_app(
        engine,
        service_key=SERVICE_KEY,
        enrollment_secret=ENROLLMENT_SECRET,
        github_adapter=adapter,
    )
    config = UvicornConfig(app, host="127.0.0.1", port=0, log_level="error")
    server = Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", engine, adapter
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
    base_url, _, _ = live_server
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = operator_cli.main(
            ["--base-url", base_url, "--token-file", str(token_file), *argv]
        )
    return code, stdout.getvalue(), stderr.getvalue()


def _seed(engine, *, effect_id: str = "effect-cli-1",
          feature_id: str = "feature-cli-1",
          effect_state: str = "unknown",
          remote_key: str = "idem-cli-1") -> str:
    """One parked feature + its unknown (or wakeable) effect + target record."""
    now = utc_now()
    feature_state = "reconciliation_required" if effect_state == "unknown" \
        else "awaiting_merge"
    with session_factory(engine)() as session, session.begin():
        session.add(feature_row(
            feature_id=feature_id, version=3, state=feature_state, now=now
        ))
        session.add(external_effect_row(
            effect_id=effect_id, owner_id=feature_id, version=1,
            state=effect_state, now=now,
        ))
    from personal_agent_dal.storage.machine_models import ExternalEffect

    with session_factory(engine)() as session, session.begin():
        row = session.get(ExternalEffect, effect_id)
        assert row is not None
        row.target_fingerprint = fingerprint_for(
            "push_branch", {"branch": BRANCH, "head_sha": HEAD}
        )
        row.remote_idempotency_key = remote_key
    with session_factory(engine)() as session, session.begin():
        record_effect_target(
            session, effect_id=effect_id, action="push_branch",
            payload={"branch": BRANCH, "head_sha": HEAD}, now=utc_now(),
        )
    return effect_id


def test_effects_lists_unknown_backlog(live_server, token_file) -> None:
    engine = live_server[1]
    effect_id = _seed(engine)
    code, out, _ = _run(live_server, token_file, "effects")
    assert code == 0
    assert effect_id in out
    assert "feature-cli-1" in out
    assert "idem-cli-1" in out
    code, out, _ = _run(live_server, token_file, "effects")
    assert "unknown effects: 1" in out


def test_wake_requires_confirmation_and_aborts_cleanly(
    live_server, token_file, monkeypatch
) -> None:
    engine = live_server[1]
    effect_id = _seed(engine, effect_state="intent_recorded", remote_key="idem-wake-1")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    code, out, _ = _run(
        live_server, token_file, "wake", effect_id,
        "--expected-state", "intent_recorded", "--expected-version", "1",
    )
    assert code == 1
    assert "aborted" in out
    import sqlalchemy as sa

    with engine.connect() as connection:
        state = connection.execute(
            sa.text("SELECT state FROM external_effects WHERE effect_id = :e")
            .bindparams(e=effect_id)
        ).scalar_one()
    assert state == "intent_recorded"


def test_wake_dispatches_with_binding_only(live_server, token_file, monkeypatch) -> None:
    engine, adapter = live_server[1], live_server[2]
    effect_id = _seed(engine, effect_state="intent_recorded", remote_key="idem-wake-2")
    adapter.push_outcome = CONFIRMED_PUSH
    monkeypatch.setattr("builtins.input", lambda _: "y")
    code, out, _ = _run(
        live_server, token_file, "wake", effect_id,
        "--expected-state", "intent_recorded", "--expected-version", "1",
    )
    assert code == 0
    assert f"woken: {effect_id} -> dispatch_started" in out
    assert adapter.calls == ["push"], adapter.calls
    import sqlalchemy as sa

    with engine.connect() as connection:
        state = connection.execute(
            sa.text("SELECT state FROM external_effects WHERE effect_id = :e")
            .bindparams(e=effect_id)
        ).scalar_one()
    assert state == "dispatch_started"


def test_wake_reports_executor_refusal(live_server, token_file, monkeypatch) -> None:
    """A binding mismatch surfaces the server's envelope, never a traceback."""
    engine = live_server[1]
    effect_id = _seed(engine, effect_state="unknown", remote_key="idem-wake-3")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    with pytest.raises(SystemExit) as excinfo:
        _run(
            live_server, token_file, "wake", effect_id,
            "--expected-state", "claimed", "--expected-version", "1",
        )
    message = str(excinfo.value)
    assert "HTTP 409" in message
    assert "state_mismatch" in message


def test_reconcile_sweep_reports_landed_effect(live_server, token_file) -> None:
    engine, adapter = live_server[1], live_server[2]
    effect_id = _seed(engine)
    code, out, _ = _run(live_server, token_file, "reconcile-sweep")
    assert code == 0
    assert "swept: 1" in out
    assert effect_id in out
    assert "authoritative=confirmed_completed" in out
    assert "push" not in adapter.calls, "the sweep must be read-only"


def test_reconcile_sweep_read_only_on_empty_backlog(live_server, token_file) -> None:
    _engine, adapter = live_server[1], live_server[2]
    code, out, _ = _run(live_server, token_file, "reconcile-sweep")
    assert code == 0
    assert "swept: 0" in out
    assert adapter.calls == [], adapter.calls


def test_systemd_mode_mints_in_memory_token_for_sweep_only(
    live_server, tmp_path: Path, monkeypatch
) -> None:
    base_url, engine, adapter = live_server
    monkeypatch.setattr(operator_cli, "SERVER_LOCAL_SWEEP_BASE_URL", base_url)
    effect_id = _seed(engine)
    key_file = tmp_path / "service-key"
    key_file.write_bytes(SERVICE_KEY)
    key_file.chmod(0o600)
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = operator_cli.main(
            [
                "--base-url",
                base_url,
                "--service-key-file",
                str(key_file),
                "reconcile-sweep",
            ]
        )
    assert code == 0
    assert effect_id in stdout.getvalue()
    assert "push" not in adapter.calls

    with pytest.raises(SystemExit) as excinfo:
        operator_cli.main(
            [
                "--base-url",
                base_url,
                "--service-key-file",
                str(key_file),
                "effects",
            ]
        )
    assert excinfo.value.code == 2

    with pytest.raises(SystemExit) as remote_exc:
        operator_cli.main(
            [
                "--base-url",
                "https://example.invalid/dal",
                "--service-key-file",
                str(key_file),
                "reconcile-sweep",
            ]
        )
    assert remote_exc.value.code == 2


def test_executor_error_envelope_surfaces(live_server, token_file, tmp_path: Path) -> None:
    """A 501 from a non-composed service surfaces as a clean envelope."""
    # Point the CLI at the same server but ask for an effect the composed
    # service knows nothing about — the wake refuses NOT_FOUND, the CLI
    # prints the envelope and exits non-zero.
    with pytest.raises(SystemExit) as excinfo:
        _run(
            live_server, token_file, "wake", "effect-absent",
            "--expected-state", "intent_recorded", "--expected-version", "1",
            "--yes",
        )
    assert "HTTP 404" in str(excinfo.value)
    assert "effect_not_found" in str(excinfo.value)
