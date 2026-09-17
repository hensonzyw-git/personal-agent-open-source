"""DAL-026: coder launcher argv/env/settings/sandbox contract.

The launcher is the I/O half (`worker/coder_launcher.py`), deliberately outside
the G3 offline receipt that covers the pure classifier. These tests pin what the
launcher *builds* — argv, environment, the isolated settings file, and the
sandbox profile — and prove that timeout and cancellation SIGKILL the whole
process group. The real `claude -p` spawn is DAL-006 §9 P2/P3 work; here
the supervisor spawn seam is mocked, never a real launch. This exercises
historical stream handling only; it grants no mini acceptance.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from personal_agent_dal.worker import coder_launcher
from personal_agent_dal.worker.coder_launcher import (
    CCR_BASE_URL,
    CCR_ENDPOINT,
    CoderRunSpec,
    coder_argv,
    coder_environment,
    coder_sandbox_profile,
    run_coder,
    settings_json,
    write_settings_file,
)


def _spec(**overrides) -> CoderRunSpec:
    defaults = dict(
        model_alias="ccr-deepseek",
        max_turns=16,
        max_wall_seconds=300.0,
        allowed_tools=("read", "edit", "bash"),
        prompt="fix the flaky test",
        cwd=Path("/tmp/run-worktree"),
        run_root=Path("/tmp/run-root"),
    )
    defaults.update(overrides)
    return CoderRunSpec(**defaults)


def test_argv_shape_pins_every_flag() -> None:
    """The argv carries exactly the frozen launch flags, in order."""
    settings = Path("/tmp/run-root/coder-settings.json")
    argv = coder_argv(_spec(), settings)

    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[argv.index("--max-turns") + 1] == "16"
    assert argv[argv.index("--allowedTools") + 1] == "read,edit,bash"
    assert argv[argv.index("--settings") + 1] == str(settings)
    assert argv[argv.index("--model") + 1] == coder_launcher.DEEPSEEK_MODEL_ID
    assert argv[-1] == "fix the flaky test"


def test_environment_is_pinned_and_credential_free() -> None:
    """The child environment is a fresh dict, not an inherited one."""
    env = coder_environment()
    assert env == {
        "ANTHROPIC_BASE_URL": CCR_BASE_URL,
        "HOME": "/var/empty",
        "PATH": coder_launcher.CHILD_PATH,
    }
    assert CCR_ENDPOINT == "127.0.0.1:3456"
    assert CCR_BASE_URL == "http://127.0.0.1:3456"
    for inherited in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        assert inherited not in env


def test_environment_carries_the_token_only_when_given() -> None:
    """The upstream token is child-scoped and never present otherwise."""
    env = coder_environment(upstream_token="tok")
    assert env["ANTHROPIC_BASE_URL"] == CCR_BASE_URL
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok"


def test_settings_json_has_pinned_endpoint_and_no_inheritance() -> None:
    """The settings body carries only the pinned endpoint, nothing more."""
    body = settings_json()
    assert body == {"env": {"ANTHROPIC_BASE_URL": CCR_BASE_URL}}
    for forbidden in (
        "mcpServers",
        "enabledPlugins",
        "permissions",
        "hooks",
        "modelOverrides",
    ):
        assert forbidden not in body


def test_write_settings_file_is_isolated_and_0600(tmp_path: Path) -> None:
    """The settings file lands in the run root, mode 0600, pinned endpoint only."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    path = write_settings_file(run_root)

    assert path == run_root / "coder-settings.json"
    assert path.parent == run_root
    assert (path.stat().st_mode & 0o777) == 0o600
    body = json.loads(path.read_text(encoding="utf-8"))
    assert "modelOverrides" not in body
    assert body["env"]["ANTHROPIC_BASE_URL"] == CCR_BASE_URL


