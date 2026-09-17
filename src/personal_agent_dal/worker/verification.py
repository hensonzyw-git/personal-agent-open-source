"""Deterministic verification runner (DAL-029 runnable layer).

`machine/verification_contract.py` is the decision; this module is the thin I/O
that gathers the evidence the decision needs. It captures the git diff (the
declared `diff` command), runs the four check stages through the existing
`execute_toolchain`, assembles the untrusted `injected_results`, and hands the
whole envelope to the pure `consume_verification`. It performs no classification
and no persistence: the machine classifier owns the verdict, and this module
only records what actually ran.

Every subprocess here — the `git rev-parse HEAD` baseline and the `git diff`
capture — runs through `run_sandboxed_command`, so the diff capture gets the
same default-deny sandbox, credential-free environment and output bound as the
toolchain stages; no bare `subprocess.run` escapes the boundary. The baseline is
read before *and* after the diff and the two must agree, so a concurrent HEAD
move during the capture fails closed (the classifier then refuses the diff as
unbound) instead of silently binding a patch to the wrong base.

Commands come only from the declared `registry_commands` (for the diff) and the
pinned toolchain manifest (for the checks) — never from issue text, a model, or
the environment. The check-stage observed command is read from the `StageResult`
(the argv actually executed), never re-read from the manifest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine.verification_contract import (
    COMMAND_TYPE,
    CONTRACT_VERSION,
    EVIDENCE_SOURCE,
    OPERATION_SPEC_ID,
    SERVICE_ACTOR,
    VerificationEvaluation,
    consume_verification,
)
from personal_agent_dal.worker.toolchain import (
    ToolchainManifest,
    execute_toolchain,
    run_sandboxed_command,
)

CHECK_STAGES: Final[tuple[str, ...]] = ("format", "lint", "build", "test")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _head_sha(
    repo_path: Path, *, read_only_paths: tuple[Path, ...] = ()
) -> str:
    """The worktree HEAD, read under the sandbox. Reported as the diff base so
    the classifier can refuse a diff that was not taken against the requested
    base. A failed `rev-parse` returns an empty/non-sha string, which the
    classifier's binding check refuses."""
    output, _code = run_sandboxed_command(
        ("git", "rev-parse", "HEAD"),
        repo_path,
        read_only_paths=read_only_paths,
        capture_stderr=False,
    )
    return output.strip()


def _capture_diff(
    repo_path: Path,
    diff_command: tuple[str, ...],
    *,
    read_only_paths: tuple[Path, ...] = (),
) -> tuple[str, int]:
    """Run the declared diff command under the sandbox and return
    `(bounded_output, exit_code)`. stderr is discarded (not merged): git through
    the Xcode shim under the sandbox emits timestamped diagnostics on stderr that
    would corrupt the diff bytes and break replay. A failed `git diff` is still
    refused by the classifier's diff-exit-code and empty-diff checks."""
    return run_sandboxed_command(
        diff_command, repo_path, read_only_paths=read_only_paths, capture_stderr=False
    )


def run_verification(
    *,
    repo_path: Path,
    base_sha: str,
    registry_commands: dict[str, tuple[str, ...]],
    manifest: ToolchainManifest,
    prior_last_verified_sha: str | None = None,
    entity_id: str = "feature",
    version: int = 0,
    operation_id: str = "verify-feature",
    read_only_paths: tuple[Path, ...] = (),
    lease_guard: Callable[[], bool] | None = None,
    heartbeat_interval_s: float = 5.0,
) -> VerificationEvaluation:
    """Capture the diff, run the checks, and consume the verification.

    The `diff` stage runs the declared `registry_commands["diff"]` command and
    records the patch plus its exit code; the four check stages run through
    `execute_toolchain` (commands from the pinned toolchain manifest). The
    observed commands and exit codes are handed to `consume_verification` as
    untrusted injected results, together with the trusted base SHA, the declared
    registry, and the prior last-verified SHA.
    """
    if not isinstance(registry_commands, dict) or "diff" not in registry_commands:
        raise DalError(
            DalErrorCode.INVALID_ARGUMENT,
            internal_detail="registry_commands must declare the diff stage",
        )
    diff_command = registry_commands["diff"]
    if (
        not isinstance(diff_command, (list, tuple))
        or not diff_command
        or not all(isinstance(token, str) and token for token in diff_command)
    ):
        raise DalError(
            DalErrorCode.INVALID_ARGUMENT,
            internal_detail="registry_commands['diff'] must be a non-empty argv",
        )
    diff_command = tuple(diff_command)

    # Read the baseline before and after the diff; a concurrent HEAD move during
    # the capture makes the two disagree, and we report an unbound base so the
    # classifier fails closed rather than binding the patch to the wrong SHA.
    head_before = _head_sha(repo_path, read_only_paths=read_only_paths)
    diff_text, diff_exit_code = _capture_diff(
        repo_path, diff_command, read_only_paths=read_only_paths
    )
    head_after = _head_sha(repo_path, read_only_paths=read_only_paths)
    diff_base_sha = head_before if head_before == head_after else ""

    toolchain = execute_toolchain(
        repo_path,
        manifest,
        stages=CHECK_STAGES,
        lease_guard=lease_guard,
        heartbeat_interval_s=heartbeat_interval_s,
    )
    stage_results: dict[str, dict[str, Any]] = {
        "diff": {"command": list(diff_command), "exit_code": diff_exit_code},
    }
    for stage_result in toolchain.stages:
        stage_results[stage_result.stage] = {
            "command": list(stage_result.command),
            "exit_code": stage_result.returncode,
        }

    command = {
        "operation_spec_id": OPERATION_SPEC_ID,
        "schema_version": "dal.test-operation-command/1.0",
        "operation_id": operation_id,
        "idempotency_key": f"verify:{entity_id}:{base_sha}",
        "actor_type": SERVICE_ACTOR,
        "evidence_source_type": EVIDENCE_SOURCE,
        "input": {
            "schema_version": "dal.operation-input/1.0",
            "target": {
                "entity_id": entity_id,
                "entity_type": "feature",
                "state": "verifying",
                "version": version,
            },
            "action_sequence": [
                {"command": COMMAND_TYPE, "contract_version": CONTRACT_VERSION}
            ],
            "authoritative_facts": {
                "base_sha": base_sha,
                "registry_commands": {k: list(v) for k, v in registry_commands.items()},
                "prior_last_verified_sha": prior_last_verified_sha,
            },
            "injected_results": {
                "diff": diff_text,
                "diff_sha": _sha256_text(diff_text),
                "diff_base_sha": diff_base_sha,
                "stage_results": stage_results,
            },
        },
    }
    return consume_verification(command)
