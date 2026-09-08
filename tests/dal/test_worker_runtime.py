"""DAL-016/017/019/020: the runnable Home Mac Worker runtime.

The G2 frozen gate closed on the *pure policy* half (the four worker-isolation
guards and the lease/epoch decisions). This suite exercises the execution half
the policy governs, against a real SQLite database built by the migration chain
(so migration `0005` is exercised, not just `create_all`) and a real synthetic
git repository (so `worktree add` / toolchain / checkpoint / receipt are real,
not faked):

- the durable queue primitives — CAS claim, heartbeat, expiry reclaim with an
  attempt budget, and idempotent result receipts;
- the deterministic toolchain registry and executor — closed schema, bounded
  timeout, commands only from the pinned manifest;
- the checkpoint/handoff bundle — atomic write and lossless load;
- the full `run_poll_once` cycle — success, toolchain failure, and an
  allowlist refusal, each with the terminal state and receipt it must leave.
"""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.worker import checkpoint as checkpoint_mod
from personal_agent_dal.worker import queue
from personal_agent_dal.worker import toolchain
from personal_agent_dal.worker.checkpoint import CheckpointBundle
from personal_agent_dal.worker.config import (
    LocalTransportConfig,
    RepoAllowlistEntry,
    WorkerConfig,
)
from personal_agent_dal.worker.coder_launcher import CoderRunResult
from personal_agent_dal.worker.poll_once import run_poll_once
from personal_agent_dal.worker.transport import LocalSQLiteAdapter
from personal_agent_dal.worker.toolchain import (
    TIMEOUT_RETURNCODE,
    load_toolchain_manifest,
    execute_toolchain,
    run_sandboxed_command,
)

BASE_SHA = "0" * 40


@pytest.fixture()
def engine(tmp_path: Path):
    eng = create_database_engine(tmp_path / "worker.db")
    db.upgrade(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        worker_id="test-worker",
        transport=LocalTransportConfig(database_path=tmp_path / "worker.db"),
        worktree_root=tmp_path / "worktrees",
        checkpoint_root=tmp_path / "checkpoints",
        kill_switch_path=tmp_path / "worker.disabled",
        lease_ttl_seconds=60,
        max_attempts=3,
        repos={
            "synthetic": RepoAllowlistEntry(
                repository_id="synthetic", local_path=str(tmp_path / "repo")
            )
        },
    )



def _poll(engine, config: WorkerConfig):
    """Run one cycle through the local adapter, as the worker CLI composes it."""
    return run_poll_once(
        LocalSQLiteAdapter(
            engine,
            worker_id=config.worker_id,
            lease_ttl_seconds=config.lease_ttl_seconds,
            max_attempts=config.max_attempts,
            checkpoint_root=config.checkpoint_root,
        ),
        config,
    )


def _enqueue(engine, *, feature_id="feat-1", repository_id="synthetic", now=None) -> str:
    return queue.enqueue_job(
        engine,
        feature_id=feature_id,
        repository_id=repository_id,
        base_sha=BASE_SHA,
        branch_name=f"codex/feature-{feature_id}",
        toolchain_ref=".personal-agent/toolchain.json",
        now=now,
    )


def _count(engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


# --- queue primitives -------------------------------------------------------


def test_claim_is_cas_two_workers_one_winner(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)
    barrier = threading.Barrier(2)

    def claim(worker_id: str):
        barrier.wait()
        return queue.claim_job(
            engine, worker_id=worker_id, lease_ttl_seconds=60, now=now
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("w1", "w2")))

    assert sorted(result is None for result in results) == [False, True]
    assert job_id in results
    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "leased"
    assert record.worker_id in ("w1", "w2")
    assert record.lease_epoch == 1


def test_worker_queue_migration_downgrade_and_upgrade(engine) -> None:
    assert inspect(engine).has_table("worker_jobs")
    assert inspect(engine).has_table("worker_result_receipts")

    db.downgrade(engine, "0004")
    assert not inspect(engine).has_table("worker_jobs")
    assert not inspect(engine).has_table("worker_result_receipts")

    db.upgrade(engine)
    assert inspect(engine).has_table("worker_jobs")
    assert inspect(engine).has_table("worker_result_receipts")


def test_claim_orders_oldest_pending_first(engine) -> None:
    now = utc_now()
    older = _enqueue(engine, feature_id="feat-older", now=now)
    newer = _enqueue(engine, feature_id="feat-newer", now=now + timedelta(seconds=1))

    first = queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60, now=now)
    second = queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60, now=now)

    assert first == older
    assert second == newer


def test_heartbeat_refreshes_lease(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)
    queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60, now=now)

    before = queue.get_job(engine, job_id=job_id).lease_expires_at
    epoch = queue.get_job(engine, job_id=job_id).lease_epoch
    ok = queue.heartbeat(
        engine,
        job_id=job_id,
        worker_id="w1",
        lease_epoch=epoch,
        lease_ttl_seconds=60,
        now=now + timedelta(seconds=30),
    )
    after = queue.get_job(engine, job_id=job_id).lease_expires_at

    assert ok
    assert after > before


def test_heartbeat_rejected_after_losing_lease(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)
    queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=10, now=now)
    old_epoch = queue.get_job(engine, job_id=job_id).lease_epoch
    queue.reclaim_expired(engine, max_attempts=3, now=now + timedelta(seconds=30))
    queue.claim_job(engine, worker_id="w2", lease_ttl_seconds=60, now=now + timedelta(seconds=30))

    ok = queue.heartbeat(
        engine,
        job_id=job_id,
        worker_id="w1",
        lease_epoch=old_epoch,
        lease_ttl_seconds=60,
        now=now,
    )
    assert not ok


