"""Real write-switch state files for tests.

There is deliberately no in-memory fake here. The switch's whole value is that
it reads a file on a real filesystem and refuses everything it cannot parse, so
a fake that answers `True` would be exactly the counterparty §5.1 warns about:
built from the same assumptions as the code, able only to confirm them.

These helpers write through the same renderer the operator CLI uses, so a test
that enables writes has proven the production reader accepts the production
writer's output.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from personal_agent_core.write_switch import (
    WRITES_DISABLED,
    WRITES_ENABLED,
    WriteSwitch,
    render_state_file,
)


def write_switch_state(
    path: Path, *, writes: str, reason: str = "test fixture"
) -> Path:
    """Put a real, valid state file at `path` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_state_file(
            writes=writes,
            reason=reason,
            changed_at="2026-08-02T00:00:00+00:00",
        ),
        encoding="utf-8",
    )
    return path


def enabled_write_switch(directory: Path, *, name: str = "write-switch.json") -> WriteSwitch:
    """A switch whose state file really says writes are enabled."""
    return WriteSwitch(
        write_switch_state(directory / name, writes=WRITES_ENABLED)
    )


def disabled_write_switch_file(
    directory: Path, *, name: str = "write-switch.json"
) -> WriteSwitch:
    """A switch whose state file really says writes are disabled."""
    return WriteSwitch(
        write_switch_state(directory / name, writes=WRITES_DISABLED)
    )


_SHARED: dict[str, WriteSwitch] = {}


def shared_enabled_write_switch() -> WriteSwitch:
    """One real enabled state file for tests that only need writes to be on.

    Cached per process so that a test which flips it would affect others -- which
    is why nothing here mutates it. Tests about the switch itself build their own
    file under `tmp_path` and are free to change it.
    """
    if "enabled" not in _SHARED:
        directory = Path(tempfile.mkdtemp(prefix="shared-write-switch."))
        _SHARED["enabled"] = enabled_write_switch(directory)
    return _SHARED["enabled"]


def flip(switch: WriteSwitch, *, writes: str, reason: str = "test flip") -> None:
    """Change an existing switch's state file in place, as an operator would."""
    write_switch_state(switch.path, writes=writes, reason=reason)
