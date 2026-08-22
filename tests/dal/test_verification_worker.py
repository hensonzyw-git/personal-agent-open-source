"""DAL-029: deterministic verification runner I/O contract.

`machine/verification_contract.py` owns the verdict; `worker/verification.py`
only gathers evidence — capture the git diff, run the four check stages through
the existing `execute_toolchain`, and hand the whole envelope to the pure
`consume_verification`. These tests pin what the runner *builds* (the observed
commands must equal the declared registry, the diff base must be the worktree
HEAD, the idempotency key must bind `entity_id` + `base_sha`) and prove the two
verdict paths — a green run advances `last_verified_sha`, a failed stage reports
`blocked` / `task_failure` and preserves it. No real subprocess is spawned:
`_head_sha`, `_capture_diff` and `execute_toolchain` are mocked.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from personal_agent_dal.machine.verification_contract import CHECK_STAGES
from personal_agent_dal.worker import verification
from personal_agent_dal.worker.toolchain import (
    StageResult,
    StageSpec,
    ToolchainManifest,
    ToolchainResult,
)

BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
PRIOR_LAST_VERIFIED_SHA = "fedcba9876543210fedcba9876543210fedcba98"
REPO = Path("/tmp/run-worktree")
DIFF_TEXT = "diff --git a/x b/x\n--- a/x\n+++ b/x\n"


def _registry() -> dict[str, tuple[str, ...]]:
    return {
        "diff": ("git", "diff", "--binary", "--no-ext-diff", "HEAD"),
        "format": ("make", "format"),
        "lint": ("make", "lint"),
        "build": ("make", "build"),
        "test": ("make", "test"),
    }


def _manifest() -> ToolchainManifest:
    return ToolchainManifest(
        schema_version="dal.toolchain-manifest/1.0",
        toolchain_ref="repo-ref",
        manifest_sha256="0" * 64,
        stages={stage: StageSpec(command=("make", stage), timeout_s=120.0) for stage in CHECK_STAGES},
    )


def _install_fakes(
    monkeypatch, *, returncodes: dict[str, int], capture: dict
) -> None:
    """Install mocked HEAD, diff capture and toolchain; record what each saw."""

    monkeypatch.setattr(verification, "_head_sha", lambda repo_path: BASE_SHA)

    def fake_capture_diff(repo_path: Path, diff_command: tuple[str, ...]):
        capture["diff_command"] = diff_command
        return DIFF_TEXT, 0

    monkeypatch.setattr(verification, "_capture_diff", fake_capture_diff)

    def fake_execute_toolchain(repo_path: Path, manifest: ToolchainManifest, *, stages=(), **kwargs):
        capture["stages"] = tuple(stages)
        capture["manifest"] = manifest
        results = tuple(
            StageResult(
                stage=s,
                returncode=returncodes.get(s, 0),
                output=f"{s} ok",
                duration_s=0.1,
            )
            for s in stages
        )
        return ToolchainResult(stages=results)

    monkeypatch.setattr(verification, "execute_toolchain", fake_execute_toolchain)


def test_green_run_advances_last_verified_sha(monkeypatch) -> None:
    """A run whose four check stages all exit 0 advances the verified SHA."""
    capture: dict = {}
    _install_fakes(monkeypatch, returncodes={}, capture=capture)

    outcome = verification.run_verification(
        repo_path=REPO,
        base_sha=BASE_SHA,
        registry_commands=_registry(),
        manifest=_manifest(),
        prior_last_verified_sha=PRIOR_LAST_VERIFIED_SHA,
    )

    assert outcome.result_status == "succeeded"
    assert outcome.failure_class is None
    assert outcome.final_state == "verified"
    assert outcome.last_verified_sha == BASE_SHA
    assert outcome.declared_write_set == ("aggregate", "transition_receipt")
    assert outcome.event_trace == ()


def test_failed_stage_reports_task_failure_and_preserves_sha(monkeypatch) -> None:
    """A failed check stage reports `blocked`/`task_failure` and preserves the
    prior SHA — a model swap cannot fix a genuinely failing test."""
    capture: dict = {}
    _install_fakes(monkeypatch, returncodes={"test": 1}, capture=capture)

    outcome = verification.run_verification(
        repo_path=REPO,
        base_sha=BASE_SHA,
        registry_commands=_registry(),
        manifest=_manifest(),
        prior_last_verified_sha=PRIOR_LAST_VERIFIED_SHA,
    )

    assert outcome.result_status == "blocked"
    assert outcome.failure_class == "task_failure"
    assert outcome.final_state == "blocked_test"
    assert outcome.final_reason_code == "TEST_BLOCKED"
    assert outcome.last_verified_sha == PRIOR_LAST_VERIFIED_SHA


def test_observed_commands_match_declared_registry(monkeypatch) -> None:
    """Every stage command the runner records is the declared command — never a
    model-stitched argv. The check stages run through the manifest in fixed
    order, and the diff stage runs the declared `registry_commands["diff"]`."""
    capture: dict = {}
    _install_fakes(monkeypatch, returncodes={}, capture=capture)
    registry = _registry()

    outcome = verification.run_verification(
        repo_path=REPO,
        base_sha=BASE_SHA,
        registry_commands=registry,
        manifest=_manifest(),
    )

    assert capture["stages"] == CHECK_STAGES
    assert capture["diff_command"] == registry["diff"]
    assert outcome.report.commands == {
        "diff": ["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
        "format": ["make", "format"],
        "lint": ["make", "lint"],
        "build": ["make", "build"],
        "test": ["make", "test"],
    }
    assert outcome.report.exit_codes == {
        "diff": 0,
        "format": 0,
        "lint": 0,
        "build": 0,
        "test": 0,
    }


def test_envelope_binds_sha_and_idempotency_key(monkeypatch) -> None:
    """The assembled command carries the diff's true base as `diff_base_sha`
    (the worktree HEAD), a recomputed `diff_sha`, and an idempotency key that
    binds `entity_id` + `base_sha`."""
    capture: dict = {}
    _install_fakes(monkeypatch, returncodes={}, capture=capture)

    recorded: dict = {}
    real_consume = verification.consume_verification

    def recording_consume(command):
        recorded["command"] = command
        return real_consume(command)

    monkeypatch.setattr(verification, "consume_verification", recording_consume)

    verification.run_verification(
        repo_path=REPO,
        base_sha=BASE_SHA,
        registry_commands=_registry(),
        manifest=_manifest(),
    )

    injected = recorded["command"]["input"]["injected_results"]
    assert injected["diff_sha"] == hashlib.sha256(DIFF_TEXT.encode("utf-8")).hexdigest()
    assert injected["diff_base_sha"] == BASE_SHA
    assert recorded["command"]["idempotency_key"] == f"verify:feature:{BASE_SHA}"
    assert recorded["command"]["actor_type"] == "service"
    assert recorded["command"]["evidence_source_type"] == "verification-adapter"
