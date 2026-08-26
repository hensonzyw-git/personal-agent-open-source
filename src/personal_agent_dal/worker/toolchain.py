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
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Iterable

from personal_agent_core.manifest import canonical_json

TOOLCHAIN_SCHEMA: Final[str] = "dal.toolchain-manifest/1.0"

#: The fixed, repo-checked-in manifest path. Never derived from issue text.
TOOLCHAIN_PATH: Final[str] = ".personal-agent/toolchain.json"

#: Closed top-level keys. `registry` and `fixture_coder` are optional and absent
#: in the historical manifest, so their absence is not an error — only an
#: unexpected key is.
_TOP_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "stages", "registry", "fixture_coder", "coder"}
)

#: The stages, in the order a worker always runs them.
STAGES: Final[tuple[str, ...]] = ("format", "lint", "build", "test")

DEFAULT_TIMEOUT_S: Final[float] = 120.0
MAX_TIMEOUT_S: Final[float] = 600.0
MAX_OUTPUT_BYTES: Final[int] = 64 * 1024
SANDBOX_EXEC: Final[str] = "/usr/bin/sandbox-exec"
CHILD_PATH: Final[str] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

#: Machine-owned runtime roots a credential-free tool/test child may read.  User
#: home, sibling repositories and arbitrary absolute paths are intentionally not
#: present: the supervisor uses the login identity, but an untrusted stage must
#: not inherit that identity's filesystem authority.
SYSTEM_READ_PATHS: Final[tuple[Path, ...]] = tuple(
    Path(path)
    for path in (
        "/System",
        "/usr/bin",
        "/usr/lib",
        "/usr/libexec",
        "/usr/sbin",
        "/usr/share",
        "/bin",
        "/sbin",
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/opt/homebrew/Cellar",
        "/opt/homebrew/Frameworks",
        "/opt/homebrew/lib",
        "/opt/homebrew/opt",
        "/opt/homebrew/share",
        "/Applications/Xcode.app",
        "/Library/Apple",
        "/Library/Developer",
        "/Library/Frameworks",
        "/private/etc",
        "/private/var/db/dyld",
        "/private/var/db/timezone",
    )
)
SYSTEM_READ_FILES: Final[tuple[Path, ...]] = tuple(
    Path(path)
    for path in (
        "/",
        "/dev/dtracehelper",
        "/dev/null",
        "/dev/random",
        "/dev/urandom",
        "/Library/Preferences/.GlobalPreferences.plist",
        "/Library/Preferences/com.apple.dt.Xcode.plist",
        "/private/var/db/xcode_select_link",
    )
)
def _login_user_home() -> Path:
    """The supervisor's authoritative home, taken from the login database.

    The sandbox profile is composed in the supervisor process, whose uid is the
    login user, so ``getpwuid`` returns the real home even if ``HOME`` is
    tampered.  ``pwd`` is Unix-only; this module only runs on macOS anyway.
    """
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):  # pragma: no cover - non-Unix fallback
        return Path.home()


_HOME: Final[Path] = _login_user_home()

#: The metadata carve-out is a *deny* list, not an allow list.  The
#: ``/usr/bin/python3`` shim must ``readlink`` ``/var/select/developer_dir``
#: before exec'ing the real interpreter, and macOS only grants that through a
#: broad ``file-read-metadata`` grant (no scoped rule satisfies it -- verified
#: empirically against several narrower profiles).  We therefore keep the broad
#: grant and instead deny metadata on the home subtrees that actually hold
#: credentials: Keychain, browser, iCloud and messaging state live under
#: ``~/Library``, and SSH/GPG/cloud credentials live in home dot-directories.
#: The login user's repositories and worktrees (which a stage must still be able
#: to stat) live under a sibling project directory and are deliberately not
#: denied.  Paths are filtered by existence at supervisor time, so a machine
#: without one of these simply gets no rule for it.
METADATA_DENY_PATHS: Final[tuple[Path, ...]] = tuple(
    path
    for path in (
        _HOME / "Library",
        _HOME / ".ssh",
        _HOME / ".gnupg",
        _HOME / ".aws",
        _HOME / ".azure",
        _HOME / ".docker",
        _HOME / ".kube",
        _HOME / ".config",
        _HOME / ".netrc",
    )
    if path.exists()
)
SYSTEM_WRITE_FILES: Final[tuple[Path, ...]] = (Path("/dev/null"),)

