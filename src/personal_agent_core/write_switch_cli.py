"""`personal-agent-write-switch`: the operator's hand on the kill switch.

One writer, one format. The services only ever read, and they refuse anything
they do not recognise, so a hand-edited file that is one comma wrong stops
writes rather than allowing them -- which is the safe direction, but a confusing
way to spend an incident. This CLI exists so the normal path never gets there.

The write is atomic (temp file in the same directory, then `os.replace`), so a
reader can never observe a half-written state file, and the mode is explicit
rather than inherited from the umask: both services run as different users and
both must be able to read it, while only the operator may write it.

`status` exits 0 for enabled, 1 for deliberately disabled, and 2 when the state
could not be established at all -- missing, unreadable or malformed. Those last
two must not share a code: a corrupt file that reported "disabled" would look
exactly like a decision somebody made, and the fault would never be noticed.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from personal_agent_core.timeutil import utc_now
from personal_agent_core.write_switch import (
    WRITES_DISABLED,
    WRITES_ENABLED,
    WRITE_SWITCH_PATH_ENV,
    WriteSwitch,
    WriteSwitchConfigError,
    WriteSwitchState,
    render_state_file,
)


#: World-readable on purpose: two service users read it and it holds no secret.
#: Writable only by the owner, which is the operator account that deploys.
STATE_FILE_MODE = 0o644


def _resolve_path(argument: str | None) -> Path:
    configured = (argument or os.environ.get(WRITE_SWITCH_PATH_ENV) or "").strip()
    if not configured:
        raise SystemExit(
            f"pass --path or set {WRITE_SWITCH_PATH_ENV} to the state file"
        )
    path = Path(configured)
    if not path.is_absolute():
        raise SystemExit("the write switch path must be absolute")
    return path


def _write_state(path: Path, *, writes: str, reason: str) -> None:
    body = render_state_file(
        writes=writes,
        reason=reason,
        changed_at=utc_now().isoformat(),
    )
    directory = path.parent
    if not directory.is_dir():
        raise SystemExit(f"{directory} does not exist")
    handle, temporary = tempfile.mkstemp(dir=str(directory), prefix=".write-switch.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, STATE_FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        # A failed flip must not leave a temp file that a later reader could be
        # pointed at, nor a partially written state file at the real path.
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    # Durably: the point of the switch is that it survives whatever made the
    # operator reach for it, including the machine going down a second later.
    directory_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read or set the external-write kill switch. Disabling stops every "
            "Finance write tool at both the Agent and the Finance MCP layer; "
            "reads, device identity and chat keep working."
        )
    )
    parser.add_argument(
        "--path",
        default=None,
        help=f"state file path (default: ${WRITE_SWITCH_PATH_ENV})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "status",
        help="print the state; exit 0 enabled / 1 disabled / 2 indeterminate",
    )
    for name, help_text in (
        ("disable", "stop all external writes"),
        ("enable", "allow external writes again"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--reason",
            required=True,
            help="why, for the operator who reads this file next -- possibly you",
        )
    args = parser.parse_args(argv)

    path = _resolve_path(args.path)

    if args.command == "status":
        return _report(WriteSwitch(path).read(), path)

    writes = WRITES_ENABLED if args.command == "enable" else WRITES_DISABLED
    try:
        _write_state(path, writes=writes, reason=args.reason)
    except WriteSwitchConfigError as error:
        raise SystemExit(str(error)) from error

    # Read back through the same reader the services use. Writing the file and
    # announcing success is a claim; reading it back with the production reader
    # is evidence, and this is the one control whose state must never be assumed.
    state = WriteSwitch(path).read()
    if state.writes_allowed != (writes == WRITES_ENABLED) or not state.deliberate:
        print(f"{path}: {state.detail}")
        print(
            "the state file does not read back as it was written",
            file=sys.stderr,
        )
        return 2
    return _report(state, path)


def _report(state: WriteSwitchState, path: Path) -> int:
    """0 enabled, 1 deliberately disabled, 2 state could not be established."""
    print(f"{path}: {state.detail}")
    if not state.deliberate:
        return 2
    return 0 if state.writes_allowed else 1


if __name__ == "__main__":  # pragma: no cover - console entrypoint
    raise SystemExit(main())
