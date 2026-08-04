"""The per-breakpoint fault-injection pause for the live chaos drill.

§13.2's last open item requires a live restart at each ordering-critical
breakpoint of `execute_governed_write`. The `prepared` state window is a few
microseconds, so no amount of polling can land a `kill` on it from outside; the
deployed service needs a deliberate, operator-armed pause to hold the write at
the target state until the operator can take the process down. This module is
that pause.

It is the first fault-injection mechanism in the production code, and it is
designed to mirror the write kill switch (`write_switch.py`) because the two are
the same shape of control: an operator-owned state file, read fresh on every
call, failing closed on any doubt. Three differences from the switch are
deliberate:

**The failure direction is inverted.** A kill switch fails closed *towards*
stopping writes (a doubtful file must not allow a write). A fault breakpoint
fails closed *towards not pausing*: a pause is the risky act, so a missing,
malformed, oversized, symlinked or unrecognised file means the write proceeds.
The consequence is a drill that silently does not pause -- which is why the
drill script confirms the pause took effect (the execution is observed stuck at
the target state) before it kills the process, instead of trusting the file.

**It is not a safety control.** Nothing depends on it being armed, so the boot
requirement is softer in spirit but kept the same in letter (the env must name a
file): a service that cannot find its breakpoint file has silently lost the
only tool that can close the §13.2 gate, and that should be loud, not quiet.

**The pause is time-bounded.** `seconds` is validated to `1..MAX_SECONDS`, so an
armed-but-abandoned drill can hold a write for seconds, never forever, and never
long enough to consume the Agent's write-call budget (see `MAX_SECONDS`).

The pause sits **between** the write path's `with sessions()` blocks -- after a
state commit and before the next transition or network call -- so it never holds
a transaction across a pause (§5.2). Recovery deliberately does not go through
`execute_governed_write` (the reconciler calls `adapter.create_record` directly),
so an armed breakpoint can never re-pause a recovery.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: Where Finance MCP looks for the breakpoint state file. There is no default
#: path, mirroring the write switch: a service that cannot find its fault
#: breakpoint file must not silently lack the only tool that closes the gate.
FAULT_BREAKPOINT_PATH_ENV: Final[str] = "PERSONAL_AGENT_FAULT_BREAKPOINT_FILE"

#: The closed set of write-path breakpoints. Unknown names are refused rather
#: than treated as a pause, because a typo must not become a silent no-op that
#: turns the drill's kill into a guess.
BREAKPOINT_BEFORE_PREPARE: Final[str] = "before_prepare"
BREAKPOINT_PREPARED: Final[str] = "prepared"
BREAKPOINT_SUBMITTING: Final[str] = "submitting"
BREAKPOINT_COMMITTED_UNVERIFIED: Final[str] = "committed_unverified"
RECOGNISED_BREAKPOINTS: Final[frozenset[str]] = frozenset(
    {
        BREAKPOINT_BEFORE_PREPARE,
        BREAKPOINT_PREPARED,
        BREAKPOINT_SUBMITTING,
        BREAKPOINT_COMMITTED_UNVERIFIED,
    }
)

#: Upper bound for an armed pause, and it is derived, not chosen for roundness.
#: The Agent gives a write tool a 30-second call budget
#: (`personal_agent.mcp_client.core.DEFAULT_WRITE_CALL_TIMEOUT`; this module may
#: not import it, since `personal_agent_core` sits below `personal_agent`). A
#: pause is only one part of that call -- the Feishu create and the read-back
#: still have to fit -- so the cap has to leave the rest of the write room. At 60
#: an operator could arm a pause that outlives the budget, and the drill would
#: then measure a transport timeout (`source_commit_unknown`) while scoring it as
#: a crash-recovery run: the intended breakpoint silently becomes an accidental
#: timeout classification, which is precisely what this bound exists to prevent.
#: A drill needs a handful of seconds to observe the state and kill the process,
#: so 20 is generous for the purpose and still well inside the budget.
MAX_SECONDS: Final[int] = 20

#: A state file is a few dozen bytes. The cap exists so that a truncated write
#: or a log accidentally redirected onto the path is refused by size.
MAX_STATE_FILE_BYTES: Final[int] = 4096

_REQUIRED_KEYS: Final[frozenset[str]] = frozenset({"breakpoint", "seconds"})
_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset({"reason", "changed_at"})


class FaultBreakpointConfigError(RuntimeError):
    """The breakpoint is not configured. Raised at composition, never at call."""


@dataclass(frozen=True)
class FaultBreakpointState:
    """One fresh reading of the breakpoint file.

    `breakpoint` is the armed name and `seconds` the armed pause, both meaningful
    only when `deliberate` is true. A missing or malformed file yields
    `breakpoint=None, deliberate=False`: the safe reading is "no pause", and the
    drill detects a silently-inert file by observing that the write never stuck
    at the target state, not by this object being right.

    `missing` distinguishes the two non-armed outcomes for the operator: a file
    that simply is not there (the normal disarmed position) from one that is
    present but cannot be read as an armed breakpoint (needs attention).

    `detail` is diagnostic text for logs and the operator CLI. It never reaches
    the wire.
    """

    breakpoint: str | None
    seconds: int
    detail: str
    deliberate: bool = False
    missing: bool = False


def _disarmed(detail: str, *, missing: bool = False) -> FaultBreakpointState:
    """No pause. Never deliberate."""
    return FaultBreakpointState(
        breakpoint=None,
        seconds=0,
        detail=detail,
        deliberate=False,
        missing=missing,
    )


@dataclass(frozen=True)
class FaultPause:
    """What `pause_if_armed` actually did, for the caller to log."""

    breakpoint: str
    seconds: int


class FaultBreakpoint:
    """Reads the breakpoint state file fresh, every time it is asked."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        if not self._path.is_absolute():
            raise FaultBreakpointConfigError(
                "the fault breakpoint path must be absolute, so that it cannot "
                "depend on a service's working directory"
            )

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> FaultBreakpointState:
        """The current breakpoint, if any. Any doubt means no pause.

        The checks are made against the *opened* file, not against a path that
        is stat'ed and then opened separately: `O_NOFOLLOW` makes the symlink
        refusal the kernel's decision at open time, and `fstat` on the resulting
        descriptor describes the very inode the bytes come from. Checking a path
        and then opening it leaves a window in which the two are different files,
        and every property established before that window would be about a file
        this reader never read.
        """
        try:
            descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            # Absent is the disarmed position. The write switch treats a missing
            # file as refused because writes are the risk; here the pause is the
            # risk, so a missing file means "no pause".
            return _disarmed("no fault breakpoint armed", missing=True)
        except OSError as exc:
            # ELOOP/EMLINK is O_NOFOLLOW refusing a symlink, which is the case
            # worth naming; anything else is reported by its type.
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return _disarmed("fault breakpoint file is a symlink")
            return _disarmed(
                f"fault breakpoint file could not be opened ({type(exc).__name__})"
            )

        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                return _disarmed("fault breakpoint path is not a regular file")
            if status.st_size > MAX_STATE_FILE_BYTES:
                return _disarmed("fault breakpoint file is implausibly large")
            try:
                # Read to EOF rather than trusting one `os.read` to return the
                # whole file. A short read that still parsed would be a state
                # file this reader agreed with and never fully saw.
                chunks: list[bytes] = []
                remaining = MAX_STATE_FILE_BYTES
                while remaining > 0:
                    chunk = os.read(descriptor, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks).decode("utf-8")
            except OSError as exc:
                return _disarmed(
                    f"fault breakpoint file could not be read ({type(exc).__name__})"
                )
            except UnicodeDecodeError:
                return _disarmed("fault breakpoint file is not UTF-8")
        finally:
            os.close(descriptor)

        return _interpret(raw)

    async def pause_if_armed(self, name: str) -> FaultPause | None:
        """Pause the write path at `name` if and only if it is armed for `name`.

        Returns what was paused (for the caller's log and audit), or None when
        the file is not armed for this name. The sleep is bounded by the
        validated `seconds` in the file.
        """
        state = self.read()
        if not state.deliberate or state.breakpoint != name:
            return None
        await asyncio.sleep(state.seconds)
        return FaultPause(breakpoint=name, seconds=state.seconds)