def test_reclaim_expired_requeues_then_expires(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)
    queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=10, now=now)

    # attempt 1 expired -> back to pending, attempt_count becomes 1
    reclaimed = queue.reclaim_expired(engine, max_attempts=3, now=now + timedelta(seconds=20))
    assert reclaimed == [job_id]
    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "pending"
    assert record.attempt_count == 1
    assert record.worker_id is None
    assert record.lease_epoch == 1

    queue.claim_job(engine, worker_id="w2", lease_ttl_seconds=10, now=now + timedelta(seconds=20))
    # attempt 2 expired -> attempt_count 2 (still < 3, requeue)
    queue.reclaim_expired(engine, max_attempts=3, now=now + timedelta(seconds=40))
    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "pending"
    assert record.attempt_count == 2

    queue.claim_job(engine, worker_id="w3", lease_ttl_seconds=10, now=now + timedelta(seconds=40))
    # attempt 3 expired -> budget exhausted (3 >= 3), terminal expired
    queue.reclaim_expired(engine, max_attempts=3, now=now + timedelta(seconds=60))
    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "expired"
    assert record.attempt_count == 3


def test_result_receipt_is_idempotent(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)

    queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60, now=now)
    epoch = queue.get_job(engine, job_id=job_id).lease_epoch
    first = queue.finish_job(
        engine,
        job_id=job_id,
        worker_id="w1",
        lease_epoch=epoch,
        state="succeeded",
        result_sha256="a" * 64,
        now=now,
    )
    second = queue.finish_job(
        engine,
        job_id=job_id,
        worker_id="w1",
        lease_epoch=epoch,
        state="succeeded",
        result_sha256="a" * 64,
        now=now,
    )

    assert first == second
    assert _count(engine, "worker_result_receipts") == 1


def test_finish_job_moves_owned_job_to_terminal(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)
    queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60, now=now)
    epoch = queue.get_job(engine, job_id=job_id).lease_epoch

    ok = queue.finish_job(
        engine, job_id=job_id, worker_id="w1", lease_epoch=epoch, state="succeeded",
        result_sha256="b" * 64, now=now,
    )
    assert ok
    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "succeeded"
    assert record.result_sha256 == "b" * 64


def test_finish_job_rejected_by_another_worker(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)
    queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60, now=now)
    epoch = queue.get_job(engine, job_id=job_id).lease_epoch

    ok = queue.finish_job(
        engine,
        job_id=job_id,
        worker_id="w2",
        lease_epoch=epoch,
        state="succeeded",
        now=now,
    )
    assert not ok
    assert queue.get_job(engine, job_id=job_id).state == "leased"


def test_reclaimed_lease_epoch_fences_same_worker_id(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)
    queue.claim_job(engine, worker_id="stable-worker", lease_ttl_seconds=1, now=now)
    old_epoch = queue.get_job(engine, job_id=job_id).lease_epoch
    queue.reclaim_expired(engine, max_attempts=3, now=now + timedelta(seconds=2))
    queue.claim_job(
        engine,
        worker_id="stable-worker",
        lease_ttl_seconds=60,
        now=now + timedelta(seconds=2),
    )
    replacement = queue.get_job(engine, job_id=job_id)

    assert replacement.lease_epoch == old_epoch + 1
    assert not queue.finish_job(
        engine,
        job_id=job_id,
        worker_id="stable-worker",
        lease_epoch=old_epoch,
        state="succeeded",
        result_sha256="a" * 64,
        now=now + timedelta(seconds=3),
    )
    assert queue.get_job(engine, job_id=job_id).state == "leased"
    assert _count(engine, "worker_result_receipts") == 0


def test_conflicting_result_is_not_idempotent(engine) -> None:
    now = utc_now()
    job_id = _enqueue(engine, now=now)
    queue.claim_job(engine, worker_id="w1", lease_ttl_seconds=60, now=now)
    epoch = queue.get_job(engine, job_id=job_id).lease_epoch
    assert queue.finish_job(
        engine,
        job_id=job_id,
        worker_id="w1",
        lease_epoch=epoch,
        state="succeeded",
        result_sha256="a" * 64,
        now=now,
    )
    with pytest.raises(queue.ResultConflictError):
        queue.finish_job(
            engine,
            job_id=job_id,
            worker_id="w1",
            lease_epoch=epoch,
            state="succeeded",
            result_sha256="b" * 64,
            now=now,
        )
    assert queue.get_job(engine, job_id=job_id).result_sha256 == "a" * 64
    assert _count(engine, "worker_result_receipts") == 1


# --- toolchain registry + executor ------------------------------------------


def _write_manifest(
    repo: Path,
    *,
    format_cmd=("true",),
    test_cmd=("true",),
    fixture: bool = False,
    fixture_path: str = "README.md",
    template: str = "fixture {feature_id}\n",
    coder: bool = False,
    coder_allowed_paths=("README.md",),
) -> None:
    body = {
        "schema_version": "dal.toolchain-manifest/1.0",
        "stages": {
            "format": {"cmd": list(format_cmd)},
            "lint": {"cmd": ["true"]},
            "build": {"cmd": ["true"]},
            "test": {"cmd": list(test_cmd)},
        },
    }
    if fixture:
        body["fixture_coder"] = {"path": fixture_path, "template": template}
        body["registry"] = {
            "diff": ["git", "diff", "--binary", "--no-ext-diff", "HEAD"]
        }
    if coder:
        body["coder"] = {
            "model_alias": "dal-ccr-primary",
            "allowed_tools": ["Read", "Edit", "Bash"],
            "max_turns": 8,
            "max_wall_seconds": 900,
            "max_patch_bytes": 1048576,
            "prompt": "In {feature_id}, add one tracked change.",
            "allowed_paths": list(coder_allowed_paths),
            "pinned_endpoint": "127.0.0.1:3456",
        }
        body["registry"] = {
            "diff": ["git", "diff", "--binary", "--no-ext-diff", "HEAD"]
        }
    (repo / ".personal-agent").mkdir(parents=True, exist_ok=True)
    (repo / ".personal-agent" / "toolchain.json").write_text(json.dumps(body))


