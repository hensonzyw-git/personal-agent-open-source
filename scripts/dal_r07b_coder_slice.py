#!/usr/bin/env python3
"""DAL-R07B real-coder slice: one real `claude -p` through the worker.

This is the live half of R07B, run under Henson's explicit authorisation: real
`claude -p` on this machine (== the roadmap Mac mini), via the local CCR proxy,
DeepSeek V4 Pro, at most 3 calls, 180s each, on a temporary synthetic repo with
allowed path README.md only.

It reuses the R07A two-process skeleton: a real TLS service process plus a real
`poll-once` CLI worker. The only difference is the synthetic repo's manifest
declares `coder` (not `fixture_coder`), so `poll_once` launches the real
`run_coder` instead of the fixture. No commit/push/PR, no GitHub write.
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
MAX_WALL_SECONDS = 180
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


def _make_coder_repo(repo: Path, *, max_wall_seconds: int = MAX_WALL_SECONDS) -> str:
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
                    "test": {"cmd": ["true"]},
                },
                "coder": {
                    "model_alias": "dal-ccr-primary",
                    "allowed_tools": ["Read", "Edit", "Bash"],
                    "max_turns": 8,
                    "max_wall_seconds": max_wall_seconds,
                    "max_patch_bytes": 1048576,
                    "prompt": (
                        "Append the line 'coder change for {feature_id}' to README.md. "
                        "Keep every other line unchanged."
                    ),
                    "allowed_paths": ["README.md"],
                    "pinned_endpoint": "127.0.0.1:3456",
                },
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
        [
            sys.executable, "-m", "personal_agent_dal.worker.cli",
            "--config", str(config_path), "poll-once",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=MAX_WALL_SECONDS + 120,
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
    root = Path(tempfile.mkdtemp(prefix="dal-r07b-"))
    service_dir = root / "service"
    service_dir.mkdir()
    api_process: subprocess.Popen | None = None

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
        base_sha = _make_coder_repo(repo)

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

        worker_home = root / "worker"
        config_path = root / "worker-config.json"
        _write_worker_config(
            config_path, endpoint=endpoint, ca_bundle=cert, worker_id="worker-1",
            repo=repo, home=worker_home, enrollment_secret_path=enrollment_secret,
        )

        job_id = queue.enqueue_job(
            engine,
            feature_id="live",
            repository_id="synthetic",
            base_sha=base_sha,
            branch_name="codex/feature-live",
            toolchain_ref=TOOLCHAIN_REF,
        )

        completed = _run_worker(config_path)
        report.record(
            "worker poll-once exited",
            completed.returncode == 0,
            f"rc={completed.returncode} stderr={completed.stderr.strip()[-300:]}",
        )
        record = queue.get_job(engine, job_id=job_id)
        report.record(
            "job reached succeeded",
            record.state == "succeeded",
            f"{record.state} last_error={record.last_error}",
        )
        report.record("result receipt accepted", record.result_sha256 is not None)
        report.record(
            "authority recorded a checkpoint", len(_checkpoint_rows(engine, job_id)) == 1
        )
        bundle = load_checkpoint(worker_home / "checkpoints", "live")
        report.record(
            "checkpoint carries a non-empty diff",
            bundle is not None and "coder change for live" in bundle.patch,
        )
        worktree_readme = worker_home / "worktrees" / "feature-live" / "README.md"
        report.record(
            "worktree really contains the coder change",
            worktree_readme.is_file()
            and "coder change for live" in worktree_readme.read_text(),
        )

        engine.dispose()
        print()
        print(f"{len(report.checks) - report.failed} PASS / {report.failed} FAIL")
        print("scope: one real claude -p call, temporary synthetic repo, no GitHub write.")
        return 1 if report.failed else 0
    finally:
        if api_process is not None and api_process.poll() is None:
            api_process.send_signal(signal.SIGTERM)
            try:
                api_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                api_process.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