def _interpret(raw: str) -> FaultBreakpointState:
    """Turn the file's bytes into a pause decision, recognising nothing extra."""
    try:
        document: Any = json.loads(raw)
    except ValueError:
        return _disarmed("fault breakpoint file is not valid JSON")
    if not isinstance(document, dict):
        return _disarmed("fault breakpoint file is not a JSON object")

    keys = set(document)
    missing = sorted(_REQUIRED_KEYS - keys)
    if missing:
        return _disarmed(f"fault breakpoint file is missing {missing}")
    unknown = sorted(keys - _REQUIRED_KEYS - _OPTIONAL_KEYS)
    if unknown:
        return _disarmed(f"fault breakpoint file carries unknown keys {unknown}")

    breakpoint = document["breakpoint"]
    if not isinstance(breakpoint, str) or breakpoint not in RECOGNISED_BREAKPOINTS:
        return _disarmed(
            "fault breakpoint is not a name this build recognises"
        )

    seconds = document["seconds"]
    if (
        not isinstance(seconds, int)
        or isinstance(seconds, bool)
        or not 1 <= seconds <= MAX_SECONDS
    ):
        return _disarmed(
            f"fault breakpoint seconds must be an int in 1..{MAX_SECONDS}"
        )

    return FaultBreakpointState(
        breakpoint=breakpoint,
        seconds=seconds,
        detail=f"armed to pause at {breakpoint} for {seconds}s",
        deliberate=True,
    )


def load_fault_breakpoint(env: dict[str, str] | None = None) -> FaultBreakpoint:
    """The breakpoint a service must hold before it may serve write tools."""
    env = env if env is not None else dict(os.environ)
    configured = (env.get(FAULT_BREAKPOINT_PATH_ENV) or "").strip()
    if not configured:
        raise FaultBreakpointConfigError(
            f"set {FAULT_BREAKPOINT_PATH_ENV} to the fault breakpoint state file; "
            "a service that cannot find its breakpoint file must not start"
        )
    return FaultBreakpoint(Path(configured))


def render_state_file(
    *,
    breakpoint: str,
    seconds: int,
    reason: str,
    changed_at: str,
) -> str:
    """The exact bytes an operator tool must write. One writer, one format."""
    if breakpoint not in RECOGNISED_BREAKPOINTS:
        raise FaultBreakpointConfigError(
            f"unknown fault breakpoint {breakpoint!r}"
        )
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise FaultBreakpointConfigError("seconds must be an int")
    if not 1 <= seconds <= MAX_SECONDS:
        raise FaultBreakpointConfigError(
            f"seconds must be in 1..{MAX_SECONDS}"
        )
    if not reason.strip():
        raise FaultBreakpointConfigError(
            "an armed breakpoint must carry a reason: the next person to read "
            "this file is the operator during an incident, possibly you, later"
        )
    return (
        json.dumps(
            {
                "breakpoint": breakpoint,
                "seconds": seconds,
                "reason": reason,
                "changed_at": changed_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
