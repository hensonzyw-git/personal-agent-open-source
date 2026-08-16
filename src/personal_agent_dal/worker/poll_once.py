"""One poll cycle (DAL-017/018/019/020 runnable composition).

`run_poll_once` is the whole of one worker pass, and the only place the pieces
are composed. It reclaims expired leases, claims one pending job, then — against
an isolated worktree of the allowlisted repository at the job's base SHA — runs
the declared deterministic toolchain, writes a checkpoint and records an
idempotent result receipt.

It never pushes, merges or deploys; never runs a command that did not come from
the repo's pinned manifest; and never holds a transaction open across a
subprocess call (each `queue` primitive commits before the next step, per
CLAUDE.md §5.2).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from sqlalchemy import Engine

from personal_agent_core.manifest import sha256_of
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.worker import checkpoint as checkpoint_mod
from personal_agent_dal.worker import queue
from personal_agent_dal.worker.checkpoint import CheckpointBundle
from personal_agent_dal.worker.config import WorkerConfig
from personal_agent_dal.worker.toolchain import (
    STAGES,
    LeaseLostError,
    SandboxUnavailableError,
    execute_toolchain,
    load_toolchain_manifest,
)

RESULT_SCHEMA: Final[str] = "dal.worker-result/1.0"

#: Feature ids must be a single safe path segment. Anything else is refused
#: before it can reach a filesystem path or a branch name.
_FEATURE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_GIT_TIMEOUT: Final[int] = 60


@dataclass(frozen=True)
class PollOutcome:
    """What one poll cycle did."""

    claimed: bool
    job_id: str | None
    state: str | None
    error: str | None
    reclaimed: tuple[str, ...]


@dataclass(frozen=True)
class _ExecutionResult:
    state: str  # "succeeded" or "failed"
    result_sha256: str | None
    last_error: str | None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def run_poll_once(
    engine: Engine, config: WorkerConfig, *, now: datetime | None = None
) -> PollOutcome:
    """Run one full poll cycle and return a bounded description of what happened."""
    now = now or utc_now()
    if config.kill_switch_path.exists():
        return PollOutcome(
            claimed=False,
            job_id=None,
            state=None,
            error="kill_switch_active",
            reclaimed=(),
        )
    reclaimed = tuple(
        queue.reclaim_expired(engine, max_attempts=config.max_attempts, now=now)
    )

    job_id = queue.claim_job(
        engine,
        worker_id=config.worker_id,
        lease_ttl_seconds=config.lease_ttl_seconds,
        now=now,
    )
    if job_id is None:
        return PollOutcome(
            claimed=False, job_id=None, state=None, error=None, reclaimed=reclaimed
        )

    record = queue.get_job(engine, job_id=job_id)
    if record is None:
        return PollOutcome(
            claimed=True, job_id=job_id, state=None,
            error="job row missing", reclaimed=reclaimed,
        )

    try:
        result = _execute_job(engine, config, record, now=now)
    except Exception as error:  # noqa: BLE001 - fail closed upward, bounded message
        result = _ExecutionResult(
            state="failed",
            result_sha256=None,
            last_error=f"unexpected:{type(error).__name__}",
        )

    try:
        finished = queue.finish_job(
            engine,
            job_id=job_id,
            worker_id=config.worker_id,
            lease_epoch=record.lease_epoch,
            state=result.state,
            result_sha256=result.result_sha256,
            last_error=result.last_error,
            now=utc_now(),
        )
    except queue.ResultConflictError:
        return PollOutcome(
            claimed=True,
            job_id=job_id,
            state=None,
            error="result_conflict",
            reclaimed=reclaimed,
        )
    if not finished:
        return PollOutcome(
            claimed=True,
            job_id=job_id,
            state=None,
            error="lease_lost",
            reclaimed=reclaimed,
        )
    return PollOutcome(
        claimed=True,
        job_id=job_id,
        state=result.state,
        error=result.last_error,
        reclaimed=reclaimed,
    )


def _execute_job(
    engine: Engine, config: WorkerConfig, record: queue.JobRecord, *, now: datetime
) -> _ExecutionResult:
    """Resolve, isolate, run and checkpoint one claimed job.

    Returns `result_sha256` only when the deterministic toolchain actually ran —
    a pre-execution refusal (unknown repo, bad base SHA, worktree failure) has
    no result to receipt, only a bounded failure category.
    """
    entry = config.repos.get(record.repository_id)
    if entry is None:
        return _ExecutionResult("failed", None, "repo_not_allowlisted")

    repo_path = Path(entry.local_path)
    if not repo_path.is_dir():
        return _ExecutionResult("failed", None, "repo_path_missing")
    if not (repo_path / ".git").exists():
        return _ExecutionResult("failed", None, "repo_not_a_git_repo")

    if not _FEATURE_ID_RE.match(record.feature_id):
        return _ExecutionResult("failed", None, "invalid_feature_id")

    expected_branch = f"codex/feature-{record.feature_id}"
    if record.branch_name != expected_branch:
        return _ExecutionResult("failed", None, "invalid_branch_name")

    verify = _git(repo_path, "rev-parse", "--verify", f"{record.base_sha}^{{commit}}")
    if verify.returncode != 0:
        return _ExecutionResult("failed", None, "base_sha_not_found")

    config.worktree_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.worktree_root, 0o700)
    worktree_path = config.worktree_root / f"feature-{record.feature_id}"
    if worktree_path.is_relative_to(repo_path) or repo_path.is_relative_to(
        config.worktree_root
    ):
        return _ExecutionResult("failed", None, "worktree_root_overlaps_repo")
    if worktree_path.is_symlink():
        return _ExecutionResult("failed", None, "worktree_symlink")
    if not worktree_path.exists():
        add = _git(
            repo_path,
            "worktree",
            "add",
            str(worktree_path),
            "-b",
            record.branch_name,
            record.base_sha,
        )
        if add.returncode != 0:
            return _ExecutionResult("failed", None, "worktree_add_failed")

    worktree_error = _validate_worktree(
        repo_path, worktree_path, base_sha=record.base_sha, branch_name=record.branch_name
    )
    if worktree_error is not None:
        return _ExecutionResult("failed", None, worktree_error)

    if not queue.mark_running(
        engine,
        job_id=record.job_id,
        worker_id=config.worker_id,
        lease_epoch=record.lease_epoch,
        now=now,
    ):
        return _ExecutionResult("failed", None, "lease_lost")

    try:
        manifest = load_toolchain_manifest(worktree_path)
    except ValueError:
        return _ExecutionResult("failed", None, "toolchain_manifest_invalid")
    if record.toolchain_ref != manifest.toolchain_ref:
        return _ExecutionResult("failed", None, "toolchain_ref_mismatch")

    checkpoint, checkpoint_error = _restore_checkpoint(
        config.checkpoint_root,
        worktree_path,
        record,
        manifest.manifest_sha256,
    )
    if checkpoint_error is not None:
        return _ExecutionResult("failed", None, checkpoint_error)

    completed = list(checkpoint.acceptance_progress) if checkpoint else []
    test_results = dict(checkpoint.test_results) if checkpoint else {}
    if checkpoint is None and _changed_files(worktree_path):
        return _ExecutionResult("failed", None, "worktree_dirty_without_checkpoint")

    guard_failure = "lease_lost"

    def _lease_guard() -> bool:
        nonlocal guard_failure
        if config.kill_switch_path.exists():
            guard_failure = "kill_switch_active"
            return False
        return queue.heartbeat(
            engine,
            job_id=record.job_id,
            worker_id=config.worker_id,
            lease_epoch=record.lease_epoch,
            lease_ttl_seconds=config.lease_ttl_seconds,
            now=utc_now(),
        )

    try:
        sibling_worktrees = tuple(
            path for path in config.worktree_root.iterdir() if path != worktree_path
        )
        for stage in STAGES[len(completed) :]:
            stage_result = execute_toolchain(
                worktree_path,
                manifest,
                stages=(stage,),
                lease_guard=_lease_guard,
                forbidden_paths=(
                    config.database_path,
                    config.checkpoint_root,
                    config.kill_switch_path,
                    *sibling_worktrees,
                ),
                read_only_paths=(repo_path, worktree_path / ".git"),
                heartbeat_interval_s=max(
                    0.25, min(5.0, config.lease_ttl_seconds / 3)
                ),
            ).stages[0]
            test_results[stage] = stage_result.returncode
            completed.append(stage)
            checkpoint = _build_checkpoint(
                worktree_path, record, manifest.manifest_sha256, completed, test_results
            )
            checkpoint_mod.write_checkpoint(config.checkpoint_root, checkpoint)
    except LeaseLostError:
        return _ExecutionResult("failed", None, guard_failure)
    except SandboxUnavailableError:
        return _ExecutionResult("failed", None, "sandbox_unavailable")

    if checkpoint is None:
        return _ExecutionResult("failed", None, "checkpoint_missing")

    result_sha256 = sha256_of(
        {
            "schema": RESULT_SCHEMA,
            "job_id": record.job_id,
            "feature_id": record.feature_id,
            "repository_id": record.repository_id,
            "base_sha": record.base_sha,
            "branch_name": record.branch_name,
            "toolchain_ref": record.toolchain_ref,
            "toolchain_manifest_sha256": manifest.manifest_sha256,
            "checkpoint_sha256": checkpoint_mod.checkpoint_sha256(checkpoint),
        }
    )

    if all(test_results[stage] == 0 for stage in STAGES):
        return _ExecutionResult("succeeded", result_sha256, None)
    return _ExecutionResult("failed", result_sha256, "toolchain_failed")


def _validate_worktree(
    repo_path: Path, worktree_path: Path, *, base_sha: str, branch_name: str
) -> str | None:
    """Prove an existing path is the exact registered worktree requested."""
    if worktree_path.is_symlink() or not worktree_path.is_dir():
        return "worktree_not_directory"
    git_marker = worktree_path / ".git"
    if git_marker.is_symlink() or not git_marker.exists():
        return "worktree_not_registered"
    top = _git(worktree_path, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != worktree_path.resolve():
        return "worktree_root_mismatch"
    repo_common = _git(repo_path, "rev-parse", "--git-common-dir")
    worktree_common = _git(worktree_path, "rev-parse", "--git-common-dir")
    if repo_common.returncode != 0 or worktree_common.returncode != 0:
        return "worktree_repo_unverified"

    def _git_path(root: Path, value: str) -> Path:
        candidate = Path(value.strip())
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    if _git_path(repo_path, repo_common.stdout) != _git_path(
        worktree_path, worktree_common.stdout
    ):
        return "worktree_repo_mismatch"
    head = _git(worktree_path, "rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != base_sha:
        return "worktree_head_mismatch"
    branch = _git(worktree_path, "symbolic-ref", "--short", "HEAD")
    if branch.returncode != 0 or branch.stdout.strip() != branch_name:
        return "worktree_branch_mismatch"
    return None


def _build_checkpoint(
    worktree_path: Path,
    record: queue.JobRecord,
    manifest_sha256: str,
    completed: list[str],
    test_results: dict[str, int],
) -> CheckpointBundle:
    head_proc = _git(worktree_path, "rev-parse", "HEAD")
    if head_proc.returncode != 0:
        raise ValueError("worktree head unavailable")
    patch = _worktree_patch(worktree_path)
    if patch is None:
        raise ValueError("worktree patch unavailable")
    return CheckpointBundle(
        schema_version=checkpoint_mod.CHECKPOINT_SCHEMA,
        feature_id=record.feature_id,
        repository_id=record.repository_id,
        base_sha=record.base_sha,
        head_sha=head_proc.stdout.strip(),
        changed_files=tuple(sorted(_changed_files(worktree_path))),
        acceptance_progress=tuple(completed),
        test_results=dict(test_results),
        toolchain_ref=record.toolchain_ref,
        toolchain_manifest_sha256=manifest_sha256,
        patch=patch,
    )


def _restore_checkpoint(
    checkpoint_root: Path,
    worktree_path: Path,
    record: queue.JobRecord,
    manifest_sha256: str,
) -> tuple[CheckpointBundle | None, str | None]:
    try:
        bundle = checkpoint_mod.load_checkpoint(checkpoint_root, record.feature_id)
    except (OSError, ValueError, TypeError):
        return None, "checkpoint_invalid"
    if bundle is None:
        return None, None
    if (
        bundle.feature_id != record.feature_id
        or bundle.repository_id != record.repository_id
        or bundle.base_sha != record.base_sha
        or bundle.head_sha != record.base_sha
        or bundle.toolchain_ref != record.toolchain_ref
        or bundle.toolchain_manifest_sha256 != manifest_sha256
    ):
        return None, "checkpoint_binding_mismatch"
    completed = tuple(bundle.acceptance_progress)
    if completed != STAGES[: len(completed)] or set(bundle.test_results) != set(completed):
        return None, "checkpoint_progress_invalid"
    current_patch = _worktree_patch(worktree_path)
    if current_patch is None:
        return None, "checkpoint_worktree_unreadable"
    if current_patch == bundle.patch:
        return bundle, None
    if current_patch or not bundle.patch:
        return None, "checkpoint_patch_mismatch"
    check = subprocess.run(
        ["git", "-C", str(worktree_path), "apply", "--check", "--binary", "-"],
        input=bundle.patch,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if check.returncode != 0:
        return None, "checkpoint_patch_invalid"
    apply = subprocess.run(
        ["git", "-C", str(worktree_path), "apply", "--binary", "-"],
        input=bundle.patch,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if apply.returncode != 0:
        return None, "checkpoint_restore_failed"
    return bundle, None


def _worktree_patch(worktree_path: Path) -> str | None:
    """Return a binary patch covering tracked and untracked worktree content."""
    tracked = _git(worktree_path, "diff", "--binary", "--no-ext-diff", "HEAD")
    if tracked.returncode != 0:
        return None
    untracked = _git(worktree_path, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked.returncode != 0:
        return None
    pieces = [tracked.stdout]
    for relative in filter(None, untracked.stdout.split("\0")):
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            return None
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "diff",
                "--binary",
                "--no-index",
                "--",
                "/dev/null",
                relative,
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode not in (0, 1):
            return None
        pieces.append(proc.stdout)
    return "".join(pieces)


def _changed_files(worktree_path: Path) -> list[str]:
    """Parse ``git status --porcelain`` into a list of changed paths."""
    proc = _git(worktree_path, "status", "--porcelain")
    if proc.returncode != 0:
        return []
    files: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 3:
            continue
        files.append(line[3:])
    return files