def test_manifest_schema_is_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".personal-agent").mkdir()
    manifest_path = repo / ".personal-agent" / "toolchain.json"

    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "dal.toolchain-manifest/1.0",
                "stages": {
                    "format": {"cmd": ["true"]},
                    "lint": {"cmd": ["true"]},
                    "build": {"cmd": ["true"]},
                    "test": {"cmd": ["true"]},
                    "extra": {"cmd": ["true"]},
                },
            }
        )
    )
    with pytest.raises(ValueError):
        load_toolchain_manifest(repo)

    # a missing stage is an error, not silently skipped
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "dal.toolchain-manifest/1.0",
                "stages": {"format": {"cmd": ["true"]}},
            }
        )
    )
    with pytest.raises(ValueError):
        load_toolchain_manifest(repo)

    # a non-string argv is an error
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "dal.toolchain-manifest/1.0",
                "stages": {
                    "format": {"cmd": [1]},
                    "lint": {"cmd": ["true"]},
                    "build": {"cmd": ["true"]},
                    "test": {"cmd": ["true"]},
                },
            }
        )
    )
    with pytest.raises(ValueError):
        load_toolchain_manifest(repo)


def test_manifest_timeout_is_clamped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".personal-agent").mkdir()
    (repo / ".personal-agent" / "toolchain.json").write_text(
        json.dumps(
            {
                "schema_version": "dal.toolchain-manifest/1.0",
                "stages": {
                    "format": {"cmd": ["true"], "timeout_s": 999999},
                    "lint": {"cmd": ["true"]},
                    "build": {"cmd": ["true"]},
                    "test": {"cmd": ["true"]},
                },
            }
        )
    )
    manifest = load_toolchain_manifest(repo)
    assert manifest.stages["format"].timeout_s == 600.0


