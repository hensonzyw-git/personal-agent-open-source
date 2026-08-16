"""Deterministic toolchain registry + executor (DAL-019 runnable layer).

`machine/injection.py` is the negative half — it refuses commands stitched
together from issue text or a model. This module is the positive half: the only
commands a worker will ever run are the ones a repository declares in its pinned
`.personal-agent/toolchain.json`. The manifest is loaded with a closed schema,
every stage has a bounded timeout, and every stage's output is truncated to a
bounded size — so neither a hostile repo nor an unbound subprocess can escape
the contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Iterable

from personal_agent_core.manifest import canonical_json

TOOLCHAIN_SCHEMA: Final[str] = "dal.toolchain-manifest/1.0"

#: The fixed, repo-checked-in manifest path. Never derived from issue text.
TOOLCHAIN_PATH: Final[str] = ".personal-agent/toolchain.json"

#: The stages, in the order a worker always runs them.
STAGES: Final[tuple[str, ...]] = ("format", "lint", "build", "test")

DEFAULT_TIMEOUT_S: Final[float] = 120.0
MAX_TIMEOUT_S: Final[float] = 600.0
MAX_OUTPUT_BYTES: Final[int] = 64 * 1024
SANDBOX_EXEC: Final[str] = "/usr/bin/sandbox-exec"
CHILD_PATH: Final[str] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

#: Return code recorded when a stage exceeds its timeout, matching the `timeout`
#: utility convention so a killed stage is distinguishable from an exit 0.
TIMEOUT_RETURNCODE: Final[int] = 124


@dataclass(frozen=True)
class StageSpec:
    """One declared stage: an argv tuple and a bounded timeout."""

    command: tuple[str, ...]
    timeout_s: float


@dataclass(frozen=True)
class ToolchainManifest:
    """The parsed, validated manifest. `stages` is keyed by stage name."""

    schema_version: str
    toolchain_ref: str
    manifest_sha256: str
    stages: dict[str, StageSpec]


@dataclass(frozen=True)
class StageResult:
    """The outcome of one stage. `output` is already truncated."""

    stage: str
    returncode: int
    output: str
    duration_s: float


@dataclass(frozen=True)
class ToolchainResult:
    """The outcome of a full toolchain run, in declaration order."""

    stages: tuple[StageResult, ...]

    @property
    def succeeded(self) -> bool:
        return all(stage.returncode == 0 for stage in self.stages)

    def returncodes(self) -> dict[str, int]:
        return {stage.stage: stage.returncode for stage in self.stages}


def _bounded(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore") + (
        "\n[toolchain: output truncated]"
    )


def load_toolchain_manifest(repo_path: Path) -> ToolchainManifest:
    """Load and validate the repo's pinned toolchain manifest.

    The schema is closed: unknown stages or fields are an error, a missing or
    non-string command is an error, and any timeout outside ``(0, MAX]`` is
    clamped to the ceiling rather than trusted. A manifest that does not parse
    raises rather than silently running nothing.
    """
    manifest_path = repo_path / TOOLCHAIN_PATH
    if not manifest_path.is_file():
        raise ValueError(f"toolchain manifest not found: {TOOLCHAIN_PATH}")
    body = json.loads(manifest_path.read_text("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("toolchain manifest must be an object")
    if body.get("schema_version") != TOOLCHAIN_SCHEMA:
        raise ValueError(
            f"unsupported toolchain schema: {body.get('schema_version')!r}"
        )
    raw_stages = body.get("stages")
    if not isinstance(raw_stages, dict):
        raise ValueError("toolchain manifest 'stages' must be an object")
    unknown_stages = set(raw_stages) - set(STAGES)
    if unknown_stages:
        raise ValueError(f"unknown toolchain stages: {sorted(unknown_stages)!r}")

    stages: dict[str, StageSpec] = {}
    for stage in STAGES:
        raw = raw_stages.get(stage)
        if raw is None:
            raise ValueError(f"toolchain manifest missing stage: {stage}")
        if not isinstance(raw, dict):
            raise ValueError(f"toolchain stage {stage!r} must be an object")
        unknown_fields = set(raw) - {"cmd", "timeout_s"}
        if unknown_fields:
            raise ValueError(
                f"unknown fields in toolchain stage {stage!r}: {sorted(unknown_fields)!r}"
            )
        command = raw.get("cmd")
        if not isinstance(command, list) or not command or not all(
            isinstance(token, str) and token for token in command
        ):
            raise ValueError(f"toolchain stage {stage!r} must declare a non-empty argv")
        timeout_s = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
            raise ValueError(f"toolchain stage {stage!r} timeout must be a number")
        if timeout_s <= 0 or timeout_s > MAX_TIMEOUT_S:
            timeout_s = MAX_TIMEOUT_S
        stages[stage] = StageSpec(command=tuple(command), timeout_s=float(timeout_s))

    return ToolchainManifest(
        schema_version=TOOLCHAIN_SCHEMA,
        toolchain_ref=TOOLCHAIN_PATH,
        manifest_sha256=hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest(),
        stages=stages,
    )


class LeaseLostError(RuntimeError):
    """The worker lost its fenced lease while a toolchain was running."""


class SandboxUnavailableError(RuntimeError):
    """The Home Mac network/credential sandbox cannot be composed."""


def _child_environment() -> dict[str, str]:
    """Return the complete, credential-free environment given to a stage."""
    environment = {"PATH": CHILD_PATH, "HOME": "/var/empty"}
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _sandbox_literal(path: Path) -> str:
    return json.dumps(str(path.resolve()))


def _sandbox_profile(
    forbidden_paths: tuple[Path, ...], read_only_paths: tuple[Path, ...]
) -> str:
    """Deny network/keychain plus the supervisor's database and control files.

    The worker supervisor runs as the existing login user, so these explicit
    sandbox denies are what separate an untrusted repo toolchain from the
    queue/checkpoint paths the supervisor itself must access.
    """
    forbidden_rules = " ".join(
        f"(literal {_sandbox_literal(path)}) (subpath {_sandbox_literal(path)})"
        for path in forbidden_paths
    )
    read_only_rules = " ".join(
        f"(literal {_sandbox_literal(path)}) (subpath {_sandbox_literal(path)})"
        for path in read_only_paths
    )
    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        '(deny mach-lookup (global-name "com.apple.securityd"))\n'
        '(deny mach-lookup (global-name "com.apple.securityd.xpc"))\n'
    )
    if forbidden_rules:
        profile += f"(deny file-read* {forbidden_rules})\n"
        profile += f"(deny file-write* {forbidden_rules})\n"
    if read_only_rules:
        profile += f"(deny file-write* {read_only_rules})\n"
    return profile


def _sandboxed_argv(
    command: tuple[str, ...],
    forbidden_paths: tuple[Path, ...],
    read_only_paths: tuple[Path, ...],
) -> list[str]:
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC).is_file():
        raise SandboxUnavailableError("darwin sandbox-exec is required")
    return [
        SANDBOX_EXEC,
        "-p",
        _sandbox_profile(forbidden_paths, read_only_paths),
        *command,
    ]


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _run_stage(
    repo_path: Path,
    stage: str,
    spec: StageSpec,
    *,
    lease_guard: Callable[[], bool] | None = None,
    forbidden_paths: tuple[Path, ...] = (),
    read_only_paths: tuple[Path, ...] = (),
    heartbeat_interval_s: float = 5.0,
    monotonic=time.monotonic,
) -> StageResult:
    started = monotonic()
    if lease_guard is not None and not lease_guard():
        raise LeaseLostError("lease lost before stage start")
    process = subprocess.Popen(
        _sandboxed_argv(spec.command, forbidden_paths, read_only_paths),
        cwd=str(repo_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_child_environment(),
        close_fds=True,
        start_new_session=True,
    )
    try:
        while True:
            remaining = spec.timeout_s - (monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(spec.command, spec.timeout_s)
            try:
                output, _ = process.communicate(
                    timeout=min(remaining, heartbeat_interval_s)
                )
                returncode = process.returncode
                break
            except subprocess.TimeoutExpired:
                if lease_guard is not None and not lease_guard():
                    _kill_process_group(process)
                    raise LeaseLostError("lease lost during stage")
        output = _bounded(output or "")
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        output = _bounded(
            f"[toolchain: stage {stage!r} exceeded {spec.timeout_s}s and was killed]"
        )
        returncode = TIMEOUT_RETURNCODE
    except LeaseLostError:
        raise
    except BaseException:
        _kill_process_group(process)
        raise
    return StageResult(
        stage=stage,
        returncode=returncode,
        output=output,
        duration_s=monotonic() - started,
    )


def execute_toolchain(
    repo_path: Path,
    manifest: ToolchainManifest,
    *,
    stages: Iterable[str] = STAGES,
    lease_guard: Callable[[], bool] | None = None,
    forbidden_paths: tuple[Path, ...] = (),
    read_only_paths: tuple[Path, ...] = (),
    heartbeat_interval_s: float = 5.0,
    monotonic=time.monotonic,
) -> ToolchainResult:
    """Run every declared stage in order, capturing each outcome.

    Commands come only from `manifest` — never from issue text, a model, or the
    environment. No credentials are injected into the subprocess environment.
    """
    selected = tuple(stages)
    if any(stage not in STAGES for stage in selected) or len(set(selected)) != len(selected):
        raise ValueError("stages must be a unique subset of the fixed toolchain order")
    if selected != tuple(stage for stage in STAGES if stage in selected):
        raise ValueError("stages must preserve the fixed toolchain order")
    results = tuple(
        _run_stage(
            repo_path,
            stage,
            manifest.stages[stage],
            lease_guard=lease_guard,
            forbidden_paths=forbidden_paths,
            read_only_paths=read_only_paths,
            heartbeat_interval_s=heartbeat_interval_s,
            monotonic=monotonic,
        )
        for stage in selected
    )
    return ToolchainResult(stages=results)
