#!/usr/bin/env python3
"""DAL-R06 acceptance layer 1: two processes, two databases, real TLS.

This runs the DAL-R03 synthetic transport scenario end to end with the service
and the worker as genuinely separate OS processes, over a real TLS socket:

    enqueue -> outbound claim -> heartbeat x2 -> checkpoint v1 -> SIGKILL worker
    -> reclaim -> checkpoint v2 -> result -> identical replay -> conflicting
    result -> cancel a second job

Two things it deliberately does *not* claim. It is a **same-machine** run: it is
evidence about the transport, not about the Mac mini, and nothing here may be
reported as cross-machine acceptance. And it stops at the transport — no coder,
no provider, no toolchain; the full vertical slice is DAL-R07A.

The TLS is not decoration. Production pins an HTTPS origin and refuses
redirects, so the acceptance runs the same code path against a throwaway CA:
the worker is given `ca_bundle_path`, and a certificate it does not trust ends
the run. Everything is created under one temporary directory and removed on
exit; no repository, credential or database outside it is touched.
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

from personal_agent_dal.storage import db  # noqa: E402
from personal_agent_dal.storage.engine import create_database_engine  # noqa: E402
from personal_agent_dal.worker import queue  # noqa: E402
from personal_agent_dal.worker.checkpoint import (  # noqa: E402
    CHECKPOINT_SCHEMA,
    CheckpointBundle,
)
from personal_agent_dal.worker.remote import (  # noqa: E402
    RemoteHttpAdapter,
    RemoteTransportSettings,
)
from personal_agent_dal.worker.transport import TransportError  # noqa: E402

BASE_SHA = "0" * 40
RESULT_SHA = "c" * 64
CONFLICTING_SHA = "d" * 64
HOST = "127.0.0.1"
LEASE_TTL_SECONDS = 2
MAX_ATTEMPTS = 5


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


def worker_settings(
    root: Path, endpoint: str, ca_bundle: Path, *, worker_id: str
) -> RemoteTransportSettings:
    """Worker-side state, entirely under the worker's own directory."""
    worker_home = root / "worker"
    worker_home.mkdir(exist_ok=True)
    return RemoteTransportSettings(
        endpoint=endpoint,
        worker_id=worker_id,
        machine_id="acceptance-host",
        capabilities=("coding", "verification", "checkpoint"),
        enrollment_secret_path=root / "service" / "enrollment.secret",
        token_cache_path=worker_home / "token-cache.json",
        checkpoint_root=worker_home / "checkpoints",
        ca_bundle_path=ca_bundle,
        request_timeout_seconds=10.0,
        retry_attempts=3,
        backoff_base_seconds=0.1,
        backoff_max_seconds=1.0,
    )


def bundle(feature_id: str, *, sequence: int) -> CheckpointBundle:
    stages = ("format", "lint", "build", "test")[:sequence]
    return CheckpointBundle(
        schema_version=CHECKPOINT_SCHEMA,
        feature_id=feature_id,
        repository_id="synthetic",
        base_sha=BASE_SHA,
        head_sha=BASE_SHA,
        changed_files=("README.md",),
        acceptance_progress=stages,
        test_results={stage: 0 for stage in stages},
        toolchain_ref="toolchain-v1",
        toolchain_manifest_sha256="e" * 64,
        patch=f"diff-after-{sequence}-stages",
    )