def test_sandbox_profile_allows_only_the_loopback_proxy(tmp_path: Path) -> None:
    """The coder child may dial one destination and nothing else."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    profile = coder_sandbox_profile(Path("/tmp/wt"), Path("/tmp/tmp"), run_root)

    assert '(allow network-outbound (remote tcp "localhost:3456"))' in profile
    assert "(deny network*)" not in profile
    assert "(deny default)" in profile
    # run_root is readable so the child can read coder-settings.json.
    assert str(run_root.resolve()) in profile


def test_timeout_kills_the_whole_process_group(
    monkeypatch, tmp_path: Path
) -> None:
    """A wall-clock overflow SIGKILLs the group and reports `timed_out`."""
    fake_process = mock.Mock()
    fake_process.pid = 4242
    monkeypatch.setattr("personal_agent_dal.worker.supervisor.spawn_unaccepted", mock.Mock(return_value=fake_process))
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: kills.append((pid, sig)))

    spec = _spec(run_root=tmp_path, max_wall_seconds=0.0)
    result = run_coder(spec, monotonic=lambda: 0.0)

    assert result.timed_out is True
    assert result.cancelled is False
    assert result.returncode is None
    assert kills == [(4242, signal.SIGKILL)]
    fake_process.wait.assert_called_once()


def test_cancel_kills_the_whole_process_group(
    monkeypatch, tmp_path: Path
) -> None:
    """A cancel event SIGKILLs the group and reports `cancelled`."""
    fake_process = mock.Mock()
    fake_process.pid = 4242
    monkeypatch.setattr("personal_agent_dal.worker.supervisor.spawn_unaccepted", mock.Mock(return_value=fake_process))
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: kills.append((pid, sig)))

    spec = _spec(run_root=tmp_path)
    result = run_coder(spec, cancel_event=lambda: True, monotonic=lambda: 0.0)

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.returncode is None
    assert kills == [(4242, signal.SIGKILL)]
    fake_process.wait.assert_called_once()


def test_clean_exit_captures_the_returncode(
    monkeypatch, tmp_path: Path
) -> None:
    """A clean run returns the process exit code and its bounded output."""
    fake_process = mock.Mock()
    fake_process.pid = 4242
    fake_process.communicate.return_value = ('{"type":"final"}\n', None)
    fake_process.returncode = 0
    monkeypatch.setattr("personal_agent_dal.worker.supervisor.spawn_unaccepted", mock.Mock(return_value=fake_process))
    monkeypatch.setattr(os, "killpg", mock.Mock())

    spec = _spec(run_root=tmp_path)
    result = run_coder(spec, monotonic=lambda: 0.0)

    assert result.timed_out is False
    assert result.cancelled is False
    assert result.returncode == 0
    assert result.output == '{"type":"final"}\n'
    assert result.truncated is False
    os.killpg.assert_not_called()


def test_output_cap_is_1mib_matching_the_io_bound() -> None:
    """The cap matches the system's existing I/O bound: 1 MiB.

    History: 64 KiB cut healthy streams (R10 T1 incident run, 65,562 bytes);
    256 KiB survived four days before the same task's non-deterministic
    stream size (DeepSeek thinking-event count varies run to run) produced
    a 262,144+ byte run and hit `coder_output_truncated` (2026-09-09,
    job 3d2ae1dd). The cap is NOT the product budget — turns, wall clock
    and patch bytes are, enforced by the contract — it is an I/O guard
    against an unbounded subprocess. It therefore matches the system's
    existing total-patch I/O bound (`MAX_PATCH_TOTAL_SIZE_BYTES`,
    1 MiB) rather than tracking any single observed stream size, and
    `max_turns=12` keeps the event-count ceiling that bounds a hostile
    runaway regardless.
    """
    assert coder_launcher.MAX_OUTPUT_BYTES == 1024 * 1024


def _run_with_output(monkeypatch, tmp_path: Path, text: str):
    """Run `run_coder` against a fake process emitting exactly `text`."""
    fake_process = mock.Mock()
    fake_process.pid = 4242
    fake_process.communicate.return_value = (text, None)
    fake_process.returncode = 0
    monkeypatch.setattr("personal_agent_dal.worker.supervisor.spawn_unaccepted", mock.Mock(return_value=fake_process))
    monkeypatch.setattr(os, "killpg", mock.Mock())
    return run_coder(_spec(run_root=tmp_path), monotonic=lambda: 0.0)


def test_truncation_keeps_stdout_pure_ndjson_and_reports_the_flag(
    monkeypatch, tmp_path: Path
) -> None:
    """An oversized stream is byte-bounded with NO non-JSON marker appended.

    The stdout contract is that every line of a complete run parses as JSON;
    the historical `"[coder: output truncated]"` marker broke that invariant
    (the marker itself is not JSON), turning every over-budget run into
    `coder_output_unparseable` instead of an identifiable budget condition.
    The truncation is now signalled structurally (`CoderRunResult.truncated`)
    and the stdout text carries bytes only — a cut mid-event stays a parse
    failure the caller can attribute to the cap via the flag.
    """
    line = json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
    oversized = line * 30000  # ~1.5 MiB, past the 1 MiB cap
    result = _run_with_output(monkeypatch, tmp_path, oversized)

    assert result.truncated is True
    assert len(result.output.encode("utf-8")) <= coder_launcher.MAX_OUTPUT_BYTES
    assert "[coder: output truncated]" not in result.output


def test_truncation_cut_mid_json_line_yields_no_json_marker(
    monkeypatch, tmp_path: Path
) -> None:
    """The cut byte lands mid-event: the tail is a half JSON line, not a marker.

    Pins the exact failure shape of the R10 T1 incident: the last line of a
    truncated stream may be a partial JSON object. That is accepted — the
    caller distinguishes it from genuine CLI corruption via `truncated`.
    """
    good = json.dumps({"type": "system", "subtype": "init"}) + "\n"
    tail_fragment = '{"type": "assistant", "message": {"cont'
    # Pad so the cap falls inside the fragment: 30000 lines (~1.14 MiB) plus
    # the fragment pushes the cut point past the fragment's start.
    oversized = good * 30000 + tail_fragment
    result = _run_with_output(monkeypatch, tmp_path, oversized)

    assert result.truncated is True
    last_line = result.output.splitlines()[-1]
    with pytest.raises(json.JSONDecodeError):
        json.loads(last_line)
    assert "truncated" not in last_line


def test_argv_carries_disallowed_tools_as_the_closed_universe_minus_allowlist() -> None:
    """The argv denies every CLI tool outside the frozen allowlist.

    R10 T1 dispatch 4 (job 71f8f8c9, 2026-09-09): DeepSeek drifted off the
    frozen tool set — it launched an Agent subagent successfully and attempted
    Write (permission-layer refused). The stream classifier caught it after
    the fact as policy_failure, but the subagent had already executed: the
    `--allowedTools` flag alone does not gate execution, it only shapes the
    default offer. `--disallowedTools` moves the refusal ahead of execution
    and feeds the refusal back to the model, which adapts within the run
    (observed: Write refused -> model switched to Edit and succeeded).

    The deny list is the CLOSED UNIVERSE minus the allowlist — not an
    enumerated blacklist of observed offenders, which would silently miss
    whatever the next CLI version adds.
    """
    settings = Path("/tmp/run-root/coder-settings.json")
    spec = _spec(allowed_tools=("Read", "Edit", "Bash"))
    argv = coder_argv(spec, settings)

    assert "--disallowedTools" in argv
    idx = argv.index("--disallowedTools")
    denied = argv[idx + 1]
    # Frozen universe minus the allowlist: the major execution surfaces.
    assert "Agent" in denied.split(",")
    assert "Write" in denied.split(",")
    assert "WebFetch" in denied.split(",")
    assert "WebSearch" in denied.split(",")
    assert "Task" in denied.split(",")
    assert "NotebookEdit" in denied.split(",")
    # And nothing from the allowlist is denied.
    for allowed in ("Read", "Edit", "Bash"):
        assert allowed not in denied.split(",")


def test_disallowed_universe_is_version_pinned_and_explicit() -> None:
    """The deny universe is a frozen constant, not a runtime query.

    A runtime query of the CLI's live tool list would make the launch argv
    depend on an untrusted, version-drifting source; the universe is frozen
    here instead and re-checked when the pinned CLI version changes.
    """
    universe = coder_launcher.CLI_TOOL_UNIVERSE
    assert "Read" in universe and "Edit" in universe and "Bash" in universe
    # The R10 incident's escapees are in the universe — otherwise they could
    # not be denied.
    assert "Agent" in universe and "Write" in universe


def test_environment_injects_claude_code_tmpdir_inside_the_temp_block(
    monkeypatch, tmp_path: Path
) -> None:
    """`CLAUDE_CODE_TMPDIR` points the CLI's per-uid scratch at the sandbox
    temp dir, reviving Bash inside the coder sandbox.

    Spike 5c (2026-09-09, mini): every Bash call died pre-execution with
    EPERM mkdir /tmp/claude-501/<cwd-slug> — the CLI builds its own per-uid
    scratch root under os.tmpdir() for shell snapshots, and /tmp is not in
    the coder sandbox's writable set. `TMPDIR` alone does NOT participate
    (spike 5b, falsified); `CLAUDE_CODE_TMPDIR` does (binary grep + 5c
    intervention: Bash executed, marker readable, 3-turn clean run). The
    value carries a trailing slash (macOS TMPDIR convention, observed live).

    Widening the sandbox's writable set to /tmp/claude-501 was rejected:
    that directory is shared with the operator's own Claude Code sessions
    — making it writable would make every coder sandbox able to read every
    local session snapshot. CLAUDE_CODE_TMPDIR adds zero writable surface.
    """
    seen: dict[str, str | None] = {}

    def fake_popen(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        process = mock.Mock()
        process.pid = 4242
        process.communicate.return_value = ('{"type":"result"}\n', None)
        process.returncode = 0
        return process

    monkeypatch.setattr("personal_agent_dal.worker.supervisor.spawn_unaccepted", fake_popen)
    monkeypatch.setattr(os, "killpg", mock.Mock())

    run_coder(_spec(run_root=tmp_path), monotonic=lambda: 0.0)

    env = seen["env"]
    assert env is not None
    assert "CLAUDE_CODE_TMPDIR" in env, (
        "the coder child env must redirect the CLI's per-uid scratch root, "
        "or every Bash call dies with EPERM before executing"
    )
    value = env["CLAUDE_CODE_TMPDIR"]
    assert value.endswith("/"), "macOS TMPDIR convention: trailing slash"
    assert "personal-agent-dal-coder-" in value, (
        "must point at the per-run TemporaryDirectory, not a shared path"
    )


def test_coder_environment_pure_function_stays_tmpdir_free() -> None:
    """`coder_environment` itself carries no tmpdir: the redirect is per-run.

    The pure builder cannot know the run's temp dir (it is born inside
    `run_coder`'s with-block), so pinning its absence here makes any future
    refactor that reintroduces a static tmpdir a visible test failure
    rather than a silent shared-scratch regression.
    """
    env = coder_environment("some-token")
    assert "CLAUDE_CODE_TMPDIR" not in env
    assert "TMPDIR" not in env
    env_no_token = coder_environment(None)
    assert "CLAUDE_CODE_TMPDIR" not in env_no_token


def test_output_at_exact_cap_is_not_truncated(
    monkeypatch, tmp_path: Path
) -> None:
    """Output of exactly the cap passes through untouched; cap+1 trips the flag."""
    cap = coder_launcher.MAX_OUTPUT_BYTES
    # A JSON prefix repeated to (almost) the cap, then padded with spaces
    # inside no line-structure assumption — spaces keep every line valid.
    line = json.dumps({"type": "system", "subtype": "init"})
    prefix = (line + "\n") * (cap // (len(line) + 1))
    at_cap_text = prefix + " " * (cap - len(prefix.encode()))
    assert len(at_cap_text.encode()) == cap

    at_cap = _run_with_output(monkeypatch, tmp_path, at_cap_text)
    assert at_cap.truncated is False
    assert at_cap.output == at_cap_text

    over = _run_with_output(monkeypatch, tmp_path, at_cap_text + " ")
    assert over.truncated is True
    assert len(over.output.encode()) == cap


def test_production_launcher_refuses_before_subprocess(monkeypatch, tmp_path):
    from personal_agent_dal.worker.supervisor import SupervisorRefusal
    calls=[]
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: calls.append(1))
    with pytest.raises(SupervisorRefusal, match='MACHINE_PROOF_REQUIRED'):
        run_coder(_spec(run_root=tmp_path))
    assert calls==[]
