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


def _feature_id_of(description: str, repo: str, base_sha: str) -> str:
    """The content-addressed feature id the intake core derives."""
    from personal_agent_dal.service.intake import _feature_id

    return _feature_id(repo, description, base_sha)


def _create_feature_at_intake(engine, feature_id: str) -> None:
    """Drive the real SM-CREATE transition for one feature (no job)."""
    from personal_agent_dal.machine.engine import TransitionCommand, apply_transition
    from personal_agent_dal.machine.transition_types import ReceiptCodes

    command = TransitionCommand(
        aggregate_type="feature",
        aggregate_id=feature_id,
        command_type="create_feature",
        command_parameters={"effect_outcome": None, "target_state": "intake"},
        actor_type="service",
        evidence_source_types=("workflow-service",),
        evidence_schema_versions=("dal.evidence.feature/1.0",),
        decision_action=None,
        reason_code=None,
        expected_version=None,
        idempotency_key=f"intake:{feature_id}",
        evidence_documents=(),
    )
    outcome = apply_transition(engine, command)
    assert outcome.receipt_code == ReceiptCodes.APPLIED, outcome.receipt_code


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
    # F7: the persisted intake body is rewritten alongside the re-enqueue.
    with engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT task_description, task_description_sha256, toolchain_ref "
                "FROM feature_intake_requests WHERE intake_key = :k"
            ).bindparams(k=f"intake:{second.feature_id}")
        ).one()
        assert row.task_description == DESC
        assert row.task_description_sha256 == hashlib.sha256(
            DESC.encode("utf-8")
        ).hexdigest()
        assert row.toolchain_ref == TOOLCHAIN


def test_concurrent_identical_intakes_create_exactly_one_job(
    engine, monkeypatch
) -> None:
    """F4 (2026-09-07 review): the find-or-create must survive a real race.

    The old code's lookup (`_existing_job_id`) and insert ran in separate
    transactions with a fresh `job_id` per insert, so two concurrent identical
    intakes both read "no job" and both enqueued. Here both threads pass the
    feature-creation step before either enqueues (a barrier at the enqueue
    boundary); the unique intake key must arbitrate — exactly one job row,
    both callers receive the same job_id.
    """
    import personal_agent_dal.worker.queue as queue_module

    barrier = threading.Barrier(2)
    real_enqueue = queue_module.enqueue_job

    def synchronized_enqueue(engine_arg, **kwargs):
        barrier.wait(timeout=10)
        return real_enqueue(engine_arg, **kwargs)

    # The intake module imported enqueue_job by name; patch both the module
    # attribute the core reads and the queue function itself.
    monkeypatch.setattr(
        "personal_agent_dal.service.intake.enqueue_job", synchronized_enqueue
    )

    results: list = []
    errors: list = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            outcome = intake_task(
                engine, task_description=DESC, repository_id=REPO,
                base_sha=BASE_SHA, toolchain_ref=TOOLCHAIN,
            )
            with lock:
                results.append(outcome)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads), "intake race threads hung"

    assert not errors, [type(e).__name__ for e in errors]
    assert len(results) == 2
    assert _row_count(engine, "features") == 1
    assert _row_count(engine, "worker_jobs") == 1
    job_ids = {outcome.job_id for outcome in results}
    assert len(job_ids) == 1, [outcome.job_id for outcome in results]
    # Both callers see the same feature too (the transition's replay path).
    assert len({outcome.feature_id for outcome in results}) == 1


def test_intake_refuses_a_conflicting_toolchain(engine) -> None:
    """F4/F7: the same task re-submitted under a different toolchain must refuse.

    `toolchain_ref` does not participate in the feature identity (the feature
    is the task, not the toolchain), so the old code silently returned the old
    job with the old toolchain — the operator's explicit change expressed
    nothing. The refusal is typed; the worker-side `toolchain_ref_mismatch`
    fence stays as the second line of defence.
    """
    first = intake_task(
        engine, task_description=DESC, repository_id=REPO,
        base_sha=BASE_SHA, toolchain_ref=TOOLCHAIN,
    )
    assert first.job_id

    with pytest.raises(IntakeRefusal) as exc:
        intake_task(
            engine, task_description=DESC, repository_id=REPO,
            base_sha=BASE_SHA, toolchain_ref=".personal-agent/other-toolchain.json",
        )
    assert exc.value.code == "toolchain_conflict"
    # The refusal wrote nothing: one feature, one job, unchanged toolchain.
    assert _row_count(engine, "features") == 1
    assert _row_count(engine, "worker_jobs") == 1
    job = queue.get_job(engine, job_id=first.job_id)
    assert job is not None and job.toolchain_ref == TOOLCHAIN