WORKER_STEP = '''
import json, sys
sys.path.insert(0, {src!r})
from pathlib import Path
from personal_agent_dal.worker.checkpoint import CHECKPOINT_SCHEMA, CheckpointBundle
from personal_agent_dal.worker.remote import RemoteHttpAdapter, RemoteTransportSettings

spec = json.loads(sys.stdin.read())
settings = RemoteTransportSettings(
    endpoint=spec["endpoint"],
    worker_id=spec["worker_id"],
    machine_id="acceptance-host",
    capabilities=("coding", "verification", "checkpoint"),
    enrollment_secret_path=Path(spec["enrollment_secret_path"]),
    token_cache_path=Path(spec["token_cache_path"]),
    checkpoint_root=Path(spec["checkpoint_root"]),
    ca_bundle_path=Path(spec["ca_bundle_path"]),
    request_timeout_seconds=10.0,
    retry_attempts=3,
    backoff_base_seconds=0.1,
    backoff_max_seconds=1.0,
)
adapter = RemoteHttpAdapter(settings)
lease = adapter.claim()
if lease is None:
    print(json.dumps({{"claimed": False}}), flush=True)
    raise SystemExit(1)
adapter.heartbeat(lease)
adapter.heartbeat(lease)
stages = ("format",)
adapter.record_checkpoint(
    lease,
    CheckpointBundle(
        schema_version=CHECKPOINT_SCHEMA,
        feature_id=lease.feature_id,
        repository_id=lease.repository_id,
        base_sha=lease.base_sha,
        head_sha=lease.base_sha,
        changed_files=("README.md",),
        acceptance_progress=stages,
        test_results={{stage: 0 for stage in stages}},
        toolchain_ref=lease.toolchain_ref,
        toolchain_manifest_sha256="e" * 64,
        patch="diff-after-1-stages",
    ),
    sequence=1,
)
print(json.dumps({{"claimed": True, "job_id": lease.job_id,
                  "lease_epoch": lease.lease_epoch,
                  "feature_id": lease.feature_id}}), flush=True)
# Hold the lease and wait to be killed: this is the crash the scenario needs.
while True:
    import time; time.sleep(1)
'''


