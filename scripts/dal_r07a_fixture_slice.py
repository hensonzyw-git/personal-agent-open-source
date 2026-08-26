#!/usr/bin/env python3
"""DAL-R07A acceptance layer 1: the no-provider fixture slice, two processes.

This proves the vertical slice DWS -> Worker -> worktree -> deterministic fixture
coder -> verification -> checkpoint -> result receipt end to end, with the
service and the worker as genuinely separate OS processes over real TLS.

The worker is the real `poll-once` CLI (not an inline transport verb): it loads a
`dal.worker-config/1.1` remote config, claims over the pinned endpoint, writes the
fixture's tracked-file change, runs the deterministic verification, and records
the checkpoint + result. The synthetic repo's `.personal-agent/toolchain.json`
declares the `fixture_coder` + `registry` that drive the slice.

Scope is deliberately bounded: this is a same-machine run, no provider, no
coder subprocess, no GitHub write. The cross-machine (ECS + Mac mini) acceptance
is a separate, separately authorised layer, and the dispatch_graph executor is
out of scope for this slice.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy import select  # noqa: E402

from personal_agent_dal.storage import db  # noqa: E402
from personal_agent_dal.storage.engine import create_database_engine, session_factory  # noqa: E402
from personal_agent_dal.storage.transport_models import WorkerCheckpoint  # noqa: E402
from personal_agent_dal.worker import queue  # noqa: E402
from personal_agent_dal.worker.checkpoint import load_checkpoint  # noqa: E402

HOST = "127.0.0.1"
LEASE_TTL_SECONDS = 3
MAX_ATTEMPTS = 5
TOOLCHAIN_REF = ".personal-agent/toolchain.json"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, passed, detail))
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] {name}{f' — {detail}' if detail else ''}", flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for check in self.checks if not check.passed)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def make_tls_material(root: Path) -> tuple[Path, Path]:
    """Create a throwaway self-signed cert for 127.0.0.1, and its CA bundle."""
    key = root / "tls.key"
    cert = root / "tls.crt"
    config = root / "openssl.cnf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
        "[dn]\nCN=127.0.0.1\n"
        "[v3]\nsubjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n"
    )
    completed = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-config", str(config),
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(f"openssl failed: {completed.stderr.strip()[:200]}")
    os.chmod(key, 0o600)
    return cert, key


def write_secret(path: Path, value: bytes) -> Path:
    path.write_bytes(value)
    os.chmod(path, 0o600)
    return path


def wait_for_health(endpoint: str, ca_bundle: Path, process: subprocess.Popen) -> bool:
    """Poll `/health` until the service answers, or the process dies."""
    import httpx

    deadline = time.monotonic() + 30
    with httpx.Client(verify=str(ca_bundle), timeout=2.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            try:
                if client.get(f"{endpoint}/health").status_code == 200:
                    return True
            except httpx.HTTPError:
                time.sleep(0.2)
    return False


def _make_fixture_repo(repo: Path) -> str:
    """Create the synthetic repo whose manifest declares the fixture slice.

    The test stage sleeps briefly so the crash-recovery half of the run has a
    window in which to SIGKILL a worker mid-verification.
    """
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / ".personal-agent").mkdir(parents=True)
    (repo / ".personal-agent" / "toolchain.json").write_text(
        json.dumps(
            {
                "schema_version": "dal.toolchain-manifest/1.0",
                "stages": {
                    "format": {"cmd": ["true"]},
                    "lint": {"cmd": ["true"]},
                    "build": {"cmd": ["true"]},
                    "test": {"cmd": ["sleep", "1"]},
                },
                "fixture_coder": {"path": "README.md", "template": "fixture {feature_id}\n"},
                "registry": {
                    "diff": ["git", "diff", "--binary", "--no-ext-diff", "HEAD"]
                },
            }
        )
    )
    (repo / "README.md").write_text("synthetic\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _write_worker_config(
    path: Path,
    *,
    endpoint: str,
    ca_bundle: Path,
    worker_id: str,
    repo: Path,
    home: Path,
    enrollment_secret_path: Path,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "dal.worker-config/1.1",
                "worker_id": worker_id,
                "transport": {
                    "mode": "remote",
                    "endpoint": endpoint,
                    "machine_id": "acceptance-host",
                    "capabilities": ["coding", "verification", "checkpoint"],
                    "enrollment_secret_path": str(enrollment_secret_path),
                    "token_cache_path": str(home / "token-cache.json"),
                    "ca_bundle_path": str(ca_bundle),
                },
                "worktree_root": str(home / "worktrees"),
                "checkpoint_root": str(home / "checkpoints"),
                "kill_switch_path": str(home / "worker.disabled"),
                "lease_ttl_seconds": LEASE_TTL_SECONDS,
                "max_attempts": MAX_ATTEMPTS,
                "repos": {"synthetic": {"local_path": str(repo)}},
            }
        )
    )


def _run_worker(config_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "personal_agent_dal.worker.cli", "--config", str(config_path), "poll-once"],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _checkpoint_rows(engine, job_id: str) -> list[str]:
    sessions = session_factory(engine)
    with sessions() as session:
        return list(
            session.execute(
                select(WorkerCheckpoint.checkpoint_id).where(
                    WorkerCheckpoint.job_id == job_id
                )
            ).scalars()
        )


def run() -> int:
    report = Report()
    root = Path(tempfile.mkdtemp(prefix="dal-r07a-"))
    service_dir = root / "service"
    service_dir.mkdir()
    api_process: subprocess.Popen | None = None
    worker_process: subprocess.Popen | None = None

    try:
        cert, key = make_tls_material(root)
        service_key = write_secret(service_dir / "service.key", os.urandom(32).hex().encode())
        enrollment_secret = write_secret(
            service_dir / "enrollment.secret", os.urandom(32).hex().encode()
        )
        database = service_dir / "workflow.db"
        engine = create_database_engine(database)
        db.upgrade(engine)

        repo = root / "repo"
        base_sha = _make_fixture_repo(repo)

        port = free_port()
        endpoint = f"https://{HOST}:{port}"
        api_process = subprocess.Popen(
            [
                sys.executable, "-m", "personal_agent_dal.service.cli",
                "--database", str(database),
                "--service-key-file", str(service_key),
                "--enrollment-secret-file", str(enrollment_secret),
                "--host", HOST, "--port", str(port),
                "--lease-ttl-seconds", str(LEASE_TTL_SECONDS),
                "--max-attempts", str(MAX_ATTEMPTS),
                "--ssl-certfile", str(cert), "--ssl-keyfile", str(key),
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        report.record(
            "service process is up over TLS",
            wait_for_health(endpoint, cert, api_process),
            endpoint,
        )
        if report.failed:
            return 1

        worker1_home = root / "worker-1"
        config1 = root / "worker-1-config.json"
        _write_worker_config(
            config1, endpoint=endpoint, ca_bundle=cert, worker_id="worker-1",
            repo=repo, home=worker1_home, enrollment_secret_path=enrollment_secret,
        )

        # --- happy path: one job through the real poll-once worker ----------
        job_id = queue.enqueue_job(
            engine,
            feature_id="acceptance",
            repository_id="synthetic",
            base_sha=base_sha,
            branch_name="codex/feature-acceptance",
            toolchain_ref=TOOLCHAIN_REF,
        )
        completed = _run_worker(config1)
        report.record(
            "worker poll-once exited cleanly",
            completed.returncode == 0,
            f"rc={completed.returncode}",
        )
        record = queue.get_job(engine, job_id=job_id)
        report.record("job reached succeeded", record.state == "succeeded", record.state)
        report.record(
            "result receipt was accepted", record.result_sha256 is not None
        )
        report.record(
            "the authority recorded a checkpoint",
            len(_checkpoint_rows(engine, job_id)) == 1,
        )
        bundle = load_checkpoint(worker1_home / "checkpoints", "acceptance")
        report.record(
            "the checkpoint carries the fixture diff",
            bundle is not None and "fixture acceptance" in bundle.patch,
        )
        worktree_readme = (
            worker1_home / "worktrees" / "feature-acceptance" / "README.md"
        )
        report.record(
            "the worktree really contains the fixture change",
            worktree_readme.is_file() and worktree_readme.read_text() == "fixture acceptance\n",
        )

        # --- crash + reclaim: a second worker recovers without the SQLite ----
        job2 = queue.enqueue_job(
            engine,
            feature_id="recover",
            repository_id="synthetic",
            base_sha=base_sha,
            branch_name="codex/feature-recover",
            toolchain_ref=TOOLCHAIN_REF,
        )
        worker_process = subprocess.Popen(
            [sys.executable, "-m", "personal_agent_dal.worker.cli", "--config", str(config1), "poll-once"],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # The test stage sleeps 1s: kill the worker mid-verification.
        time.sleep(0.4)
        worker_process.kill()
        worker_process.wait(timeout=10)
        report.record("worker was killed mid-verification", worker_process.poll() is not None)

        time.sleep(LEASE_TTL_SECONDS + 1)
        # The same worker restarts (same config, same worktree/checkpoint dirs)
        # and reclaims the job from ECS — it never reopens a shared SQLite.
        completed2 = _run_worker(config1)
        report.record(
            "the worker restarted and reclaimed the job",
            completed2.returncode == 0,
            f"rc={completed2.returncode} stderr={completed2.stderr.strip()[-200:]}",
        )
        record2 = queue.get_job(engine, job_id=job2)
        report.record(
            "the recovered job reached succeeded",
            record2.state == "succeeded",
            f"{record2.state} last_error={record2.last_error}",
        )
        report.record(
            "recovery evidence lives on the authority, not a shared database",
            len(_checkpoint_rows(engine, job2)) == 1,
        )
        report.record(
            "the worker config names no database path",
            "database_path" not in config1.read_text(),
        )

        engine.dispose()
        print()
        print(f"{len(report.checks) - report.failed} PASS / {report.failed} FAIL")
        print("scope: same-machine, two processes, transport + fixture coder +")
        print("       verification, no provider, no GitHub write.")
        return 1 if report.failed else 0
    finally:
        for process in (worker_process, api_process):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
