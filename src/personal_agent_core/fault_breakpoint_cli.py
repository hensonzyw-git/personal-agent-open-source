"""`personal-agent-fault-breakpoint`: the operator's hand on the drill pause.

One writer, one format. The service only ever reads, and it refuses anything it
does not recognise -- a hand-edited file that is one comma wrong means the drill
silently does not pause, which turns the operator's `kill` into a guess. This CLI
exists so the normal path never gets there.

The write is atomic (temp file in the same directory, then `os.replace`), so a
reader can never observe a half-written state file, and the mode is explicit
rather than inherited from the umask: the Finance service user must be able to
read it while only the operator may write it.

`status` exits 0 for armed, 1 for disarmed (file absent), and 2 when the file is
present but cannot be read as an armed breakpoint. A corrupt file must not share
a code with a deliberate absence: one is a decision, the other is a fault.

Those three codes are `status`'s alone. `arm` and `disarm` follow the ordinary
convention -- 0 when the operator's instruction was carried out, non-zero when it
was not -- because they are *commands*, not questions. Returning `status`'s
"disarmed" 1 from a successful `disarm` made every caller that checks an exit
code treat the success as a failure, which is exactly what the drill script did:
it aborted immediately after the SIGKILL and never reached its recovery
assertions.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from personal_agent_core.fault_breakpoint import (
    FAULT_BREAKPOINT_PATH_ENV,
    MAX_SECONDS,
    FaultBreakpoint,
    FaultBreakpointConfigError,
    FaultBreakpointState,
    RECOGNISED_BREAKPOINTS,
    render_state_file,
)
from personal_agent_core.timeutil import utc_now


#: World-readable on purpose: the service user reads it and it holds no secret.
#: Writable only by the owner, which is the operator account that deploys.
STATE_FILE_MODE = 0o644


def _resolve_path(argument: str | None) -> Path:
    configured = (argument or os.environ.get(FAULT_BREAKPOINT_PATH_ENV) or "").strip()
    if not configured:
        raise SystemExit(
            f"pass --path or set {FAULT_BREAKPOINT_PATH_ENV} to the state file"
        )
    path = Path(configured)
    if not path.is_absolute():
        raise SystemExit("the fault breakpoint path must be absolute")
    return path


def _write_state(
    path: Path, *, breakpoint: str, seconds: int, reason: str
) -> None:
    body = render_state_file(
        breakpoint=breakpoint,
        seconds=seconds,
        reason=reason,
        changed_at=utc_now().isoformat(),
    )
    directory = path.parent
    if not directory.is_dir():
        raise SystemExit(f"{directory} does not exist")
    handle, temporary = tempfile.mkstemp(dir=str(directory), prefix=".fault-breakpoint.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, STATE_FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    directory_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _disarm(path: Path) -> None:
    """Remove the breakpoint file. Absence is the disarmed position."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read or set the fault-injection breakpoint on the Finance write "
            "path. Arming pauses a live write at the named breakpoint so the "
            "operator can take the process down at exactly that point; the "
            "pause is time-bounded and fails closed (no file, no pause)."
        )
    )
    parser.add_argument(
        "--path",
        default=None,
        help=f"state file path (default: ${FAULT_BREAKPOINT_PATH_ENV})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "status",
        help=(
            "print the breakpoint; exit 0 armed / 1 disarmed / 2 indeterminate. "
            "arm and disarm instead exit 0 on success."
        ),
    )
    arm = sub.add_parser("arm", help="pause the write path at a named breakpoint")
    arm.add_argument(
        "--breakpoint",
        required=True,
        choices=sorted(RECOGNISED_BREAKPOINTS),
        help="which write-path breakpoint to pause at",
    )
    arm.add_argument(
        "--seconds",
        type=int,
        required=True,
        help=f"how long to hold the write (1..{MAX_SECONDS})",
    )
    arm.add_argument(
        "--reason",
        required=True,
        help="why, for the operator who reads this file next -- possibly you",
    )
    disarm = sub.add_parser("disarm", help="remove the breakpoint (disarm)")
    disarm.add_argument(
        "--reason",
        required=True,
        help="why, for the drill record",
    )
    args = parser.parse_args(argv)

    path = _resolve_path(args.path)

    if args.command == "status":
        return _report(FaultBreakpoint(path).read(), path)

    if args.command == "arm":
        try:
            _write_state(
                path,
                breakpoint=args.breakpoint,
                seconds=args.seconds,
                reason=args.reason,
            )
        except FaultBreakpointConfigError as error:
            raise SystemExit(str(error)) from error
        # Read back through the same reader the service uses. Writing the file
        # and announcing success is a claim; reading it back with the production
        # reader is evidence, and this is the one control whose state must never
        # be assumed.
        state = FaultBreakpoint(path).read()
        if (
            not state.deliberate
            or state.breakpoint != args.breakpoint
            or state.seconds != args.seconds
        ):
            print(f"{path}: {state.detail}")
            print(
                "the state file does not read back as it was armed",
                file=sys.stderr,
            )
            return 2
        print(f"{path}: {state.detail}")
        return 0

    # disarm
    _disarm(path)
    if path.exists():
        print(f"{path}: disarm failed; the file is still present", file=sys.stderr)
        return 2
    print(f"{path}: no fault breakpoint armed (disarmed, reason: {args.reason})")
    return 0


def _report(state: FaultBreakpointState, path: Path) -> int:
    """`status` only: 0 armed, 1 disarmed, 2 present-but-not-an-armed-breakpoint."""
    print(f"{path}: {state.detail}")
    if not state.deliberate:
        return 2 if not state.missing else 1
    return 0


if __name__ == "__main__":  # pragma: no cover - console entrypoint
    raise SystemExit(main())
