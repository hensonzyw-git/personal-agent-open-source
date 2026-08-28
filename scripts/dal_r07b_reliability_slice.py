#!/usr/bin/env python3
"""DAL-R07B live closure: multi-run reliability, timeout and cancel paths.

Runs under Henson's explicit 2026-08-28 authorisation: real `claude -p` via the
local CCR proxy (DeepSeek V4 Pro), temporary synthetic repos, allowed path
README.md only, no GitHub write. Three scenarios, each with its own service
process, database, repo and worker home:

1. reliability — N independent happy-path jobs (each a fresh enqueue + fresh
   worktree reset to base SHA); the check is how many reached `succeeded`
   with a non-empty tracked diff, reported per-run, no causal claim.
2. timeout — the manifest's `max_wall_seconds` is set far below the time a
   real run needs, so the launcher SIGKILLs the group and reports
   `timed_out`; the contract must classify `budget_limit` and the job must
   reach a terminal state (never a silent hang).
3. cancel — a normal job runs while the worker's kill switch is created
   mid-run; the lease guard must observe it, the worker must abandon (not
   write a terminal result), and the job must stay reclaimable — per Henson's
   R06 decision that cancel does not spend the attempt budget.

The DAL_CODER_TOKEN environment variable must hold the claude→CCR appkey.
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
import threading
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from personal_agent_dal.storage import db  # noqa: E402
from personal_agent_dal.storage.engine import create_database_engine  # noqa: E402
from personal_agent_dal.worker import queue  # noqa: E402

HOST = "127.0.0.1"
LEASE_TTL_SECONDS = 3
MAX_ATTEMPTS = 5
TOOLCHAIN_REF = ".personal-agent/toolchain.json"
RELIABILITY_RUNS = 3
#: A wall clock far below any real completion: the timeout scenario needs the
#: kill to fire, not a finished run.
TIMEOUT_WALL_SECONDS = 15


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
        capture_output=True, text=True,
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


def make_coder_repo(
    repo: Path,
    *,
    max_wall_seconds: int,
) -> str:
    """A synthetic repo whose manifest declares the real coder route."""
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / ".personal-agent").mkdir(parents=True)
    stages = {
        "format": {"cmd": ["true"]},
        "lint": {"cmd": ["true"]},
        "build": {"cmd": ["true"]},
        "test": {"cmd": ["true"]},
    }
    (repo / ".personal-agent" / "toolchain.json").write_text(
        json.dumps(
            {
                "schema_version": "dal.toolchain-manifest/1.0",
                "stages": stages,
                "coder": {
                    "model_alias": "dal-ccr-primary",
                    "allowed_tools": ["Read", "Edit", "Bash"],
                    "max_turns": 8,
                    "max_wall_seconds": max_wall_seconds,
                    "max_patch_bytes": 1048576,
                    "prompt": (
                        "Append exactly the line 'coder change for {feature_id}' to "
                        "README.md. Keep every other line unchanged."
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


def write_worker_config(
    path: Path,
    *,
    endpoint: str,
    ca_bundle: Path,
    worker_id: str,
    repo: Path,
    home: Path,
    enrollment_secret_path: Path,
    coder_token_path: Path,
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
                "coder_token_path": str(coder_token_path),
            }
        )
    )


def run_worker(config_path: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run one poll-once in its own process group; reclaim the whole tree.

    The coder child (claude -> CCR) spawns in the worker's process group, so
    an outer timeout that killed only the worker process would leave the
    model call running and burning the authorised call budget. Start the
    worker in a fresh session and, on timeout, TERM then KILL the entire
    group and reap it. The worker env drops DAL_CODER_TOKEN: the credential
    travels only through its 0600 token file, never through the process
    environment of the worker/service children.
    """
    worker_env = {
        key: value for key, value in os.environ.items() if key != "DAL_CODER_TOKEN"
    }
    process = subprocess.Popen(
        [
            sys.executable, "-m", "personal_agent_dal.worker.cli",
            "--config", str(config_path), "poll-once",
        ],
        cwd=REPO_ROOT,
        env={**worker_env, "PYTHONPATH": str(REPO_ROOT / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise SystemExit(
            f"worker exceeded the {timeout}s outer budget; process group "
            f"{process.pid} reaped — real call budget must be re-accounted"
        )
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def start_service(root: Path) -> tuple[subprocess.Popen, object, str, Path]:
    """One TLS service process over its own database; returns handles."""
    root.mkdir(parents=True)
    cert, key = make_tls_material(root)
    service_key = write_secret(root / "service.key", os.urandom(32).hex().encode())
    enrollment_secret = write_secret(root / "enrollment.secret", os.urandom(32).hex().encode())
    database = root / "workflow.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    port = free_port()
    endpoint = f"https://{HOST}:{port}"
    process = subprocess.Popen(
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
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key != "DAL_CODER_TOKEN"
            },
            "PYTHONPATH": str(REPO_ROOT / "src"),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if not wait_for_health(endpoint, cert, process):
        raise SystemExit("service did not come up")
    return process, engine, endpoint, enrollment_secret


def stop_service(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def reset_worktree(repo: Path, worktree: Path, base_sha: str) -> None:
    """Reset the worktree to base SHA so each run starts identical."""
    subprocess.run(["git", "-C", str(worktree), "checkout", "-q", "--", "."], check=False)
    subprocess.run(["git", "-C", str(worktree), "reset", "-q", "--hard", base_sha], check=False)


class Scenario:
    """One service + one repo + one worker home, reused across its runs."""

    def __init__(self, name: str, *, max_wall_seconds: int) -> None:
        self.name = name
        self.root = Path(tempfile.mkdtemp(prefix=f"dal-r07b-{name}-"))
        self.process, self.engine, self.endpoint, self.enrollment = None, None, None, None
        self.repo = self.root / "repo"
        self.max_wall_seconds = max_wall_seconds

    def setup(self) -> str:
        self.process, self.engine, self.endpoint, self.enrollment = start_service(
            self.root / "service"
        )
        return make_coder_repo(self.repo, max_wall_seconds=self.max_wall_seconds)

    def worker_config(self, run_index: int) -> tuple[Path, Path]:
        home = self.root / f"worker-{run_index}"
        config_path = self.root / f"worker-config-{run_index}.json"
        coder_token = self.root / "coder-token"
        coder_token.write_text(os.environ["DAL_CODER_TOKEN"] + "\n")
        os.chmod(coder_token, 0o600)
        write_worker_config(
            config_path,
            endpoint=self.endpoint,
            ca_bundle=self.root / "service" / "tls.crt",
            worker_id=f"worker-{run_index}",
            repo=self.repo,
            home=home,
            enrollment_secret_path=self.enrollment,
            coder_token_path=coder_token,
        )
        return config_path, home

    def cleanup(self) -> None:
        if self.process is not None:
            stop_service(self.process)
        if self.engine is not None:
            self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)


def scenario_reliability(report: Report, run_range: range | list[int]) -> None:
    """N independent happy-path runs; report per-run, no causal claim."""
    scenario = Scenario("reliability", max_wall_seconds=180)
    try:
        base_sha = scenario.setup()
        results: list[tuple[int, str, str]] = []
        for index in run_range:
            config_path, home = scenario.worker_config(index)
            # Distinct feature_id per run: branch names are repo-global, so
            # sharing one branch across workers would collide in `worktree add`.
            feature_id = f"live-{index}"
            job_id = queue.enqueue_job(
                scenario.engine,
                feature_id=feature_id,
                repository_id="synthetic",
                base_sha=base_sha,
                branch_name=f"codex/feature-{feature_id}",
                toolchain_ref=TOOLCHAIN_REF,
            )
            completed = run_worker(config_path, timeout=300)
            record = queue.get_job(scenario.engine, job_id=job_id)
            readme = home / "worktrees" / f"feature-{feature_id}" / "README.md"
            content = readme.read_text() if readme.is_file() else ""
            state = record.state
            detail = f"run {index}: state={state} rc={completed.returncode}"
            if state != "succeeded":
                detail += f" last_error={record.last_error}"
            results.append((index, state, detail))
            report.record(
                f"reliability run {index} reached succeeded",
                state == "succeeded" and f"coder change for {feature_id}" in content,
                detail,
            )
        succeeded = sum(1 for _, state, _ in results if state == "succeeded")
        report.record(
            f"reliability summary {succeeded}/{RELIABILITY_RUNS} succeeded",
            True,
            "single-machine sample; no causal or reliability claim is made "
            "beyond this run set (Eval结论边界模板 applies to any inference)",
        )
    finally:
        scenario.cleanup()


def scenario_timeout(report: Report) -> None:
    """A wall clock far below real completion must classify budget_limit."""
    scenario = Scenario("timeout", max_wall_seconds=TIMEOUT_WALL_SECONDS)
    try:
        base_sha = scenario.setup()
        config_path, home = scenario.worker_config(1)
        job_id = queue.enqueue_job(
            scenario.engine,
            feature_id="live",
            repository_id="synthetic",
            base_sha=base_sha,
            branch_name="codex/feature-live",
            toolchain_ref=TOOLCHAIN_REF,
        )
        started = time.monotonic()
        completed = run_worker(config_path, timeout=180)
        elapsed = time.monotonic() - started
        record = queue.get_job(scenario.engine, job_id=job_id)
        report.record(
            "timeout run terminates (no hang)",
            record.state in ("succeeded", "failed", "expired", "cancelled"),
            f"state={record.state} elapsed={elapsed:.0f}s "
            f"last_error={record.last_error}",
        )
        report.record(
            "timeout classifies budget_limit, not silent success",
            record.last_error is not None and "budget_limit" in record.last_error,
            f"last_error={record.last_error}",
        )
        report.record(
            "timeout run did not produce the change",
            "coder change for live"
            not in (home / "worktrees" / "feature-live" / "README.md").read_text(),
            "worktree README unchanged",
        )
    finally:
        scenario.cleanup()


def scenario_cancel(report: Report, kill_delay: int = 12) -> None:
    """Kill switch mid-run: abandon (lease stays), job stays reclaimable."""
    scenario = Scenario("cancel", max_wall_seconds=180)
    try:
        base_sha = scenario.setup()
        config_path, home = scenario.worker_config(1)
        job_id = queue.enqueue_job(
            scenario.engine,
            feature_id="live",
            repository_id="synthetic",
            base_sha=base_sha,
            branch_name="codex/feature-live",
            toolchain_ref=TOOLCHAIN_REF,
        )
        kill_switch = home / "worker.disabled"

        def _engage_kill_switch() -> None:
            time.sleep(kill_delay)  # the coder is mid-run by then
            kill_switch.write_text("stop")

        thread = threading.Thread(target=_engage_kill_switch)
        thread.start()
        completed = run_worker(config_path, timeout=300)
        thread.join()
        record = queue.get_job(scenario.engine, job_id=job_id)
        report.record(
            "cancel observed: worker abandons without a terminal write",
            record.state in ("leased", "running", "pending"),
            f"state={record.state} last_error={record.last_error}",
        )
        report.record(
            "attempt budget not spent on cancel (R06 decision)",
            record.attempt_count <= 1,
            f"attempt={record.attempt_count}",
        )
        # Reclaim: a fresh poll with the kill switch cleared claims the
        # abandoned job and completes it — cancel stops the attempt, not the
        # job. Reuse worker-1's config: the worktree path and branch belong to
        # the original worker, and _validate_worktree requires HEAD==base_sha
        # plus the branch name, so the dirty worktree must be reset first. The
        # abandoned lease stays active until its TTL lapses, so wait it out.
        kill_switch.unlink(missing_ok=True)
        time.sleep(LEASE_TTL_SECONDS + 2)
        reset_worktree(scenario.repo, home / "worktrees" / "feature-live", base_sha)
        completed2 = run_worker(config_path, timeout=300)
        record2 = queue.get_job(scenario.engine, job_id=job_id)
        content = (home / "worktrees" / "feature-live" / "README.md").read_text()
        report.record(
            "reclaim after cancel completes the job",
            record2.state == "succeeded" and "coder change for live" in content,
            f"state={record2.state} last_error={record2.last_error}",
        )
    finally:
        scenario.cleanup()


def run() -> int:
    token = os.environ.get("DAL_CODER_TOKEN")
    if not token:
        print("DAL_CODER_TOKEN is not set; export the claude→CCR appkey first", file=sys.stderr)
        return 1
    # Scenario selection for call-budget accounting: the 2026-08-28
    # authorisation bounds total real coder calls at 7 across all executions.
    # The first execution consumed 3 (reliability run 1, timeout kill, cancel
    # kill) before a script bug crashed the reclaim; this flag reruns only the
    # missing parts (--scenarios reliability,cancel --reliability-runs 2,3).
    scenarios = {"reliability", "timeout", "cancel"}
    run_range = range(1, RELIABILITY_RUNS + 1)
    if "--reliability-runs" in sys.argv:
        spec = sys.argv[sys.argv.index("--reliability-runs") + 1]
        run_range = [int(part) for part in spec.split(",")]
    if "--scenarios" in sys.argv:
        scenarios = set(sys.argv[sys.argv.index("--scenarios") + 1].split(","))
    kill_delay = 12
    if "--cancel-delay" in sys.argv:
        kill_delay = int(sys.argv[sys.argv.index("--cancel-delay") + 1])
    report = Report()
    print("scope: real claude -p via CCR (DeepSeek V4 Pro); temporary synthetic "
          f"repos; allowed path README.md; reliability runs {list(run_range)} + "
          f"scenarios {sorted(scenarios)}; no GitHub write.", flush=True)
    if "reliability" in scenarios:
        scenario_reliability(report, run_range)
    if "timeout" in scenarios:
        scenario_timeout(report)
    if "cancel" in scenarios:
        scenario_cancel(report, kill_delay=kill_delay)
    print()
    print(f"{len(report.checks) - report.failed} PASS / {report.failed} FAIL")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