#: Return code recorded when a stage exceeds its timeout, matching the `timeout`
#: utility convention so a killed stage is distinguishable from an exit 0.
TIMEOUT_RETURNCODE: Final[int] = 124


@dataclass(frozen=True)
class StageSpec:
    """One declared stage: an argv tuple and a bounded timeout."""

    command: tuple[str, ...]
    timeout_s: float


@dataclass(frozen=True)
class FixtureCoderSpec:
    """The manifest's deterministic no-model coder declaration (DAL-R07A).

    `path` is a single repo-relative tracked file the fixture writes; `template`
    is the deterministic content with an optional `{feature_id}` placeholder.
    """

    path: str
    template: str


@dataclass(frozen=True)
class CoderSpec:
    """The manifest's real-coder declaration (DAL-R07B).

    These values feed both `CoderRunSpec` (the launch) and `consume_coder_stream`
    (the authoritative facts). `prompt` supports a `{feature_id}` placeholder.
    """

    model_alias: str
    allowed_tools: tuple[str, ...]
    max_turns: int
    max_wall_seconds: int
    max_patch_bytes: int
    prompt: str
    allowed_paths: tuple[str, ...]
    pinned_endpoint: str


@dataclass(frozen=True)
class ToolchainManifest:
    """The parsed, validated manifest. `stages` is keyed by stage name.

    `registry`, `fixture_coder` and `coder` are optional. A manifest without any
    coder drives the existing toolchain-only path; `fixture_coder` drives the
    DAL-R07A no-model slice, `coder` drives the DAL-R07B real-coder slice. The
    two coder declarations are mutually exclusive.
    """

    schema_version: str
    toolchain_ref: str
    manifest_sha256: str
    stages: dict[str, StageSpec]
    registry: dict[str, tuple[str, ...]] | None = None
    fixture_coder: FixtureCoderSpec | None = None
    coder: CoderSpec | None = None


