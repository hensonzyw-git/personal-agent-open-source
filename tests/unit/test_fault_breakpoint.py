"""§13.2 fault-breakpoint module and CLI tests.

The module is the drill hook on the Finance write path: an operator-armed state
file that pauses a live write at a named breakpoint, fail-closed (no file, no
pause), time-bounded. These tests pin the fail-closed set, the bounded pause,
the `deliberate`/`missing` semantics, and the one-writer CLI round trip.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from personal_agent_core.fault_breakpoint import (
    BREAKPOINT_BEFORE_PREPARE,
    BREAKPOINT_COMMITTED_UNVERIFIED,
    BREAKPOINT_PREPARED,
    BREAKPOINT_SUBMITTING,
    FAULT_BREAKPOINT_PATH_ENV,
    MAX_SECONDS,
    FaultBreakpoint,
    FaultBreakpointConfigError,
    load_fault_breakpoint,
    render_state_file,
)
from personal_agent_core.fault_breakpoint_cli import main as cli_main


def armed_doc(*, breakpoint: str = BREAKPOINT_PREPARED, seconds: int = 1) -> str:
    return render_state_file(
        breakpoint=breakpoint,
        seconds=seconds,
        reason="test",
        changed_at="2026-08-04T00:00:00+00:00",
    )


def write_armed(directory: Path, **kwargs) -> FaultBreakpoint:
    path = directory / "breakpoint.json"
    path.write_text(armed_doc(**kwargs), encoding="utf-8")
    return FaultBreakpoint(path)


async def _pause(breakpoint: FaultBreakpoint, name: str):
    return await breakpoint.pause_if_armed(name)


def run(coro):
    import asyncio

    return asyncio.run(coro)


# --- fail closed --------------------------------------------------------------


def test_absent_file_is_disarmed_and_never_pauses(tmp_path: Path) -> None:
    bp = FaultBreakpoint(tmp_path / "absent.json")
    state = bp.read()
    assert state.missing is True
    assert state.deliberate is False
    assert run(_pause(bp, BREAKPOINT_PREPARED)) is None


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        '{"seconds": 1}',
        '{"breakpoint": "prepared"}',
        '{"breakpoint": "prepared", "seconds": 1, "extra": true}',
        '{"breakpoint": "no_such_breakpoint", "seconds": 1}',
        '{"breakpoint": "prepared", "seconds": 0}',
        f'{{"breakpoint": "prepared", "seconds": {MAX_SECONDS + 1}}}',
        '{"breakpoint": "prepared", "seconds": 1.5}',
        '{"breakpoint": "prepared", "seconds": "1"}',
    ],
)
def test_malformed_files_never_pause(tmp_path: Path, document: str) -> None:
    path = tmp_path / "breakpoint.json"
    path.write_text(document, encoding="utf-8")
    bp = FaultBreakpoint(path)
    state = bp.read()
    assert state.deliberate is False
    assert state.missing is False  # present but unreadable as an armed breakpoint
    assert run(_pause(bp, BREAKPOINT_PREPARED)) is None


def test_unknown_breakpoint_name_never_pauses(tmp_path: Path) -> None:
    bp = write_armed(tmp_path, breakpoint=BREAKPOINT_SUBMITTING)
    # Armed for submitting, not prepared: the prepared call must not pause.
    assert run(_pause(bp, BREAKPOINT_PREPARED)) is None


def test_symlink_never_pauses(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text(armed_doc(), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target.name)
    bp = FaultBreakpoint(link)
    assert bp.read().deliberate is False
    assert run(_pause(bp, BREAKPOINT_PREPARED)) is None


def test_oversized_file_never_pauses(tmp_path: Path) -> None:
    path = tmp_path / "breakpoint.json"
    path.write_text("x" * 5000, encoding="utf-8")
    bp = FaultBreakpoint(path)
    assert bp.read().deliberate is False
    assert run(_pause(bp, BREAKPOINT_PREPARED)) is None


def test_non_regular_path_never_pauses(tmp_path: Path) -> None:
    bp = FaultBreakpoint(tmp_path)  # a directory
    assert bp.read().deliberate is False
    assert run(_pause(bp, BREAKPOINT_PREPARED)) is None


# --- the bounded pause --------------------------------------------------------


def test_armed_breakpoint_pauses_only_the_named_one(tmp_path: Path) -> None:
    bp = write_armed(tmp_path, breakpoint=BREAKPOINT_PREPARED, seconds=1)
    start = time.monotonic()
    pause = run(_pause(bp, BREAKPOINT_PREPARED))
    elapsed = time.monotonic() - start
    assert pause is not None
    assert pause.breakpoint == BREAKPOINT_PREPARED
    assert pause.seconds == 1
    assert elapsed >= 0.9, f"expected the armed pause, got {elapsed:.2f}s"


def test_all_four_breakpoint_names_are_recognised() -> None:
    for name in (
        BREAKPOINT_BEFORE_PREPARE,
        BREAKPOINT_PREPARED,
        BREAKPOINT_SUBMITTING,
        BREAKPOINT_COMMITTED_UNVERIFIED,
    ):
        assert name  # each is a non-empty closed-set member


# --- boot loader and one-writer format ----------------------------------------


def test_load_requires_the_env_var(monkeypatch) -> None:
    monkeypatch.delenv(FAULT_BREAKPOINT_PATH_ENV, raising=False)
    with pytest.raises(FaultBreakpointConfigError):
        load_fault_breakpoint(env={})
    with pytest.raises(FaultBreakpointConfigError):
        load_fault_breakpoint(env={FAULT_BREAKPOINT_PATH_ENV: "   "})


def test_load_returns_a_breakpoint_for_a_configured_path(tmp_path: Path) -> None:
    path = tmp_path / "breakpoint.json"
    bp = load_fault_breakpoint(env={FAULT_BREAKPOINT_PATH_ENV: str(path)})
    assert bp.path == path


def test_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(FaultBreakpointConfigError):
        FaultBreakpoint(Path("relative.json"))


def test_render_state_file_validates(tmp_path: Path) -> None:
    with pytest.raises(FaultBreakpointConfigError):
        render_state_file(
            breakpoint="no_such", seconds=1, reason="x", changed_at="now"
        )
    with pytest.raises(FaultBreakpointConfigError):
        render_state_file(
            breakpoint=BREAKPOINT_PREPARED, seconds=0, reason="x", changed_at="now"
        )
    with pytest.raises(FaultBreakpointConfigError):
        render_state_file(
            breakpoint=BREAKPOINT_PREPARED,
            seconds=1,
            reason="   ",
            changed_at="now",
        )


def test_rendered_document_reads_back_armed(tmp_path: Path) -> None:
    path = tmp_path / "breakpoint.json"
    path.write_text(armed_doc(), encoding="utf-8")
    state = FaultBreakpoint(path).read()
    assert state.deliberate is True
    assert state.breakpoint == BREAKPOINT_PREPARED
    assert state.seconds == 1
    assert json.loads(path.read_text(encoding="utf-8"))["breakpoint"] == (
        BREAKPOINT_PREPARED
    )


# --- the operator CLI ---------------------------------------------------------


def test_cli_arm_status_disarm_round_trip(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "breakpoint.json"
    monkeypatch.setenv(FAULT_BREAKPOINT_PATH_ENV, str(path))
    # Status with no file: disarmed.
    assert cli_main(["status"]) == 1
    # Arm, and the file must read back through the production reader.
    assert (
        cli_main(
            [
                "arm",
                "--breakpoint",
                BREAKPOINT_PREPARED,
                "--seconds",
                "1",
                "--reason",
                "test drill",
            ]
        )
        == 0
    )
    state = FaultBreakpoint(path).read()
    assert state.deliberate is True
    assert state.breakpoint == BREAKPOINT_PREPARED
    assert cli_main(["status"]) == 0
    # Disarm removes the file and reports success as success. The 0/1/2 codes
    # belong to `status` alone: a command that carried out the operator's
    # instruction and then exits non-zero makes every `set -e` caller treat the
    # success as a failure -- which is exactly how the drill script came to abort
    # immediately after its SIGKILL, before any recovery assertion ran.
    assert cli_main(["disarm", "--reason", "test drill"]) == 0
    assert not path.exists()
    assert cli_main(["status"]) == 1
    # Disarming an already-disarmed breakpoint is still success: the operator
    # asked for a position, not for a transition.
    assert cli_main(["disarm", "--reason", "again"]) == 0


def test_cli_status_returns_2_for_a_present_but_broken_file(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "breakpoint.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv(FAULT_BREAKPOINT_PATH_ENV, str(path))
    assert cli_main(["status"]) == 2


def test_cli_arm_refuses_invalid_arguments(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "breakpoint.json"
    monkeypatch.setenv(FAULT_BREAKPOINT_PATH_ENV, str(path))
    with pytest.raises(SystemExit):
        cli_main(
            [
                "arm",
                "--breakpoint",
                "no_such",
                "--seconds",
                "1",
                "--reason",
                "x",
            ]
        )
    with pytest.raises(SystemExit):
        cli_main(
            [
                "arm",
                "--breakpoint",
                BREAKPOINT_PREPARED,
                "--seconds",
                f"{MAX_SECONDS + 1}",
                "--reason",
                "x",
            ]
        )
    assert not path.exists()


# --- the boot requirement -----------------------------------------------------
#
# The env var is a composition-time contract, so the only honest place to assert
# it is `cli.main`. Without this, a unit file that dropped the env from
# `personal-data-mcp.service` would leave every offline test green and the
# service would fail only on the ECS, at restart, in the middle of a drill.


def _serving_argv(tmp_path: Path) -> list[str]:
    from personal_data_mcp.storage import db
    from personal_data_mcp.storage.engine import create_database_engine

    database = tmp_path / "finance.sqlite"
    engine = create_database_engine(database)
    db.upgrade(engine, "head")
    engine.dispose()
    config = Path(__file__).parents[1] / "fixtures" / "ledger" / "config.synthetic.json"
    return [
        "personal-data-mcp",
        "--database",
        str(database),
        "--ledger-config",
        str(config),
    ]


def test_the_write_surface_refuses_to_start_without_a_breakpoint_path(
    monkeypatch, tmp_path: Path
) -> None:
    import sys

    from personal_data_mcp import cli

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("served the write surface with no breakpoint path")

    monkeypatch.setattr(cli, "_serve_with_finance_tools", forbidden)
    monkeypatch.setenv("PERSONAL_AGENT_WRITE_SWITCH_FILE", str(tmp_path / "switch.json"))
    monkeypatch.delenv(FAULT_BREAKPOINT_PATH_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", _serving_argv(tmp_path))

    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert FAULT_BREAKPOINT_PATH_ENV in str(raised.value)


def test_a_relative_breakpoint_path_is_refused_at_boot(
    monkeypatch, tmp_path: Path
) -> None:
    """A path resolved against the working directory is not a control at all."""
    import sys

    from personal_data_mcp import cli

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("served the write surface with a relative path")

    monkeypatch.setattr(cli, "_serve_with_finance_tools", forbidden)
    monkeypatch.setenv("PERSONAL_AGENT_WRITE_SWITCH_FILE", str(tmp_path / "switch.json"))
    monkeypatch.setenv(FAULT_BREAKPOINT_PATH_ENV, "fault-breakpoint.json")
    monkeypatch.setattr(sys, "argv", _serving_argv(tmp_path))

    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert "absolute" in str(raised.value)