def run() -> int:
    report = Report()
    root = Path(tempfile.mkdtemp(prefix="dal-r06-"))
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

        # The two sides share no filesystem state: the worker's directory holds
        # its token cache and checkpoints, and it has no path to the database.
        settings = worker_settings(root, endpoint, cert, worker_id="worker-acceptance-1")
        report.record(
            "worker holds no database path",
            not any("workflow.db" in str(v) for v in settings.__dict__.values()),
        )

        job_id = queue.enqueue_job(
            engine,
            feature_id="acceptance",
            repository_id="synthetic",
            base_sha=BASE_SHA,
            branch_name="codex/feature-acceptance",
            toolchain_ref="toolchain-v1",
        )
        second_job = queue.enqueue_job(
            engine,
            feature_id="cancelme",
            repository_id="synthetic",
            base_sha=BASE_SHA,
            branch_name="codex/feature-cancelme",
            toolchain_ref="toolchain-v1",
        )

        # --- a separate worker process claims, heartbeats, checkpoints -------
        spec = {
            "endpoint": endpoint,
            "worker_id": settings.worker_id,
            "enrollment_secret_path": str(settings.enrollment_secret_path),
            "token_cache_path": str(settings.token_cache_path),
            "checkpoint_root": str(settings.checkpoint_root),
            "ca_bundle_path": str(cert),
        }
        worker_process = subprocess.Popen(
            [sys.executable, "-c", WORKER_STEP.format(src=str(REPO_ROOT / "src"))],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        worker_process.stdin.write(json.dumps(spec))
        worker_process.stdin.close()
        first = json.loads(worker_process.stdout.readline() or "{}")
        report.record("worker process claimed a job", bool(first.get("claimed")), str(first))
        if not first.get("claimed"):
            return 1
        report.record(
            "claim carried feature_id and matched the enqueued job",
            first.get("job_id") == job_id and first.get("feature_id") == "acceptance",
        )
        report.record(
            "token cache is owner-only on the worker side",
            oct(settings.token_cache_path.stat().st_mode & 0o777) == "0o600",
        )
        first_epoch = first["lease_epoch"]
        checkpoints = _checkpoint_rows(engine, job_id)
        report.record("checkpoint v1 recorded by the authority", len(checkpoints) == 1)

        # --- SIGKILL the worker; the ECS side reclaims ----------------------
        worker_process.kill()
        worker_process.wait(timeout=10)
        report.record("worker process killed", worker_process.poll() is not None)

        time.sleep(LEASE_TTL_SECONDS + 1)
        adapter = RemoteHttpAdapter(
            RemoteTransportSettings(**{**settings.__dict__, "worker_id": "worker-acceptance-2"})
        )
        relaimed_lease = adapter.claim()
        report.record(
            "a second worker reclaimed the job after the lease expired",
            relaimed_lease is not None and relaimed_lease.job_id == job_id,
        )
        if relaimed_lease is None:
            return 1
        report.record(
            "the reclaimed lease carries a newer epoch (fencing token advanced)",
            relaimed_lease.lease_epoch > first_epoch,
            f"{first_epoch} -> {relaimed_lease.lease_epoch}",
        )

        stale_settings = RemoteTransportSettings(
            **{**settings.__dict__, "worker_id": "worker-acceptance-1"}
        )
        stale_adapter = RemoteHttpAdapter(stale_settings)
        stale_lease = relaimed_lease.__class__(
            **{**relaimed_lease.__dict__, "lease_epoch": first_epoch}
        )
        report.record(
            "the killed worker's old epoch can no longer heartbeat",
            not stale_adapter.heartbeat(stale_lease).alive,
        )
        stale_checkpoint = stale_adapter.record_checkpoint(
            stale_lease, bundle("acceptance", sequence=2), sequence=2
        )
        report.record(
            "the killed worker's old epoch can no longer checkpoint",
            stale_checkpoint.recorded is False and stale_checkpoint.stale is True,
        )

        # --- resume: checkpoint v2, then the result -------------------------
        second = adapter.record_checkpoint(
            relaimed_lease, bundle("acceptance", sequence=2), sequence=2
        )
        report.record("checkpoint v2 recorded after recovery", second.recorded)
        report.record(
            "recovery evidence lives on the authority, not only on the worker",
            len(_checkpoint_rows(engine, job_id)) == 2,
        )

        result = adapter.submit_result(
            relaimed_lease, state="succeeded", result_sha256=RESULT_SHA, last_error=None
        )
        report.record(
            "result accepted with a receipt", result.accepted and bool(result.receipt_id)
        )
        replay = adapter.submit_result(
            relaimed_lease, state="succeeded", result_sha256=RESULT_SHA, last_error=None
        )
        report.record(
            "an identical result replays to the same receipt",
            replay.accepted and replay.replay and replay.receipt_id == result.receipt_id,
        )
        conflict = adapter.submit_result(
            relaimed_lease,
            state="succeeded",
            result_sha256=CONFLICTING_SHA,
            last_error=None,
        )
        stored = queue.get_job(engine, job_id=job_id)
        report.record(
            "a conflicting digest is refused and does not overwrite",
            conflict.conflict and stored.result_sha256 == RESULT_SHA,
        )

        # --- cancel is observed within one heartbeat ------------------------
        cancel_lease = adapter.claim()
        report.record(
            "the second job was claimed", cancel_lease is not None
            and cancel_lease.job_id == second_job,
        )
        if cancel_lease is None:
            return 1
        queue.cancel_job(engine, job_id=second_job)
        observed = adapter.heartbeat(cancel_lease)
        report.record(
            "cancel is visible to the worker on its next heartbeat",
            observed.cancel_requested and not observed.alive,
        )
        after_cancel = adapter.submit_result(
            cancel_lease, state="succeeded", result_sha256=RESULT_SHA, last_error=None
        )
        report.record(
            "a cancelled job cannot be completed by the worker",
            after_cancel.accepted is False and after_cancel.cancelled is True,
        )

        # --- the pin is real: an untrusted certificate ends the run ---------
        untrusted_dir = root / "untrusted"
        untrusted_dir.mkdir()
        other_cert, _ = make_tls_material(untrusted_dir)
        pinned_elsewhere = RemoteHttpAdapter(
            RemoteTransportSettings(**{**settings.__dict__, "ca_bundle_path": other_cert})
        )
        try:
            pinned_elsewhere.claim()
            report.record("an untrusted certificate is refused", False, "claim succeeded")
        except TransportError as error:
            report.record(
                "an untrusted certificate is refused", error.reason.startswith("network:")
            )
        finally:
            pinned_elsewhere.close()

        adapter.close()
        stale_adapter.close()
        engine.dispose()

        print()
        print(f"{len(report.checks) - report.failed} PASS / {report.failed} FAIL")
        print("scope: same-machine, two processes, transport only —")
        print("       not cross-machine acceptance, and no coder or toolchain ran.")
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


def _checkpoint_rows(engine, job_id: str) -> list[str]:
    from sqlalchemy import select

    from personal_agent_dal.storage.engine import session_factory
    from personal_agent_dal.storage.transport_models import WorkerCheckpoint

    sessions = session_factory(engine)
    with sessions() as session:
        return list(
            session.execute(
                select(WorkerCheckpoint.checkpoint_id).where(
                    WorkerCheckpoint.job_id == job_id
                )
            ).scalars()
        )


if __name__ == "__main__":
    raise SystemExit(run())