@dataclass(frozen=True)
class StageResult:
    """The outcome of one stage. `output` is already truncated; `command` is the
    logical argv actually executed (the declared spec command, without the
    sandbox wrapper) so callers observe what ran rather than re-reading the
    manifest."""

    stage: str
    command: tuple[str, ...]
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
    unknown_top = set(body) - _TOP_KEYS
    if unknown_top:
        raise ValueError(
            f"unknown toolchain manifest keys: {sorted(unknown_top)!r}"
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

    fixture_coder = _load_fixture_coder(body)
    coder = _load_coder(body)
    if fixture_coder is not None and coder is not None:
        raise ValueError(
            "toolchain 'fixture_coder' and 'coder' are mutually exclusive"
        )
    return ToolchainManifest(
        schema_version=TOOLCHAIN_SCHEMA,
        toolchain_ref=TOOLCHAIN_PATH,
        manifest_sha256=hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest(),
        stages=stages,
        registry=_load_registry(body),
        fixture_coder=fixture_coder,
        coder=coder,
    )


def _load_registry(body: dict) -> dict[str, tuple[str, ...]] | None:
    """Parse the optional `registry` declaration (the verification diff argv)."""
    raw = body.get("registry")
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ValueError("toolchain 'registry' must be a non-empty object")
    unknown = set(raw) - {"diff"}
    if unknown:
        raise ValueError(f"unknown registry keys: {sorted(unknown)!r}")
    diff = raw.get("diff")
    if (
        not isinstance(diff, list)
        or not diff
        or not all(isinstance(token, str) and token for token in diff)
    ):
        raise ValueError("toolchain 'registry.diff' must be a non-empty argv")
    return {"diff": tuple(diff)}


def _load_fixture_coder(body: dict) -> FixtureCoderSpec | None:
    """Parse the optional `fixture_coder` declaration (DAL-R07A no-model coder)."""
    raw = body.get("fixture_coder")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("toolchain 'fixture_coder' must be an object")
    unknown = set(raw) - {"path", "template"}
    if unknown:
        raise ValueError(f"unknown fixture_coder keys: {sorted(unknown)!r}")
    path = raw.get("path")
    template = raw.get("template")
    if not isinstance(path, str) or not path:
        raise ValueError("fixture_coder.path must be a non-empty string")
    if not isinstance(template, str) or not template:
        raise ValueError("fixture_coder.template must be a non-empty string")
    return FixtureCoderSpec(path=path, template=template)


def _load_coder(body: dict) -> CoderSpec | None:
    """Parse the optional `coder` declaration (DAL-R07B real-coder route)."""
    raw = body.get("coder")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("toolchain 'coder' must be an object")
    unknown = set(raw) - {
        "model_alias",
        "allowed_tools",
        "max_turns",
        "max_wall_seconds",
        "max_patch_bytes",
        "prompt",
        "allowed_paths",
        "pinned_endpoint",
    }
    if unknown:
        raise ValueError(f"unknown coder keys: {sorted(unknown)!r}")

    model_alias = raw.get("model_alias")
    if not isinstance(model_alias, str) or not model_alias:
        raise ValueError("coder.model_alias must be a non-empty string")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("coder.prompt must be a non-empty string")
    pinned_endpoint = raw.get("pinned_endpoint")
    if not isinstance(pinned_endpoint, str) or not pinned_endpoint:
        raise ValueError("coder.pinned_endpoint must be a non-empty string")

    allowed_tools = _non_empty_str_list(raw, "allowed_tools")
    allowed_paths = _non_empty_str_list(raw, "allowed_paths")

    return CoderSpec(
        model_alias=model_alias,
        allowed_tools=tuple(allowed_tools),
        max_turns=_positive_int_field(raw, "max_turns"),
        max_wall_seconds=_positive_int_field(raw, "max_wall_seconds"),
        max_patch_bytes=_positive_int_field(raw, "max_patch_bytes"),
        prompt=prompt,
        allowed_paths=tuple(allowed_paths),
        pinned_endpoint=pinned_endpoint,
    )


def _non_empty_str_list(raw: dict, key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"coder.{key} must be a non-empty array")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"coder.{key} must be non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"coder.{key} must not repeat")
    return value


def _positive_int_field(raw: dict, key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"coder.{key} must be a positive integer")
    return value


class LeaseLostError(RuntimeError):
    """The worker lost its fenced lease while a toolchain was running."""


class SandboxUnavailableError(RuntimeError):
    """The Home Mac network/credential sandbox cannot be composed."""


def _child_environment(temp_path: Path) -> dict[str, str]:
    """Return the complete, credential-free environment given to a stage."""
    environment = {
        "PATH": CHILD_PATH,
        "HOME": "/var/empty",
        "TMPDIR": str(temp_path.resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _sandbox_literal(path: Path) -> str:
    return json.dumps(str(path.resolve()))


def _sandbox_profile(
    repo_path: Path,
    temp_path: Path,
    forbidden_paths: tuple[Path, ...],
    read_only_paths: tuple[Path, ...],
) -> str:
    """Default-deny files/network while permitting one worktree and runtime.

    The supervisor deliberately runs as the existing login user.  The stage
    does not inherit that user's filesystem authority: it may read machine-owned
    runtime files plus explicitly declared read-only roots, and may write only
    its current worktree and a private per-stage temporary directory.
    """
    readable_paths = (*SYSTEM_READ_PATHS, repo_path, temp_path, *read_only_paths)
    readable_files = SYSTEM_READ_FILES
    writable_paths = (repo_path, temp_path)
    readable_rules = " ".join(
        f"(literal {_sandbox_literal(path)}) (subpath {_sandbox_literal(path)})"
        for path in readable_paths
        if path.exists()
    )
    readable_file_rules = " ".join(
        f"(literal {_sandbox_literal(path)})" for path in readable_files if path.exists()
    )
    writable_file_rules = " ".join(
        f"(literal {_sandbox_literal(path)})"
        for path in SYSTEM_WRITE_FILES
        if path.exists()
    )
    writable_rules = " ".join(
        f"(literal {_sandbox_literal(path)}) (subpath {_sandbox_literal(path)})"
        for path in writable_paths
    )
    metadata_deny_rules = " ".join(
        f"(literal {_sandbox_literal(path)})"
        + (f" (subpath {_sandbox_literal(path)})" if path.is_dir() else "")
        for path in METADATA_DENY_PATHS
        if path.exists()
    )
    forbidden_rules = " ".join(
        f"(literal {_sandbox_literal(path)}) (subpath {_sandbox_literal(path)})"
        for path in forbidden_paths
    )
    read_only_rules = " ".join(
        f"(literal {_sandbox_literal(path)}) (subpath {_sandbox_literal(path)})"
        for path in read_only_paths
    )
    # `sysctl-read` and the machine read roots below are required for the
    # interpreter/Homebrew toolchain to start; their residual exposure (process
    # argv enumeration, world-readable /etc) is bounded and documented in
    # docs/dal/DAL004_威胁模型与权限矩阵_v0.1.md §4.3.1, not accidental.
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
        "(deny network*)\n"
        '(deny mach-lookup (global-name "com.apple.securityd"))\n'
        '(deny mach-lookup (global-name "com.apple.securityd.xpc"))\n'
    )
    if forbidden_rules:
        profile += f"(deny file-read* {forbidden_rules})\n"
        profile += f"(deny file-write* {forbidden_rules})\n"
    if read_only_rules:
        profile += f"(deny file-write* {read_only_rules})\n"
    if metadata_deny_rules:
        profile += f"(deny file-read-metadata {metadata_deny_rules})\n"
    return profile


def _sandboxed_argv(
    command: tuple[str, ...],
    repo_path: Path,
    temp_path: Path,
    forbidden_paths: tuple[Path, ...],
    read_only_paths: tuple[Path, ...],
) -> list[str]:
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC).is_file():
        raise SandboxUnavailableError("darwin sandbox-exec is required")
    return [
        SANDBOX_EXEC,
        "-p",
        _sandbox_profile(repo_path, temp_path, forbidden_paths, read_only_paths),
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
    with tempfile.TemporaryDirectory(prefix="personal-agent-dal-stage-") as raw_temp:
        temp_path = Path(raw_temp)
        os.chmod(temp_path, 0o700)
        process = subprocess.Popen(
            _sandboxed_argv(
                spec.command,
                repo_path,
                temp_path,
                forbidden_paths,
                read_only_paths,
            ),
            cwd=str(repo_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_child_environment(temp_path),
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
        command=spec.command,
        returncode=returncode,
        output=output,
        duration_s=monotonic() - started,
    )


def run_sandboxed_command(
    command: tuple[str, ...],
    repo_path: Path,
    *,
    forbidden_paths: tuple[Path, ...] = (),
    read_only_paths: tuple[Path, ...] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S,
    monotonic=time.monotonic,
    capture_stderr: bool = True,
) -> tuple[str, int]:
    """Run one command under the same default-deny sandbox, credential-free env
    and output bound that `execute_toolchain` stages use, returning
    `(bounded_output, returncode)`. A timeout returns `TIMEOUT_RETURNCODE` with a
    bounded marker instead of raising.

    Exists so the diff capture — which is *not* a toolchain manifest stage — gets
    the identical subprocess boundary instead of a bare `subprocess.run`. It has
    no lease/heartbeat semantics; callers that need those use `execute_toolchain`.

    `capture_stderr=False` discards stderr instead of merging it. Git run through
    the Xcode CommandLineTools shim under the sandbox emits noisy, timestamped
    `xcodebuild`/DVT diagnostics on stderr; a caller that only wants git's clean
    stdout (a SHA, a diff) must opt out of merging or that noise corrupts the
    output and breaks replay.
    """
    with tempfile.TemporaryDirectory(prefix="personal-agent-dal-cmd-") as raw_temp:
        temp_path = Path(raw_temp)
        os.chmod(temp_path, 0o700)
        process = subprocess.Popen(
            _sandboxed_argv(command, repo_path, temp_path, forbidden_paths, read_only_paths),
            cwd=str(repo_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if capture_stderr else subprocess.DEVNULL,
            text=True,
            env=_child_environment(temp_path),
            close_fds=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=timeout_s)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            return (
                _bounded(f"[toolchain: command timed out after {timeout_s}s]"),
                TIMEOUT_RETURNCODE,
            )
        except BaseException:
            _kill_process_group(process)
            raise
    return _bounded(output or ""), returncode


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
