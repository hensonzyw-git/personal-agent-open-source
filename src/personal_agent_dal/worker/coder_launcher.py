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
    CHILD_PATH,
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
#: The URL the coder child dials (endpoint + `http://` scheme). `claude` parses
#: `ANTHROPIC_BASE_URL` as a URL, so a bare `host:port` fails ("cannot be parsed
#: as a URL").
CCR_BASE_URL: Final[str] = f"http://{CCR_ENDPOINT}"
#: The loopback port for the sandbox network rule (host must be `localhost`).
CCR_PORT: Final[str] = CCR_ENDPOINT.rsplit(":", 1)[1]
OUTPUT_FORMAT: Final[str] = "stream-json"
SETTINGS_FILENAME: Final[str] = "coder-settings.json"
#: The upstream credential is injected as a child-scoped environment variable,
#: never written to disk and never inherited from the supervisor's environment.
ANTHROPIC_TOKEN_VAR: Final[str] = "ANTHROPIC_AUTH_TOKEN"
#: Frozen *intent*: the override maps the CCR-recognized alias (what `--model`
#: accepts) onto DeepSeek V4 Pro. The exact key/value shape is version-dependent
#: and re-checked at P2/P3 (§7.1).
DEEPSEEK_MODEL_ID: Final[str] = "DeepSeek/deepseek-v4-pro"

#: The stdout byte cap: an I/O guard, not a product budget. 1 MiB, matching
#: the system's existing total-patch I/O bound (machine/patch_policy.
#: MAX_PATCH_TOTAL_SIZE_BYTES). The product budgets — max_turns, wall clock,
#: max_patch_bytes — are enforced by the frozen manifest and the contract
#: classifier; this cap exists only so an unbounded subprocess cannot exhaust
#: worker memory/disk. Sizing history (both directions observed live):
#: 64 KiB cut a healthy 65,562-byte stream mid-event and the marker appended
#: to the truncated text was itself not JSON, so a complete, correct run was
#: refused as `coder_output_unparseable` (R10 T1, 2026-09-09); 256 KiB
#: survived four days before the same task's non-deterministic stream size
#: (DeepSeek thinking-event count varies run to run) tripped
#: `coder_output_truncated` on job 3d2ae1dd. Truncation is signalled
#: structurally via `CoderRunResult.truncated`; no marker is ever appended
#: to stdout, which keeps the stdout-is-pure-NDJSON invariant for complete
#: runs.
MAX_OUTPUT_BYTES: Final[int] = 1024 * 1024
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
    #: True when the stdout bytes exceeded `MAX_OUTPUT_BYTES` and were cut.
    #: Signalled structurally, never as a text marker: a marker appended to
    #: the stream would itself violate the every-line-is-JSON contract the
    #: parser depends on (R10 T1).
    truncated: bool = False


def settings_json() -> dict[str, Any]:
    """The isolated settings body: the pinned endpoint only.

    Nothing else is carried — no model override, no `mcpServers`, no plugin or
    project-local override, no upstream token. The model is pinned on the command
    line (`--model DEEPSEEK_MODEL_ID`) rather than through a `modelOverrides`
    alias: a non-standard alias key is not recognised by this Claude Code build,
    so the alias path returns 400 from the CCR proxy (found live in DAL-R07B).
    The token travels separately in the child environment, never to disk.
    """
    return {"env": {"ANTHROPIC_BASE_URL": CCR_BASE_URL}}


def write_settings_file(run_root: Path) -> Path:
    """Write the isolated `settings.json` as mode ``0600``, return its path.

    ``run_root`` is the caller's one-shot directory; the settings file is the
    only thing written there and is never Henson's real settings file.
    """
    path = run_root / SETTINGS_FILENAME
    payload = json.dumps(settings_json(), sort_keys=True, indent=2)
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
        # stream-json over --print is rejected without --verbose by this build.
        "--verbose",
        "--max-turns",
        str(spec.max_turns),
        "--allowedTools",
        ",".join(spec.allowed_tools),
        "--settings",
        str(settings_path),
        "--model",
        DEEPSEEK_MODEL_ID,
        spec.prompt,
    ]


