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
from typing import Final, NoReturn

from personal_agent_core.manifest import sha256_of
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.worker import checkpoint as checkpoint_mod
from personal_agent_dal.worker.checkpoint import CheckpointBundle
from personal_agent_dal.worker.config import WorkerConfig
from personal_agent_dal.worker.fixture_coder import apply_fixture_change
from personal_agent_dal.worker.transport import (
    JobLease,
    TransportDisabledError,
    TransportError,
    WorkerTransport,
)
from personal_agent_dal.worker.toolchain import (
    STAGES,
    LeaseLostError,
    SandboxUnavailableError,
    execute_toolchain,
    load_toolchain_manifest,
)
from personal_agent_dal.worker.verification import CHECK_STAGES, run_verification

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
    result_sha256: str
    last_error: str | None


class _Abandoned(Exception):
    """The job must be left to the authority's reclaim, not terminal-reported.

    A transient transport failure, a lost lease, a cancel, or a kill switch is
    not a fact about the job's content — it is a reason the worker stopped. A
    terminal `failed` here would burn the attempt budget on a single network
    blip and bypass the recovery path that already exists (lease expiry ->
    reclaim -> attempt+1 -> retry). Only deterministic refusals may go terminal.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def run_poll_once(
    transport: WorkerTransport, config: WorkerConfig, *, now: datetime | None = None
) -> PollOutcome:
    """Run one full poll cycle and return a bounded description of what happened.

    The cycle talks only to `transport`, so the same composition runs against the
    local SQLite queue and against the remote Dev Workflow Service. Whichever it
    is, the authority on the other side decides: this function never treats its
    own success as the job's outcome.
    """
    now = now or utc_now()
    if config.kill_switch_path.exists():
        return PollOutcome(
            claimed=False,
            job_id=None,
            state=None,
            error="kill_switch_active",
            reclaimed=(),
        )
    try:
        reclaimed = transport.reclaim_expired()
        lease = transport.claim()
    except TransportDisabledError as error:
        return PollOutcome(
            claimed=False, job_id=None, state=None, error=error.reason, reclaimed=()
        )
    except TransportError as error:
        return PollOutcome(
            claimed=error.job_id is not None,
            job_id=error.job_id,
            state=None,
            error=error.reason,
            reclaimed=(),
        )
    if lease is None:
        return PollOutcome(
            claimed=False, job_id=None, state=None, error=None, reclaimed=reclaimed
        )

    try:
        result = _execute_job(transport, config, lease, now=now)
    except _Abandoned as abandoned:
        # Not a terminal result: leave the lease active and let the authority
        # reclaim it, so the job retries instead of dying to a transient blip.
        return PollOutcome(
            claimed=True,
            job_id=lease.job_id,
            state=None,
            error=abandoned.reason,
            reclaimed=reclaimed,
        )
    except Exception as error:  # noqa: BLE001 - fail closed upward, bounded message
        reason = f"unexpected:{type(error).__name__}"
        result = _ExecutionResult(
            state="failed",
            result_sha256=_refusal_digest(lease, reason),
            last_error=reason,
        )

    try:
        submitted = transport.submit_result(
            lease,
            state=result.state,
            result_sha256=result.result_sha256,
            last_error=result.last_error,
        )
    except TransportError as error:
        return PollOutcome(
            claimed=True,
            job_id=lease.job_id,
            state=None,
            error=error.reason,
            reclaimed=reclaimed,
        )
    if submitted.conflict:
        return PollOutcome(
            claimed=True,
            job_id=lease.job_id,
            state=None,
            error="result_conflict",
            reclaimed=reclaimed,
        )
    if not submitted.accepted:
        return PollOutcome(
            claimed=True,
            job_id=lease.job_id,
            state=None,
            error="cancelled" if submitted.cancelled else "lease_lost",
            reclaimed=reclaimed,
        )
    return PollOutcome(
        claimed=True,
        job_id=lease.job_id,
        state=result.state,
        error=result.last_error,
        reclaimed=reclaimed,
    )


def _stage_exit_code(value: object) -> int:
    """Normalise a verification report exit code to an int (None/odd -> 1)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _refusal_digest(lease: JobLease, reason: str) -> str:
    """The content-addressed digest of a refusal, so it can be reported at all.

    Every terminal state a worker reports carries a digest: the transport
    contract requires one, and without it a deterministic refusal
    (`repo_not_allowlisted` will never succeed on this worker) could only be
    expressed as a timeout, which loses the diagnosis and burns the whole
    attempt budget rediscovering it.

    This is not an execution result wearing a different hat. Its field set is
    disjoint from the executed digest's — it carries `outcome`/`reason` and no
    `toolchain_manifest_sha256`/`checkpoint_sha256` — so the two can never
    collide, and a refusal replays to the identical digest.
    """
    return sha256_of(
        {
            "schema": RESULT_SCHEMA,
            "job_id": lease.job_id,
            "feature_id": lease.feature_id,
            "repository_id": lease.repository_id,
            "base_sha": lease.base_sha,
            "branch_name": lease.branch_name,
            "toolchain_ref": lease.toolchain_ref,
            "outcome": "refused",
            "reason": reason,
        }
    )


