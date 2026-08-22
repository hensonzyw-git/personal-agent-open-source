"""Deterministic verification runner (DAL-029 runnable layer).

`machine/verification_contract.py` is the decision; this module is the thin I/O
that gathers the evidence the decision needs. It captures the git diff (the
declared `diff` command), runs the four check stages through the existing
`execute_toolchain`, assembles the untrusted `injected_results`, and hands the
whole envelope to the pure `consume_verification`. It performs no classification
and no persistence: the machine classifier owns the verdict, and this module
only records what actually ran.

The diff is captured against the worktree HEAD, and its true base is reported as
`diff_base_sha` so the classifier can refuse a diff not bound to the requested
`base_sha`. Commands come only from the declared `registry_commands` (for the
diff) and the pinned toolchain manifest (for the checks) — never from issue
text, a model, or the environment.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Final

from personal_agent_dal.machine.verification_contract import (
    COMMAND_TYPE,
    CONTRACT_VERSION,
    EVIDENCE_SOURCE,
    OPERATION_SPEC_ID,
    SERVICE_ACTOR,
    VerificationEvaluation,
    consume_verification,
)
from personal_agent_dal.worker.toolchain import ToolchainManifest, execute_toolchain

CHECK_STAGES: Final[tuple[str, ...]] = ("format", "lint", "build", "test")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _head_sha(repo_path: Path) -> str:
    """The worktree HEAD, reported as `diff_base_sha` so the classifier can
    refuse a diff that was not taken against the requested base."""
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return (process.stdout or "").strip()


def _capture_diff(repo_path: Path, diff_command: tuple[str, ...]) -> tuple[str, int]:
    """Run the declared diff command and return `(patch_text, exit_code)`."""
    process = subprocess.run(
        list(diff_command),
        cwd=str(repo_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process.stdout or "", process.returncode


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
) -> VerificationEvaluation:
    """Capture the diff, run the checks, and consume the verification.

    The `diff` stage runs the declared `registry_commands["diff"]` command and
    records the patch plus its exit code; the four check stages run through
    `execute_toolchain` (commands from the pinned toolchain manifest). The
    observed commands and exit codes are handed to `consume_verification` as
    untrusted injected results, together with the trusted base SHA, the declared
    registry, and the prior last-verified SHA.
    """
    diff_command = tuple(registry_commands["diff"])
    diff_text, diff_exit_code = _capture_diff(repo_path, diff_command)
    diff_base_sha = _head_sha(repo_path)

    toolchain = execute_toolchain(repo_path, manifest, stages=CHECK_STAGES)
    stage_results: dict[str, dict[str, Any]] = {
        "diff": {"command": list(diff_command), "exit_code": diff_exit_code},
    }
    for stage_result in toolchain.stages:
        stage_results[stage_result.stage] = {
            "command": list(manifest.stages[stage_result.stage].command),
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