def coder_environment(upstream_token: str | None = None) -> dict[str, str]:
    """A fresh, credential-free child environment.

    The dict is built from scratch, not from `os.environ`, so nothing the
    supervisor carries leaks into the child. It carries only the pinned base URL
    and, when provided, the upstream token (child-scoped, close-on-exec by way of
    a fresh `env=` mapping — it is never present in the parent's environment).
    """
    environment = {
        "ANTHROPIC_BASE_URL": CCR_BASE_URL,
        # node's os.homedir() fails with ENOENT when HOME is unset, and claude
        # needs a home to resolve its config paths. /var/empty is the same
        # credential-free home the toolchain child uses. PATH is the same
        # credential-free path: claude spawns child processes (node, apiKeyHelper)
        # that must resolve through it.
        "HOME": "/var/empty",
        "PATH": CHILD_PATH,
    }
    if upstream_token is not None:
        environment[ANTHROPIC_TOKEN_VAR] = upstream_token
    return environment


def _sandbox_literal(path: Path) -> str:
    return json.dumps(str(path.resolve()))


def coder_sandbox_profile(cwd: Path, temp_path: Path, run_root: Path) -> str:
    """A coder-specific sandbox profile: default-deny, loopback-only network.

    Mirrors `toolchain._sandbox_profile` in every respect except the network
    rule: instead of `(deny network*)`, the coder child may reach exactly one
    destination — the local CCR proxy — and nothing else. `(deny default)`
    already denies all other network traffic, so the scoped allow is the only
    outbound channel. Port-level loopback is a known `sandbox-exec` limitation;
    the profile freezes the intent and is re-verified at P2/P3 (§7.2).

    `run_root` is added to the readable set so the child can read the isolated
    `coder-settings.json` passed via `--settings`; it holds nothing else.
    """
    readable_paths = (*SYSTEM_READ_PATHS, cwd, temp_path, run_root)
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
        # sandbox-exec's network filter host is restricted to `localhost` or `*`
        # (not an IP), so the loopback CCR destination is scoped by port only.
        f'(allow network-outbound (remote tcp "localhost:{CCR_PORT}"))\n'
        '(deny mach-lookup (global-name "com.apple.securityd"))\n'
        '(deny mach-lookup (global-name "com.apple.securityd.xpc"))\n'
    )
    if metadata_deny_rules:
        profile += f"(deny file-read-metadata {metadata_deny_rules})\n"
    return profile


def sandboxed_coder_argv(
    argv: list[str], cwd: Path, temp_path: Path, run_root: Path
) -> list[str]:
    """Wrap the coder argv with the coder sandbox profile (darwin-only)."""
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC).is_file():
        raise RuntimeError("darwin sandbox-exec is required for the coder child")
    return [
        SANDBOX_EXEC,
        "-p",
        coder_sandbox_profile(cwd, temp_path, run_root),
        *argv,
    ]


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _bounded(text: str) -> tuple[str, bool]:
    """Cut to the byte cap, returning `(bounded_text, truncated)`.

    No marker is appended: the parser downstream requires every line of a
    complete run to be JSON, and a free-text marker breaks that invariant
    exactly when the run is already at its budget limit (R10 T1). The
    `truncated` flag carries the condition structurally instead.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text, False
    return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


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
    settings_path = write_settings_file(spec.run_root)
    argv = coder_argv(spec, settings_path)
    environment = coder_environment(upstream_token)
    started = monotonic()

    with tempfile.TemporaryDirectory(prefix="personal-agent-dal-coder-") as raw_temp:
        temp_path = Path(raw_temp)
        os.chmod(temp_path, 0o700)
        process = subprocess.Popen(
            sandboxed_coder_argv(argv, spec.cwd, temp_path, spec.run_root),
            cwd=str(spec.cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # stderr is discarded, not merged: `--output-format stream-json` is a
            # newline-delimited JSON stream on stdout, and claude under the
            # sandbox emits timestamped xcodebuild/DVT noise on stderr that would
            # corrupt the stream. Provider errors surface in the `result` event.
            stderr=subprocess.DEVNULL,
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
            output, truncated = _bounded(output or "")
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
        truncated=truncated,
    )