def _execute_job(
    transport: WorkerTransport, config: WorkerConfig, lease: JobLease, *, now: datetime
) -> _ExecutionResult:
    """Resolve, isolate, run and checkpoint one claimed job.

    A pre-execution refusal (unknown repo, bad base SHA, worktree failure) is
    reported with a refusal digest rather than an execution digest, so the
    authority learns the actual category instead of watching the lease expire.
    """
    record = lease

    def _refuse(reason: str) -> _ExecutionResult:
        return _ExecutionResult("failed", _refusal_digest(lease, reason), reason)

    def _abandon(reason: str) -> NoReturn:
        raise _Abandoned(reason)

    entry = config.repos.get(record.repository_id)
    if entry is None:
        return _refuse("repo_not_allowlisted")

    repo_path = Path(entry.local_path)
    if not repo_path.is_dir():
        return _refuse("repo_path_missing")
    if not (repo_path / ".git").exists():
        return _refuse("repo_not_a_git_repo")

    if not _FEATURE_ID_RE.match(record.feature_id):
        return _refuse("invalid_feature_id")

    # The claim carries `feature_id` and `branch_name` independently; they must
    # agree, or a server-side naming change would silently relocate the worktree
    # and checkpoint directory this job resumes from.
    expected_branch = f"codex/feature-{record.feature_id}"
    if record.branch_name != expected_branch:
        return _refuse("invalid_branch_name")

    verify = _git(repo_path, "rev-parse", "--verify", f"{record.base_sha}^{{commit}}")
    if verify.returncode != 0:
        return _refuse("base_sha_not_found")

    config.worktree_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.worktree_root, 0o700)
    worktree_path = config.worktree_root / f"feature-{record.feature_id}"
    if worktree_path.is_relative_to(repo_path) or repo_path.is_relative_to(
        config.worktree_root
    ):
        return _refuse("worktree_root_overlaps_repo")
    if worktree_path.is_symlink():
        return _refuse("worktree_symlink")
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
            # A prior worker may have created the branch and then crashed before
            # recording a result (branch names are repo-global, worktree paths
            # are worker-local). Attach a worktree to the existing branch, whose
            # HEAD is still `base_sha` because the worker never commits.
            add = _git(
                repo_path,
                "worktree",
                "add",
                str(worktree_path),
                record.branch_name,
            )
            if add.returncode != 0:
                return _refuse("worktree_add_failed")

    worktree_error = _validate_worktree(
        repo_path, worktree_path, base_sha=record.base_sha, branch_name=record.branch_name
    )
    if worktree_error is not None:
        return _refuse(worktree_error)

    started = transport.mark_running(lease)
    if not started.alive:
        _abandon("cancelled" if started.cancel_requested else "lease_lost")

    try:
        manifest = load_toolchain_manifest(worktree_path)
    except ValueError:
        return _refuse("toolchain_manifest_invalid")
    if record.toolchain_ref != manifest.toolchain_ref:
        return _refuse("toolchain_ref_mismatch")

    checkpoint, checkpoint_error = _restore_checkpoint(
        config.checkpoint_root,
        worktree_path,
        record,
        manifest.manifest_sha256,
    )
    if checkpoint_error is not None:
        return _refuse(checkpoint_error)

    if manifest.fixture_coder is not None:
        # DAL-R07A fixture slice: a deterministic no-model coder writes one
        # tracked file, then the deterministic verification runs the diff and
        # the four check stages. There is no provider stream and no per-stage
        # checkpoint loop — the slice is one atomic verification + checkpoint.
        if manifest.registry is None:
            return _refuse("fixture_registry_missing")
        try:
            apply_fixture_change(
                worktree_path, manifest.fixture_coder, record.feature_id
            )
        except ValueError:
            return _refuse("fixture_path_invalid")
        # The verification contract wants all five registered stages declared
        # (diff + the four checks). The check stages' execution source is the
        # pinned manifest, so their declared argv comes from it; the diff is the
        # only stage the manifest's `registry` must name.
        registry_commands = {
            "diff": manifest.registry["diff"],
            **{stage: manifest.stages[stage].command for stage in STAGES},
        }
        try:
            evaluation = run_verification(
                repo_path=worktree_path,
                base_sha=record.base_sha,
                registry_commands=registry_commands,
                manifest=manifest,
                entity_id=record.feature_id,
                # The worktree's `.git` file points back into the main repo; git
                # needs to read both to resolve HEAD and take a diff, so both are
                # read-only roots for the sandboxed capture (same as the toolchain
                # loop's `read_only_paths`).
                read_only_paths=(repo_path, worktree_path / ".git"),
            )
        except SandboxUnavailableError:
            _abandon("sandbox_unavailable")
        except TransportError as error:
            _abandon(error.reason)

        completed = list(CHECK_STAGES)
        test_results = {
            stage: _stage_exit_code(evaluation.report.exit_codes.get(stage))
            for stage in CHECK_STAGES
        }
        checkpoint = _build_checkpoint(
            worktree_path, record, manifest.manifest_sha256, completed, test_results
        )
        try:
            recorded = transport.record_checkpoint(
                lease, checkpoint, sequence=len(completed)
            )
        except TransportError as error:
            _abandon(error.reason)
        if not recorded.recorded:
            if recorded.conflict:
                return _refuse("checkpoint_conflict")
            _abandon("checkpoint_rejected")

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
                # The verification report is the authority on whether the diff
                # is a real, non-empty, correctly-based change; bind the result
                # to it, not only to the patch bytes.
                "verification_report_hash": evaluation.report.report_hash,
            }
        )
        if evaluation.result_status == "succeeded":
            return _ExecutionResult("succeeded", result_sha256, None)
        return _ExecutionResult(
            "failed", result_sha256, f"verification_{evaluation.result_status}"
        )

    completed = list(checkpoint.acceptance_progress) if checkpoint else []
    test_results = dict(checkpoint.test_results) if checkpoint else {}
    if checkpoint is None and _changed_files(worktree_path):
        return _refuse("worktree_dirty_without_checkpoint")

    guard_failure = "lease_lost"

    def _lease_guard() -> bool:
        nonlocal guard_failure
        if config.kill_switch_path.exists():
            guard_failure = "kill_switch_active"
            return False
        outcome = transport.heartbeat(lease)
        if outcome.cancel_requested:
            guard_failure = "cancelled"
        return outcome.alive

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
                    # Whatever this transport keeps on disk — the workflow
                    # database locally, the enrollment secret and token cache
                    # remotely — is off limits to anything the toolchain runs.
                    *config.transport.protected_paths(),
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
            # `sequence` is the count of stages this checkpoint covers, so a
            # replay of the same stage carries the same sequence and the
            # authority can tell a retry from progress.
            recorded = transport.record_checkpoint(
                lease, checkpoint, sequence=len(completed)
            )
            if not recorded.recorded:
                # The authority refused the resume point. Continuing would build
                # work that no recovery could ever find. A conflict is a real
                # integrity failure (terminal); a stale lease is a lost fence
                # (reclaim, not terminal).
                if recorded.conflict:
                    return _refuse("checkpoint_conflict")
                _abandon("checkpoint_rejected")
    except LeaseLostError:
        _abandon(guard_failure)
    except SandboxUnavailableError:
        _abandon("sandbox_unavailable")
    except TransportError as error:
        _abandon(error.reason)

    if checkpoint is None:
        return _refuse("checkpoint_missing")

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
    record: JobLease,
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
    record: JobLease,
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