def test_intake_replay_dedupes_across_the_0010_migration(tmp_path) -> None:
    """R2-3 (round-2 review): a pre-0010 job must deduplicate a post-migration
    replay of the same intake.

    The old producer enqueued without an intake_key; a migration that left
    those rows unstamped made the new key-based find-or-create see "no job"
    and enqueue a second one for the same feature. The migration backfills
    each feature's oldest job with f"intake:{feature_id}", so the replay
    converges on the original job.
    """
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_database_engine
    from personal_agent_dal.worker.queue import enqueue_job as old_enqueue

    old_engine = create_database_engine(tmp_path / "old-world.db")
    db.upgrade(old_engine, "0009")
    # The pre-0010 producer's shape: create the feature via the real
    # transition, then enqueue without any intake identity.
    feature_id = _feature_id_of(DESC, REPO, BASE_SHA)
    _create_feature_at_intake(old_engine, feature_id)
    old_job = old_enqueue(
        old_engine,
        feature_id=feature_id, repository_id=REPO, base_sha=BASE_SHA,
        branch_name=f"codex/feature-{feature_id}", toolchain_ref=TOOLCHAIN,
    )
    with old_engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT count(*) FROM worker_jobs")
        ).scalar_one() == 1
        # Prove the old-world row has no intake_key (the column does not
        # exist yet at 0009 — that is the point).
        columns = {
            row[1] for row in connection.execute(
                sa.text("PRAGMA table_info(worker_jobs)")
            )
        }
    assert "intake_key" not in columns
    old_engine.dispose()

    # The migration to 0010 backfills the oldest job's identity.
    migrated_engine = create_database_engine(tmp_path / "old-world.db")
    db.upgrade(migrated_engine)
    replay = intake_task(
        migrated_engine, task_description=DESC, repository_id=REPO,
        base_sha=BASE_SHA, toolchain_ref=TOOLCHAIN,
    )
    assert replay.duplicate
    assert replay.job_id == old_job, "the replay must converge on the old job"
    assert _row_count(migrated_engine, "worker_jobs") == 1
    assert _row_count(migrated_engine, "features") == 1
    migrated_engine.dispose()


def test_migration_backfill_stamps_only_the_oldest_job(tmp_path) -> None:
    """R2-3: a feature with several pre-0010 jobs keeps the extras outside the
    intake idempotency set — only the oldest carries the intake identity, so
    the multi-phase semantics (a second job is a rerun, not a replay) hold."""
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_database_engine
    from personal_agent_dal.worker.queue import enqueue_job as old_enqueue

    engine = create_database_engine(tmp_path / "multi.db")
    db.upgrade(engine, "0009")
    feature_id = _feature_id_of(DESC, REPO, BASE_SHA)
    _create_feature_at_intake(engine, feature_id)
    first = old_enqueue(
        engine, feature_id=feature_id, repository_id=REPO, base_sha=BASE_SHA,
        branch_name=f"codex/feature-{feature_id}", toolchain_ref=TOOLCHAIN,
    )
    second = old_enqueue(
        engine, feature_id=feature_id, repository_id=REPO, base_sha=BASE_SHA,
        branch_name=f"codex/feature-{feature_id}", toolchain_ref=TOOLCHAIN,
    )
    db.upgrade(engine)

    with engine.connect() as connection:
        keys = dict(
            connection.execute(
                sa.text("SELECT job_id, intake_key FROM worker_jobs")
            ).all()
        )
    assert keys[first] == f"intake:{feature_id}", "the oldest job is stamped"
    assert keys[second] is None, "the later job stays outside the intake set"
    engine.dispose()


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
