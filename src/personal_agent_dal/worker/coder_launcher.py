"""Claude Code coder launcher (DAL-026 worker half).

The machine half (`machine/coder_contract.py`) classifies an untrusted coder
stream; this module launches the `claude -p` subprocess that produces it. It
builds the argv, an isolated per-run `settings.json` carrying the model override
and the pinned endpoint (never Henson's `~/.claude/settings.json`, and never any
MCP/plugin/project-local override), a credential-free child environment, and a
coder-specific sandbox profile whose only network destination is the local CCR
proxy `127.0.0.1:3456`.

This is the I/O half, deliberately outside the G3 offline receipt that covers
the classifier. Live `claude -p` runs are DAL-006 §9 P2/P3 work; the exact
`modelOverrides` key shape and `--allowedTools` spelling are version-dependent
and are re-checked against `claude --help` / official docs before that live run
(§7.1 contract discovery). This module freezes the *intent*, not the spelling.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final

from personal_agent_dal.worker.toolchain import (
    METADATA_DENY_PATHS,
    SANDBOX_EXEC,
    SYSTEM_READ_FILES,
    SYSTEM_READ_PATHS,
    SYSTEM_WRITE_FILES,
)

CLAUDE_BIN: Final[str] = "claude"
#: The local Claude-Code-Router (CCR) proxy is the only network destination a
#: coder child may dial. It never reaches an upstream host directly.
CCR_ENDPOINT: Final[str] = "127.0.0.1:3456"
OUTPUT_FORMAT: Final[str] = "stream-json"
SETTINGS_FILENAME: Final[str] = "coder-settings.json"
#: The upstream credential is injected as a child-scoped environment variable,
#: never written to disk and never inherited from the supervisor's environment.
ANTHROPIC_TOKEN_VAR: Final[str] = "ANTHROPIC_AUTH_TOKEN"
#: Frozen *intent*: the override maps the CCR-recognized alias (what `--model`
#: accepts) onto DeepSeek V4 Pro. The exact key/value shape is version-dependent
#: and re-checked at P2/P3 (§7.1).
DEEPSEEK_MODEL_ID: Final[str] = "DeepSeek/deepseek-v4-pro"

MAX_OUTPUT_BYTES: Final[int] = 64 * 1024
TIMEOUT_RETURNCODE: Final[int] = 124


@dataclass(frozen=True)
class CoderRunSpec:
    """The frozen, trusted inputs for one coder run.

    `run_root` is the one-shot directory the supervisor creates for this run; it
    holds the isolated `settings.json` and nothing else. `cwd` is the worktree
    the coder may read and write.
    """

    model_alias: str
    max_turns: int
    max_wall_seconds: float
    allowed_tools: tuple[str, ...]
    prompt: str
    cwd: Path
    run_root: Path


@dataclass(frozen=True)
class CoderRunResult:
    """The raw outcome of one launch, before the classifier decides.

    `timed_out` and `cancelled` are the two launch-level terminal conditions the
    caller maps onto `budget_limit` and `cancelled` respectively (§6); the
    classification itself stays in `machine/coder_contract.py`.
    """

    returncode: int | None
    output: str
    timed_out: bool
    cancelled: bool
    duration_s: float


def settings_json(model_alias: str) -> dict[str, Any]:
    """The isolated settings body: model override + pinned endpoint only.

    Nothing else is carried — no `mcpServers`, no plugin or project-local
    override, no upstream token. The token travels separately in the child
    environment, so it is never written to disk inside the settings file.
    """
    return {
        "env": {"ANTHROPIC_BASE_URL": CCR_ENDPOINT},
        "modelOverrides": {model_alias: DEEPSEEK_MODEL_ID},
    }


def write_settings_file(run_root: Path, model_alias: str) -> Path:
    """Write the isolated `settings.json` as mode ``0600``, return its path.

    ``run_root`` is the caller's one-shot directory; the settings file is the
    only thing written there and is never Henson's real settings file.
    """
    path = run_root / SETTINGS_FILENAME
    payload = json.dumps(settings_json(model_alias), sort_keys=True, indent=2)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")
    os.chmod(path, 0o600)
    return path


def coder_argv(spec: CoderRunSpec, settings_path: Path) -> list[str]:
    """The `claude -p` argv, excluding the sandbox wrapper.

    `--allowedTools` spelling and `--max-turns` are frozen here and re-checked
    against the installed CLI at P2/P3; the allowlist is the caller's frozen
    tool set, never derived from a model.
    """
    return [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        OUTPUT_FORMAT,
        "--max-turns",
        str(spec.max_turns),
        "--allowedTools",
        ",".join(spec.allowed_tools),
        "--settings",
        str(settings_path),
        "--model",
        spec.model_alias,
        spec.prompt,
    ]


def coder_environment(upstream_token: str | None = None) -> dict[str, str]:
    """A fresh, credential-free child environment.

    The dict is built from scratch, not from `os.environ`, so nothing the
    supervisor carries leaks into the child. It carries only the pinned base URL
    and, when provided, the upstream token (child-scoped, close-on-exec by way of
    a fresh `env=` mapping — it is never present in the parent's environment).
    """
    environment = {"ANTHROPIC_BASE_URL": CCR_ENDPOINT}
    if upstream_token is not None:
        environment[ANTHROPIC_TOKEN_VAR] = upstream_token
    return environment


def _sandbox_literal(path: Path) -> str:
    return json.dumps(str(path.resolve()))


def coder_sandbox_profile(cwd: Path, temp_path: Path) -> str:
    """A coder-specific sandbox profile: default-deny, loopback-only network.

    Mirrors `toolchain._sandbox_profile` in every respect except the network
    rule: instead of `(deny network*)`, the coder child may reach exactly one
    destination — the local CCR proxy — and nothing else. `(deny default)`
    already denies all other network traffic, so the scoped allow is the only
    outbound channel. Port-level loopback is a known `sandbox-exec` limitation;
    the profile freezes the intent and is re-verified at P2/P3 (§7.2).
    """
    readable_paths = (*SYSTEM_READ_PATHS, cwd, temp_path)
    readable_rules = " ".join(
        f"(literal {_sandbox_literal(path)}) (subpath {_sandbox_literal(path)})"
        for path in readable_paths
        if path.exists()
    )
    readable_file_rules = " ".join(
        f"(literal {_sandbox_literal(path)})" for path in SYSTEM_READ_FILES if path.exists()
    )
    writable_rules = " ".join(
        f"(literal {_sandbox_literal(path)}) (subpath {_sandbox_literal(path)})"
        for path in (cwd, temp_path)
    )
    writable_file_rules = " ".join(
        f"(literal {_sandbox_literal(path)})"
        for path in SYSTEM_WRITE_FILES
        if path.exists()
    )
    metadata_deny_rules = " ".join(
        f"(literal {_sandbox_literal(path)})"
        + (f" (subpath {_sandbox_literal(path)})" if path.is_dir() else "")
        for path in METADATA_DENY_PATHS
        if path.exists()
    )
    profile = (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process-exec)\n"
        "(allow process-fork)\n"
        "(allow signal (target same-sandbox))\n"
        "(allow sysctl-read)\n"
        "(allow file-read-metadata)\n"
        f"(allow file-read* {readable_rules} {readable_file_rules})\n"
        f"(allow file-write* {writable_rules} {writable_file_rules})\n"
        f'(allow network-outbound (literal "{CCR_ENDPOINT}"))\n'
        '(deny mach-lookup (global-name "com.apple.securityd"))\n'
        '(deny mach-lookup (global-name "com.apple.securityd.xpc"))\n'
    )
    if metadata_deny_rules:
        profile += f"(deny file-read-metadata {metadata_deny_rules})\n"
    return profile


def sandboxed_coder_argv(argv: list[str], cwd: Path, temp_path: Path) -> list[str]:
    """Wrap the coder argv with the coder sandbox profile (darwin-only)."""
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC).is_file():
        raise RuntimeError("darwin sandbox-exec is required for the coder child")
    return [
        SANDBOX_EXEC,
        "-p",
        coder_sandbox_profile(cwd, temp_path),
        *argv,
    ]


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _bounded(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore") + (
        "\n[coder: output truncated]"
    )


def run_coder(
    spec: CoderRunSpec,
    *,
    upstream_token: str | None = None,
    cancel_event: Callable[[], bool] | None = None,
    heartbeat_interval_s: float = 5.0,
    monotonic=time.monotonic,
) -> CoderRunResult:
    """Launch one `claude -p` run with a bounded wall clock and cancellation.

    The wall clock is enforced by the same heartbeat + process-group kill the
    toolchain uses: on timeout the whole group is SIGKILLed and the result
    reports `timed_out` (the caller maps this to `budget_limit`); on cancel the
    group is SIGKILLed and the result reports `cancelled`. No orphan process
    survives either path. The command itself comes only from `spec`, never from
    a model, issue text or the environment.
    """
    settings_path = write_settings_file(spec.run_root, spec.model_alias)
    argv = coder_argv(spec, settings_path)
    environment = coder_environment(upstream_token)
    started = monotonic()

    with tempfile.TemporaryDirectory(prefix="personal-agent-dal-coder-") as raw_temp:
        temp_path = Path(raw_temp)
        os.chmod(temp_path, 0o700)
        process = subprocess.Popen(
            sandboxed_coder_argv(argv, spec.cwd, temp_path),
            cwd=str(spec.cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
        try:
            while True:
                if cancel_event is not None and cancel_event():
                    _kill_process_group(process)
                    return CoderRunResult(
                        returncode=None,
                        output="",
                        timed_out=False,
                        cancelled=True,
                        duration_s=monotonic() - started,
                    )
                remaining = spec.max_wall_seconds - (monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, spec.max_wall_seconds)
                try:
                    output, _ = process.communicate(
                        timeout=min(remaining, heartbeat_interval_s)
                    )
                    returncode = process.returncode
                    break
                except subprocess.TimeoutExpired:
                    continue
            output = _bounded(output or "")
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            return CoderRunResult(
                returncode=None,
                output="",
                timed_out=True,
                cancelled=False,
                duration_s=monotonic() - started,
            )
    return CoderRunResult(
        returncode=returncode,
        output=output,
        timed_out=False,
        cancelled=False,
        duration_s=monotonic() - started,
    )