def test_execute_toolchain_times_out(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_manifest(repo, test_cmd=("sleep", "5"))
    (repo / ".personal-agent" / "toolchain.json").write_text(
        json.dumps(
            {
                "schema_version": "dal.toolchain-manifest/1.0",
                "stages": {
                    "format": {"cmd": ["true"]},
                    "lint": {"cmd": ["true"]},
                    "build": {"cmd": ["true"]},
                    "test": {"cmd": ["sleep", "5"], "timeout_s": 1},
                },
            }
        )
    )
    manifest = load_toolchain_manifest(repo)
    result = execute_toolchain(repo, manifest)
    assert result.stages[3].returncode == TIMEOUT_RETURNCODE
    assert not result.succeeded


def test_run_sandboxed_command_bounds_output_and_clears_env(
    tmp_path: Path, monkeypatch,
) -> None:
    """The diff-capture primitive (P2-5) runs under the same sandbox,
    credential-free env and output bound as toolchain stages."""
    repo = tmp_path / "repo"
    repo.mkdir()

    output, code = run_sandboxed_command(
        ("/usr/bin/python3", "-c", "print('x' * 200_000)"), repo
    )
    assert code == 0
    assert "[toolchain: output truncated]" in output
    assert len(output) < 70_000

    monkeypatch.setenv("DAL_RUNTIME_CANARY", "secret")
    output2, code2 = run_sandboxed_command(
        ("/usr/bin/python3", "-c",
         "import os; print('leaked' if os.getenv('DAL_RUNTIME_CANARY') else 'clean')"),
        repo,
    )
    assert code2 == 0
    assert "clean" in output2


def test_toolchain_child_gets_closed_environment(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DAL_REVIEW_CANARY", "must-not-cross")
    command = (
        "/usr/bin/python3",
        "-c",
        "import os,sys; sys.exit(0 if os.getenv('DAL_REVIEW_CANARY') is None else 9)",
    )
    _write_manifest(repo, test_cmd=command)
    result = execute_toolchain(repo, load_toolchain_manifest(repo))
    assert result.succeeded


def test_toolchain_child_cannot_read_or_write_login_user_sibling(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    canary = tmp_path / "login-user-canary"
    canary.write_text("must-stay-private")
    canary.chmod(0o600)
    probe = (
        "/usr/bin/python3",
        "-c",
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "read_denied=write_denied=False; "
        "\ntry: p.read_text()"
        "\nexcept PermissionError: read_denied=True"
        "\ntry: p.write_text('sandbox-crossed')"
        "\nexcept PermissionError: write_denied=True"
        "\nraise SystemExit(0 if read_denied and write_denied else 9)",
        str(canary),
    )
    _write_manifest(repo, test_cmd=probe)

    result = execute_toolchain(repo, load_toolchain_manifest(repo))

    assert result.succeeded
    assert canary.read_text() == "must-stay-private"


def test_toolchain_child_cannot_stat_credential_subtree(
    tmp_path: Path, monkeypatch
) -> None:
    """Metadata (existence/size/mtime) is denied on credential subtrees while the
    worktree stays stat-able.

    The ``file-read-metadata`` grant is broad because the ``/usr/bin/python3``
    shim requires it, so the profile compensates with a targeted ``deny`` on the
    login user's credential home subtrees (Keychain/SSH/cloud dirs). This test
    exercises that deny list in isolation: the secret canary may not be stat'ed,
    but the worktree it is meant to build in still resolves.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    canary = secrets / "key"
    canary.write_text("must-stay-private")
    canary.chmod(0o600)
    monkeypatch.setattr(toolchain, "METADATA_DENY_PATHS", (secrets,))
    probe = (
        "/usr/bin/python3",
        "-c",
        "import os,sys; secret_denied=repo_ok=False; "
        "\ntry: os.stat(sys.argv[1])"
        "\nexcept PermissionError: secret_denied=True"
        "\ntry: os.stat(sys.argv[2])"
        "\nexcept PermissionError: pass"
        "\nelse: repo_ok=True"
        "\nraise SystemExit(0 if secret_denied and repo_ok else 9)",
        str(canary),
        str(repo),
    )
    _write_manifest(repo, test_cmd=probe)

    result = execute_toolchain(repo, load_toolchain_manifest(repo))

    assert result.succeeded
    assert canary.read_text() == "must-stay-private"


def test_toolchain_child_cannot_escape_through_worktree_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    canary = tmp_path / "symlink-canary"
    canary.write_text("must-stay-private")
    (repo / "escape").symlink_to(canary)
    probe = (
        "/usr/bin/python3",
        "-c",
        "import pathlib; p=pathlib.Path('escape'); read_denied=write_denied=False; "
        "\ntry: p.read_text()"
        "\nexcept PermissionError: read_denied=True"
        "\ntry: p.write_text('sandbox-crossed')"
        "\nexcept PermissionError: write_denied=True"
        "\nraise SystemExit(0 if read_denied and write_denied else 9)",
    )
    _write_manifest(repo, test_cmd=probe)

    result = execute_toolchain(repo, load_toolchain_manifest(repo))

    assert result.succeeded
    assert canary.read_text() == "must-stay-private"


def test_toolchain_child_can_write_current_worktree_and_private_tmp(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    probe = (
        "/usr/bin/python3",
        "-c",
        "import os,pathlib,tempfile; "
        "pathlib.Path('tracked-output').write_text('inside'); "
        "p=pathlib.Path(tempfile.gettempdir())/'stage-output'; p.write_text('temp'); "
        "assert pathlib.Path(os.environ['TMPDIR']) == pathlib.Path(tempfile.gettempdir())",
    )
    _write_manifest(repo, test_cmd=probe)

    result = execute_toolchain(repo, load_toolchain_manifest(repo))

    assert result.succeeded
    assert (repo / "tracked-output").read_text() == "inside"


def test_timeout_kills_descendant_process_group(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = repo / "escaped-child"
    child = f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('escaped')"
    parent = (
        "/usr/bin/python3",
        "-c",
        "import subprocess,time; "
        f"subprocess.Popen(['/usr/bin/python3','-c',{child!r}]); time.sleep(10)",
    )
    (repo / ".personal-agent").mkdir()
    (repo / ".personal-agent" / "toolchain.json").write_text(
        json.dumps(
            {
                "schema_version": "dal.toolchain-manifest/1.0",
                "stages": {
                    "format": {"cmd": list(parent), "timeout_s": 0.2},
                    "lint": {"cmd": ["true"]},
                    "build": {"cmd": ["true"]},
                    "test": {"cmd": ["true"]},
                },
            }
        )
    )
    result = execute_toolchain(repo, load_toolchain_manifest(repo))
    assert result.stages[0].returncode == TIMEOUT_RETURNCODE
    time.sleep(1.2)
    assert not marker.exists()


def test_toolchain_network_is_denied_by_runtime_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script = repo / "network_probe.py"
    script.write_text(
        "import errno, socket, sys\n"
        "try:\n"
        "    socket.socket().bind(('127.0.0.1', 0))\n"
        "except OSError as error:\n"
        "    raise SystemExit(0 if error.errno in (errno.EPERM, errno.EACCES) else 2)\n"
        "raise SystemExit(3)\n"
    )
    _write_manifest(repo, test_cmd=("/usr/bin/python3", str(script)))
    result = execute_toolchain(repo, load_toolchain_manifest(repo))
    assert result.succeeded


def test_toolchain_cannot_read_supervisor_control_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    supervisor_db = tmp_path / "worker.db"
    supervisor_db.write_text("supervisor-only")
    probe = (
        "/usr/bin/python3",
        "-c",
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "\ntry: p.read_text()"
        "\nexcept PermissionError: raise SystemExit(0)"
        "\nraise SystemExit(3)",
        str(supervisor_db),
    )
    _write_manifest(repo, test_cmd=probe)
    result = execute_toolchain(
        repo,
        load_toolchain_manifest(repo),
        forbidden_paths=(supervisor_db,),
    )
    assert result.succeeded


def test_toolchain_cannot_write_read_only_repo_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    protected = tmp_path / "main-repo"
    protected.mkdir()
    probe = (
        "/usr/bin/python3",
        "-c",
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "\ntry: (p/'mutated').write_text('bad')"
        "\nexcept PermissionError: raise SystemExit(0)"
        "\nraise SystemExit(3)",
        str(protected),
    )
    _write_manifest(repo, test_cmd=probe)
    result = execute_toolchain(
        repo,
        load_toolchain_manifest(repo),
        read_only_paths=(protected,),
    )
    assert result.succeeded
    assert not (protected / "mutated").exists()


def test_toolchain_cannot_overwrite_current_worktree_git_marker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git_marker = repo / ".git"
    git_marker.write_text("gitdir: protected\n")
    probe = (
        "/usr/bin/python3",
        "-c",
        "import pathlib,sys; p=pathlib.Path('.git'); "
        "\ntry: p.write_text('mutated')"
        "\nexcept PermissionError: raise SystemExit(0)"
        "\nraise SystemExit(9)",
    )
    _write_manifest(repo, test_cmd=probe)

    result = execute_toolchain(
        repo,
        load_toolchain_manifest(repo),
        read_only_paths=(git_marker,),
    )

    assert result.succeeded
    assert git_marker.read_text() == "gitdir: protected\n"


# --- checkpoint bundle ------------------------------------------------------


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    bundle = CheckpointBundle(
        schema_version=checkpoint_mod.CHECKPOINT_SCHEMA,
        feature_id="feat-1",
        repository_id="synthetic",
        base_sha=BASE_SHA,
        head_sha=BASE_SHA,
        changed_files=("a.txt",),
        acceptance_progress=(),
        test_results={"format": 0, "lint": 0, "build": 0, "test": 0},
        toolchain_ref=".personal-agent/toolchain.json",
        toolchain_manifest_sha256="c" * 64,
        patch="diff body",
    )
    path = checkpoint_mod.write_checkpoint(root, bundle)
    assert path.is_file()
    assert not path.with_suffix(".json.tmp").exists()

    loaded = checkpoint_mod.load_checkpoint(root, "feat-1")
    assert loaded == bundle
    assert checkpoint_mod.checkpoint_exists(root, "feat-1")
    assert checkpoint_mod.load_checkpoint(root, "feat-missing") is None


# --- config loader ----------------------------------------------------------


def test_config_loader_fails_closed_on_unknown_key(tmp_path: Path) -> None:
    from personal_agent_dal.worker.config import load_worker_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "dal.worker-config/1.1",
                "worker_id": "w1",
                "transport": {"mode": "local", "database_path": str(tmp_path / "db")},
                "worktree_root": str(tmp_path / "wt"),
                "checkpoint_root": str(tmp_path / "cp"),
                "kill_switch_path": str(tmp_path / "disabled"),
                "lease_ttl_seconds": 60,
                "max_attempts": 3,
                "repos": {"synthetic": {"local_path": str(tmp_path / "repo")}},
                "allow_any_repo": True,
            }
        )
    )
    with pytest.raises(ValueError):
        load_worker_config(path)


# --- full poll_once cycle ---------------------------------------------------


def _make_synthetic_repo(
    repo: Path,
    *,
    format_cmd=("true",),
    test_cmd=("true",),
    fixture: bool = False,
    fixture_path: str = "README.md",
    template: str = "fixture {feature_id}\n",
    coder: bool = False,
    coder_allowed_paths=("README.md",),
) -> str:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    _write_manifest(
        repo,
        format_cmd=format_cmd,
        test_cmd=test_cmd,
        fixture=fixture,
        fixture_path=fixture_path,
        template=template,
        coder=coder,
        coder_allowed_paths=coder_allowed_paths,
    )
    (repo / "README.md").write_text("synthetic\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _seed_job_for(engine, base_sha: str, *, repository_id="synthetic") -> str:
    return queue.enqueue_job(
        engine,
        feature_id="feat-demo",
        repository_id=repository_id,
        base_sha=base_sha,
        branch_name="codex/feature-feat-demo",
        toolchain_ref=".personal-agent/toolchain.json",
    )


def test_poll_once_succeeds_end_to_end(engine, config, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo)
    job_id = _seed_job_for(engine, base_sha)

    outcome = _poll(engine, config)

    assert outcome.claimed
    assert outcome.state == "succeeded"

    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "succeeded"
    assert record.result_sha256 is not None
    assert _count(engine, "worker_result_receipts") == 1

    worktree = config.worktree_root / "feature-feat-demo"
    assert (worktree / ".git").exists()
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == base_sha

    bundle = checkpoint_mod.load_checkpoint(config.checkpoint_root, "feat-demo")
    assert bundle is not None
    assert bundle.base_sha == base_sha
    assert bundle.head_sha == base_sha
    assert bundle.test_results == {"format": 0, "lint": 0, "build": 0, "test": 0}


def test_poll_once_heartbeats_during_long_stage(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, test_cmd=("sleep", "0.8"))
    _seed_job_for(engine, base_sha)
    config = WorkerConfig(
        worker_id=config.worker_id,
        transport=config.transport,
        worktree_root=config.worktree_root,
        checkpoint_root=config.checkpoint_root,
        kill_switch_path=config.kill_switch_path,
        lease_ttl_seconds=1,
        max_attempts=config.max_attempts,
        repos=config.repos,
    )
    original = queue.heartbeat
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(queue, "heartbeat", counted)
    outcome = _poll(engine, config)

    assert outcome.state == "succeeded"
    assert calls >= 5


def test_poll_once_resumes_from_bound_checkpoint(engine, config, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo)
    manifest_path = repo / ".personal-agent" / "toolchain.json"
    body = json.loads(manifest_path.read_text())
    body["stages"]["format"]["cmd"] = ["touch", "format-reran"]
    body["stages"]["lint"]["cmd"] = ["touch", "lint-reran"]
    manifest_path.write_text(json.dumps(body))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--amend", "--no-edit", "-q"], check=True)
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    worktree = config.worktree_root / "feature-feat-demo"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            str(worktree),
            "-b",
            "codex/feature-feat-demo",
            base_sha,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    manifest = load_toolchain_manifest(worktree)
    checkpoint_mod.write_checkpoint(
        config.checkpoint_root,
        CheckpointBundle(
            schema_version=checkpoint_mod.CHECKPOINT_SCHEMA,
            feature_id="feat-demo",
            repository_id="synthetic",
            base_sha=base_sha,
            head_sha=base_sha,
            changed_files=(),
            acceptance_progress=("format", "lint"),
            test_results={"format": 0, "lint": 0},
            toolchain_ref=".personal-agent/toolchain.json",
            toolchain_manifest_sha256=manifest.manifest_sha256,
            patch="",
        ),
    )
    _seed_job_for(engine, base_sha)

    outcome = _poll(engine, config)

    assert outcome.state == "succeeded"
    assert not (worktree / "format-reran").exists()
    assert not (worktree / "lint-reran").exists()


def test_poll_once_restores_nonempty_patch_after_supervisor_crash(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    format_cmd = (
        "/usr/bin/python3",
        "-c",
        "from pathlib import Path; "
        "p=Path('README.md'); p.write_text(p.read_text()+'branch-only change\\n')",
    )
    base_sha = _make_synthetic_repo(repo, format_cmd=format_cmd)
    _seed_job_for(engine, base_sha)
    config = WorkerConfig(
        worker_id=config.worker_id,
        transport=config.transport,
        worktree_root=config.worktree_root,
        checkpoint_root=config.checkpoint_root,
        kill_switch_path=config.kill_switch_path,
        lease_ttl_seconds=1,
        max_attempts=config.max_attempts,
        repos=config.repos,
    )
    original_write = checkpoint_mod.write_checkpoint

    # KeyboardInterrupt is a deterministic proxy for the crash boundary this
    # test actually targets: "the checkpoint is durable but the job state has
    # not advanced".  It is raised at the same logical point a SIGKILL would
    # have to land to produce a dirty state (after the atomic checkpoint, before
    # the post-format transition), and it propagates uncaught so the recovery
    # path -- not in-process cleanup -- does the resuming.  Kill *timing* at an
    # arbitrary instruction is an OS property, not a recovery invariant, and is
    # exercised in the G3 live crash acceptance rather than this deterministic
    # suite.
    def crash_after_first_checkpoint(root, bundle):
        path = original_write(root, bundle)
        if bundle.acceptance_progress == ("format",):
            raise KeyboardInterrupt("synthetic supervisor crash")
        return path

    monkeypatch.setattr(checkpoint_mod, "write_checkpoint", crash_after_first_checkpoint)
    with pytest.raises(KeyboardInterrupt, match="synthetic supervisor crash"):
        _poll(engine, config)

    worktree = config.worktree_root / "feature-feat-demo"
    crashed = checkpoint_mod.load_checkpoint(config.checkpoint_root, "feat-demo")
    assert crashed is not None
    assert crashed.acceptance_progress == ("format",)
    assert crashed.changed_files == ("README.md",)
    assert crashed.patch
    assert (worktree / "README.md").read_text().count("branch-only change") == 1

    # Simulate loss of the mutable worktree while preserving the durable
    # checkpoint.  The replacement process must apply the bound patch before
    # resuming at lint; format must not execute a second time.
    subprocess.run(
        ["git", "-C", str(worktree), "checkout", "--", "README.md"], check=True
    )
    monkeypatch.setattr(checkpoint_mod, "write_checkpoint", original_write)
    time.sleep(1.1)

    outcome = _poll(engine, config)

    assert outcome.state == "succeeded"
    restored = checkpoint_mod.load_checkpoint(config.checkpoint_root, "feat-demo")
    assert restored is not None
    assert restored.acceptance_progress == ("format", "lint", "build", "test")
    assert restored.changed_files == ("README.md",)
    assert restored.patch == crashed.patch
    assert (worktree / "README.md").read_text().count("branch-only change") == 1


def test_poll_once_rejects_precreated_non_worktree(engine, config, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo)
    job_id = _seed_job_for(engine, base_sha)
    fake = config.worktree_root / "feature-feat-demo"
    _write_manifest(fake)

    outcome = _poll(engine, config)

    assert outcome.state == "failed"
    assert outcome.error == "worktree_not_registered"
    assert queue.get_job(engine, job_id=job_id).state == "failed"
    # A refusal receipt, not a toolchain receipt: the refusal digest is what
    # lets this reach the authority as a category instead of a lease timeout.
    assert _count(engine, "worker_result_receipts") == 1


def test_kill_switch_prevents_claim(engine, config, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo)
    job_id = _seed_job_for(engine, base_sha)
    config.kill_switch_path.write_text("disabled\n")

    outcome = _poll(engine, config)

    assert not outcome.claimed
    assert outcome.error == "kill_switch_active"
    assert queue.get_job(engine, job_id=job_id).state == "pending"


def test_kill_switch_stops_an_active_toolchain(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, test_cmd=("sleep", "0.8"))
    job_id = _seed_job_for(engine, base_sha)
    original = queue.heartbeat
    calls = 0

    def engage_switch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            config.kill_switch_path.write_text("disabled\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(queue, "heartbeat", engage_switch)
    outcome = _poll(engine, config)

    assert outcome.state is None
    assert outcome.error == "kill_switch_active"
    # An operator stop is not a job failure. The lease is left active so that a
    # restart after the switch clears can reclaim and retry, rather than the job
    # dying terminal to what was, from its point of view, an interrupt.
    assert queue.get_job(engine, job_id=job_id).state in queue.ACTIVE_JOB_STATES
    assert _count(engine, "worker_result_receipts") == 0


def test_poll_once_leaves_a_lost_lease_to_reclaim(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    """A lost lease is not a terminal failure — it is the reclaim path's job.

    The worker stops without writing a result, leaving the lease active so the
    authority can reclaim it and retry. Writing `failed` here would skip the
    recovery that already exists and kill the job on a fence loss.
    """
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, test_cmd=("sleep", "0.5"))
    job_id = _seed_job_for(engine, base_sha)

    monkeypatch.setattr(queue, "heartbeat", lambda *args, **kwargs: False)

    outcome = _poll(engine, config)

    assert outcome.state is None
    assert outcome.error == "lease_lost"
    assert queue.get_job(engine, job_id=job_id).state in queue.ACTIVE_JOB_STATES
    assert _count(engine, "worker_result_receipts") == 0


# --- DAL-R07A fixture slice --------------------------------------------------


def test_poll_once_fixture_slice_succeeds(engine, config, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, fixture=True)
    job_id = _seed_job_for(engine, base_sha)

    outcome = _poll(engine, config)

    assert outcome.state == "succeeded"
    record = queue.get_job(engine, job_id=job_id)
    assert record.result_sha256 is not None
    assert _count(engine, "worker_result_receipts") == 1
    # The fixture wrote README.md, so the checkpoint carries the real diff.
    bundle = checkpoint_mod.load_checkpoint(config.checkpoint_root, "feat-demo")
    assert bundle is not None
    assert bundle.changed_files == ("README.md",)
    assert "fixture feat-demo" in bundle.patch


def test_poll_once_fixture_slice_fails_on_a_failed_test(
    engine, config, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, fixture=True, test_cmd=("false",))
    job_id = _seed_job_for(engine, base_sha)

    outcome = _poll(engine, config)

    assert outcome.state == "failed"
    assert outcome.error in ("verification_blocked", "verification_failed")
    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "failed"
    assert record.result_sha256 is not None


def test_poll_once_fixture_slice_fails_on_an_empty_diff(
    engine, config, tmp_path: Path
) -> None:
    # The fixture rewrites README.md to the same bytes it already holds, so
    # `git diff HEAD` is empty and the verification refuses the unbound patch.
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, fixture=True, template="synthetic\n")
    job_id = _seed_job_for(engine, base_sha)

    outcome = _poll(engine, config)

    assert outcome.state == "failed"
    assert outcome.error in ("verification_blocked", "verification_failed")


def test_poll_once_fixture_slice_refuses_a_missing_registry(
    engine, config, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    _make_synthetic_repo(repo, fixture=True)
    # Keep fixture_coder but drop the registry: the slice must refuse rather
    # than invent a diff command.
    manifest_path = repo / ".personal-agent" / "toolchain.json"
    body = json.loads(manifest_path.read_text())
    del body["registry"]
    manifest_path.write_text(json.dumps(body))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "drop registry"], check=True
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    job_id = _seed_job_for(engine, base_sha)

    outcome = _poll(engine, config)

    assert outcome.state == "failed"
    assert outcome.error == "fixture_registry_missing"
    assert queue.get_job(engine, job_id=job_id).state == "failed"


def test_poll_once_fixture_slice_leaves_a_lost_lease_to_reclaim(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    """The fixture slice's verification runs under the same lease fence.

    A lease lost mid-verification is abandoned for reclaim, not terminal, exactly
    like the toolchain path.
    """
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, fixture=True, test_cmd=("sleep", "0.5"))
    job_id = _seed_job_for(engine, base_sha)

    monkeypatch.setattr(queue, "heartbeat", lambda *args, **kwargs: False)

    outcome = _poll(engine, config)

    assert outcome.state is None
    assert outcome.error == "lease_lost"
    assert queue.get_job(engine, job_id=job_id).state in queue.ACTIVE_JOB_STATES
    assert _count(engine, "worker_result_receipts") == 0


# --- DAL-R07B provider-coder route (offline, fake run_coder) ----------------


_CODER_MANIFEST = {
    "model_alias": "dal-ccr-primary",
    "allowed_tools": ("Read", "Edit", "Bash"),
    "max_turns": 8,
    "max_wall_seconds": 900,
    "max_patch_bytes": 1048576,
    "prompt": "In {feature_id}, add one tracked change.",
    "allowed_paths": ("README.md",),
    "pinned_endpoint": "127.0.0.1:3456",
}


def _make_claude_fake(
    base_sha: str,
    *,
    write: bool = True,
    result_error: bool = False,
    output: str | None = None,
    truncated: bool = False,
):
    """A fake `run_coder` emitting claude's native stream-json, not the
    coder_contract vocabulary — so the adapter under test is actually exercised."""

    def fake(spec, **kw):
        if write:
            (spec.cwd / "README.md").write_text("coder change\n")
        if output is not None:
            output_text = output
        else:
            lines = [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": "s",
                        "model": "DeepSeek/deepseek-v4-pro",
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "I'll make the change."},
                                {
                                    "type": "tool_use",
                                    "id": "toolu_1",
                                    "name": "Edit",
                                    "input": {
                                        "file_path": "README.md",
                                        "new_string": "coder change\n",
                                    },
                                },
                            ],
                        },
                        "stop_reason": "tool_use",
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_1",
                                    "content": "ok",
                                }
                            ],
                        },
                    }
                ),
            ]
            if result_error:
                lines.append(
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "error",
                            "is_error": True,
                            "result": "",
                            "error": "boom",
                        }
                    )
                )
            else:
                lines.append(
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "is_error": False,
                            "result": "done",
                            "num_turns": 1,
                        }
                    )
                )
            output_text = "\n".join(lines) + "\n"
        return CoderRunResult(
            returncode=1 if result_error else 0,
            output=output_text,
            timed_out=False,
            cancelled=False,
            duration_s=0.01,
            truncated=truncated,
        )

    return fake


def _coder_config(tmp_path: Path, config: WorkerConfig) -> WorkerConfig:
    """A worker config carrying a 0600 claude -> CCR appkey file."""
    token = tmp_path / "coder-token"
    token.write_text("test-ccr-token\n")
    os.chmod(token, 0o600)
    return dataclasses.replace(config, coder_token_path=token)


def test_poll_once_provider_coder_succeeds(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, coder=True)
    job_id = _seed_job_for(engine, base_sha)

    monkeypatch.setattr(
        "personal_agent_dal.worker.poll_once.run_coder", _make_claude_fake(base_sha)
    )

    outcome = _poll(engine, _coder_config(tmp_path, config))

    assert outcome.state == "succeeded"
    record = queue.get_job(engine, job_id=job_id)
    assert record.result_sha256 is not None
    assert _count(engine, "worker_result_receipts") == 1
    # The coder wrote the worktree; the checkpoint captures that diff.
    bundle = checkpoint_mod.load_checkpoint(config.checkpoint_root, "feat-demo")
    assert bundle is not None
    assert "coder change" in bundle.patch


def test_poll_once_provider_coder_refuses_a_provider_error(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, coder=True)
    job_id = _seed_job_for(engine, base_sha)

    # claude's result event reports subtype=error: the worker refuses before
    # fabricating a final event.
    monkeypatch.setattr(
        "personal_agent_dal.worker.poll_once.run_coder",
        _make_claude_fake(base_sha, result_error=True),
    )

    outcome = _poll(engine, _coder_config(tmp_path, config))

    assert outcome.state == "failed"
    assert outcome.error.startswith("coder_provider_error")


def test_poll_once_provider_coder_refuses_an_empty_diff(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, coder=True)
    job_id = _seed_job_for(engine, base_sha)

    # The coder ran cleanly but wrote nothing: the adapter's final declares an
    # empty changed_files list, which the classifier refuses.
    monkeypatch.setattr(
        "personal_agent_dal.worker.poll_once.run_coder",
        _make_claude_fake(base_sha, write=False),
    )

    outcome = _poll(engine, _coder_config(tmp_path, config))

    assert outcome.state == "failed"
    assert outcome.error.startswith(("coder_failed", "coder_blocked"))


def test_poll_once_provider_coder_refuses_unparseable_output(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, coder=True)
    job_id = _seed_job_for(engine, base_sha)

    monkeypatch.setattr(
        "personal_agent_dal.worker.poll_once.run_coder",
        _make_claude_fake(base_sha, output="not-json\n"),
    )

    outcome = _poll(engine, _coder_config(tmp_path, config))

    assert outcome.state == "failed"
    assert outcome.error == "coder_output_unparseable"


def test_poll_once_provider_coder_distinguishes_truncated_from_unparseable(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    """A byte-capped stream refuses as `coder_output_truncated`, not unparseable.

    R10 T1 (2026-09-09): a healthy 65,562-byte coder run was cut at the
    historical 64 KiB cap mid-event and refused as `coder_output_unparseable`,
    hiding a budget condition behind a corruption message. The cap now signals
    structurally, and this refusal reason keeps "raise the budget" observable
    and distinct from genuine CLI corruption.
    """
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, coder=True)
    job_id = _seed_job_for(engine, base_sha)

    # The same unparseable tail, but arriving with truncated=True — exactly
    # the shape a capped run produces (cut lands mid-event by construction).
    monkeypatch.setattr(
        "personal_agent_dal.worker.poll_once.run_coder",
        _make_claude_fake(
            base_sha,
            output='{"type": "system", "subtype": "init"}\n{"type": "assista',
            truncated=True,
        ),
    )

    outcome = _poll(engine, _coder_config(tmp_path, config))

    assert outcome.state == "failed"
    assert outcome.error == "coder_output_truncated"


def _make_prompt_capturing_fake(base_sha: str, captured: dict):
    """A claude fake that records the exact prompt it was launched with."""

    def fake(spec, **kw):
        captured["prompt"] = spec.prompt
        return _make_claude_fake(base_sha)(spec, **kw)

    return fake


def test_coder_prompt_receives_the_task_description(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    """F7 (2026-09-07 review): the persisted intake body reaches the coder.

    The old code substituted only {feature_id}; the task text existed solely
    inside the feature-id hash, so the coder never saw the task. A manifest
    carrying {task_description} must now receive the body the intake bound,
    with {feature_id} still substituted.
    """
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, coder=True)
    # Rewrite the manifest's coder prompt to use both placeholders.
    manifest_path = repo / ".personal-agent" / "toolchain.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["coder"]["prompt"] = (
        "Feature {feature_id}: {task_description}"
    )
    manifest_path.write_text(json.dumps(manifest))
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "task-description manifest"],
        check=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    body = "Add a boundary test for the pure-string helper."
    job_id = queue.enqueue_job(
        engine,
        feature_id="feat-demo",
        repository_id="synthetic",
        base_sha=base_sha,
        branch_name="codex/feature-feat-demo",
        toolchain_ref=".personal-agent/toolchain.json",
        intake_key="intake:feat-demo",
        task_description=body,
    )

    captured: dict = {}
    monkeypatch.setattr(
        "personal_agent_dal.worker.poll_once.run_coder",
        _make_prompt_capturing_fake(base_sha, captured),
    )

    outcome = _poll(engine, _coder_config(tmp_path, config))

    assert outcome.state == "succeeded"
    assert captured["prompt"] == f"Feature feat-demo: {body}"


def test_coder_prompt_without_the_placeholder_is_unchanged(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    """A manifest without {task_description} keeps its exact old behaviour.

    Backward compatibility: existing pinned manifests must not drift — the
    placeholder is opt-in, and a body being present on the lease changes
    nothing for a manifest that does not reference it.
    """
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, coder=True)
    body = "Add a boundary test for the pure-string helper."
    queue.enqueue_job(
        engine,
        feature_id="feat-demo",
        repository_id="synthetic",
        base_sha=base_sha,
        branch_name="codex/feature-feat-demo",
        toolchain_ref=".personal-agent/toolchain.json",
        intake_key="intake:feat-demo",
        task_description=body,
    )

    captured: dict = {}
    monkeypatch.setattr(
        "personal_agent_dal.worker.poll_once.run_coder",
        _make_prompt_capturing_fake(base_sha, captured),
    )

    outcome = _poll(engine, _coder_config(tmp_path, config))

    assert outcome.state == "succeeded"
    assert captured["prompt"] == "In feat-demo, add one tracked change."


def test_task_body_digest_mismatch_refuses(
    engine, config, tmp_path: Path, monkeypatch
) -> None:
    """F7's binding fence: a tampered body refuses before any coder runs.

    The persisted body and the digest the lease carries must agree; a row
    edited between persistence and claim must not feed the coder altered
    text behind an intact-looking binding.
    """
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, coder=True)
    job_id = queue.enqueue_job(
        engine,
        feature_id="feat-demo",
        repository_id="synthetic",
        base_sha=base_sha,
        branch_name="codex/feature-feat-demo",
        toolchain_ref=".personal-agent/toolchain.json",
        intake_key="intake:feat-demo",
        task_description="original task body",
    )

    # Tamper the persisted body after the fact; the digest stays original.
    with engine.connect() as connection:
        connection.execute(
            text(
                "UPDATE feature_intake_requests SET task_description = :t "
                "WHERE intake_key = :k"
            ).bindparams(t="tampered task body", k="intake:feat-demo")
        )
        connection.commit()

    ran: list[str] = []
    monkeypatch.setattr(
        "personal_agent_dal.worker.poll_once.run_coder",
        lambda *a, **k: ran.append("ran") or _make_claude_fake(base_sha)(*a, **k),
    )

    outcome = _poll(engine, _coder_config(tmp_path, config))

    assert outcome.state == "failed"
    assert outcome.error == "task_body_digest_mismatch"
    assert ran == [], "the coder must not run on a tampered body"
    record = queue.get_job(engine, job_id=job_id)
    assert record is not None and record.state == "failed"


def test_launchd_template_runs_as_login_user() -> None:
    path = (
        Path(__file__).parents[2]
        / "src/personal_agent_dal/worker/launchd/org.example.personal-agent-dal-worker.plist"
    )
    body = plistlib.loads(path.read_bytes())
    assert "UserName" not in body
    assert "GroupName" not in body
    assert body["Label"] == "org.example.personal-agent-dal-worker"
    assert body["ProgramArguments"][-1] == "poll-once"
    assert body["StartInterval"] == 300


def test_poll_once_toolchain_failure_records_receipt(engine, config, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _make_synthetic_repo(repo, test_cmd=("false",))
    job_id = _seed_job_for(engine, base_sha)

    outcome = _poll(engine, config)

    assert outcome.state == "failed"
    assert outcome.error == "toolchain_failed"

    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "failed"
    assert record.last_error == "toolchain_failed"
    # a toolchain that ran is still a result worth receipting
    assert _count(engine, "worker_result_receipts") == 1


def test_poll_once_refuses_unallowlisted_repo(engine, config) -> None:
    job_id = _seed_job_for(engine, BASE_SHA, repository_id="other")

    outcome = _poll(engine, config)

    assert outcome.state == "failed"
    assert outcome.error == "repo_not_allowlisted"

    record = queue.get_job(engine, job_id=job_id)
    assert record.state == "failed"
    assert record.last_error == "repo_not_allowlisted"
    # No toolchain ran, but the refusal is still a fact the authority receipts:
    # without a digest this deterministic failure could only be reported as a
    # timeout, and the transport contract has no way to carry it at all.
    assert _count(engine, "worker_result_receipts") == 1
    assert record.result_sha256 is not None


def test_poll_once_no_pending_job_is_a_noop(engine, config) -> None:
    outcome = _poll(engine, config)
    assert not outcome.claimed
    assert outcome.state is None
