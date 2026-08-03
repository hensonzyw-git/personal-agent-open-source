"""The external-write kill switch, shared by both services.

Design 10.6 names the first rollback unit: "先通过 global allowlist 关闭全部
write tools，保留只读和设备状态". This module is that unit. It stops every tool
whose contract effect is not `read` from reaching the fact source, while reads,
device identity, the control plane and the conversation itself keep working --
because the conversation is how the operator finds out what happened.

Three decisions, all Henson's, all load-bearing:

**It fails closed.** The state is an explicit file that must say `enabled` for a
write to proceed. Missing, unreadable, malformed, oversized, a symlink, a
directory, or any value this build does not recognise all refuse. The reasoning
is asymmetric: a kill switch that silently *disengages* is a silent failure and
nobody would notice until a bad write landed; a switch that wrongly *engages* is
a loud refusal in front of the only user, who can read the reason and fix the
file. Given the project's own criterion -- a loud failure needs no monitoring --
only one of those two is an acceptable default.

**Nothing is cached.** Both callers re-read on every call. A kill switch with a
cache is a kill switch with a delay, and the delay is exactly as long as the
incident.

**It is not an env var.** An env var would need a service restart to flip, and a
restart during an incident tears down in-flight writes at `submitting`, turning
"stop new writes" into "strand the writes already in progress" and manufacturing
`commit_unknown` rows that then need reconciling. A file flips without touching
a single in-flight request.

The switch deliberately does **not** stop recovery. `reconcile_write` replays an
already-authorised, already-submitted intent under its original `client_token`,
so it completes a decision the user already made rather than making a new one;
stopping it would strand every in-flight execution as a `commit_unknown` alert
at exactly the moment the operator is trying to make the system quiet.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final


#: Where both services look for the state file. There is no default path: a
#: service that does not know where its kill switch lives must not start.
WRITE_SWITCH_PATH_ENV: Final[str] = "PERSONAL_AGENT_WRITE_SWITCH_FILE"

WRITES_ENABLED: Final[str] = "enabled"
WRITES_DISABLED: Final[str] = "disabled"
_RECOGNISED_STATES: Final[frozenset[str]] = frozenset(
    {WRITES_ENABLED, WRITES_DISABLED}
)

#: A state file is a few dozen bytes. The cap exists so that a truncated write,
#: a log accidentally redirected onto the path, or a deliberately huge file is
#: refused by size before it is parsed.
MAX_STATE_FILE_BYTES: Final[int] = 4096

_REQUIRED_KEYS: Final[frozenset[str]] = frozenset({"writes"})
_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset({"reason", "changed_at"})


class WriteSwitchConfigError(RuntimeError):
    """The switch itself is not configured. Raised at composition, never at call."""


@dataclass(frozen=True)
class WriteSwitchState:
    """One fresh reading of the switch.

    `writes_allowed` is the only thing an enforcement point needs. `deliberate`
    exists for the operator's view: "off because someone turned it off" and "off
    because the file is corrupt" are the same refusal to a caller and completely
    different facts to whoever is on the machine. Collapsing them would let a
    broken state file masquerade as a decision, which is how a fault becomes
    invisible.

    `detail` is diagnostic text for logs, audit summaries and `internal_detail`.
    It never reaches the wire, and it never contains the file's contents beyond
    the recognised state word -- an operator's free-text `reason` is theirs to
    read on the server, not something to echo to a client.
    """

    writes_allowed: bool
    detail: str
    deliberate: bool = False


def _refused(detail: str) -> WriteSwitchState:
    """Refused because the state could not be established. Never deliberate."""
    return WriteSwitchState(writes_allowed=False, detail=detail, deliberate=False)


class WriteSwitch:
    """Reads the state file fresh, every time it is asked."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        if not self._path.is_absolute():
            raise WriteSwitchConfigError(
                "the write switch path must be absolute, so that it cannot "
                "depend on a service's working directory"
            )

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> WriteSwitchState:
        """The current state. Any doubt whatsoever is a refusal."""
        try:
            status = os.lstat(self._path)
        except FileNotFoundError:
            return _refused("write switch state file is missing")
        except OSError as exc:
            return _refused(
                f"write switch state file could not be stat'ed ({type(exc).__name__})"
            )

        # Decided from the `lstat` above rather than from a second call, so the
        # type checked and the size checked describe the same inode.
        #
        # A symlink is refused rather than followed. Following one would move the
        # decision to a path this configuration never named, and the whole point
        # of the switch is that its answer comes from a known place.
        if stat.S_ISLNK(status.st_mode):
            return _refused("write switch state file is a symlink")
        if not stat.S_ISREG(status.st_mode):
            return _refused("write switch state path is not a regular file")
        if status.st_size > MAX_STATE_FILE_BYTES:
            return _refused("write switch state file is implausibly large")

        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            return _refused(
                f"write switch state file could not be read ({type(exc).__name__})"
            )
        except UnicodeDecodeError:
            return _refused("write switch state file is not UTF-8")

        return _interpret(raw)


def _interpret(raw: str) -> WriteSwitchState:
    """Turn the file's bytes into a decision, recognising nothing extra."""
    try:
        document: Any = json.loads(raw)
    except ValueError:
        return _refused("write switch state file is not valid JSON")
    if not isinstance(document, dict):
        return _refused("write switch state file is not a JSON object")

    keys = set(document)
    missing = sorted(_REQUIRED_KEYS - keys)
    if missing:
        return _refused(f"write switch state file is missing {missing}")
    unknown = sorted(keys - _REQUIRED_KEYS - _OPTIONAL_KEYS)
    if unknown:
        # An unknown key means the file was written by something that disagrees
        # with this build about the format. Ignoring it would mean guessing
        # which of the two is right about a safety control.
        return _refused(f"write switch state file carries unknown keys {unknown}")

    state = document["writes"]
    if not isinstance(state, str) or state not in _RECOGNISED_STATES:
        return _refused("write switch state is not a value this build recognises")
    if state == WRITES_DISABLED:
        return WriteSwitchState(
            writes_allowed=False,
            detail="external writes are disabled by the write kill switch",
            deliberate=True,
        )
    return WriteSwitchState(
        writes_allowed=True,
        detail="external writes are enabled",
        deliberate=True,
    )


class _PermanentlyDisabledWriteSwitch(WriteSwitch):
    """A switch that is off and cannot be turned on, for contexts that must not
    write at all -- the restore probe on a recovery host, for instance.

    There is deliberately no `permanently_enabled` counterpart. That would be a
    way to obtain writes without a state file, which is precisely the thing the
    fail-closed design exists to prevent; a test that needs writes enabled must
    write a real state file and exercise the real reader.
    """

    def __init__(self, reason: str) -> None:
        # It never touches the filesystem, so the path is a label rather than a
        # location; the base class's absolute-path rule still has to hold.
        super().__init__(Path("/nonexistent/write-switch-permanently-disabled"))
        self._reason = reason

    def read(self) -> WriteSwitchState:
        # Deliberate: somebody chose this composition, so an operator view must
        # not present it as a fault.
        return WriteSwitchState(
            writes_allowed=False, detail=self._reason, deliberate=True
        )


def disabled_write_switch(reason: str) -> WriteSwitch:
    """A switch pinned off, for a composition that must never write."""
    if not reason.strip():
        raise WriteSwitchConfigError("a pinned-off switch must state why")
    return _PermanentlyDisabledWriteSwitch(reason)


def load_write_switch(env: dict[str, str] | None = None) -> WriteSwitch:
    """The switch a service must hold before it may serve any write tool."""
    env = env if env is not None else dict(os.environ)
    configured = (env.get(WRITE_SWITCH_PATH_ENV) or "").strip()
    if not configured:
        raise WriteSwitchConfigError(
            f"set {WRITE_SWITCH_PATH_ENV} to the write kill switch state file; "
            "a service that cannot find its kill switch must not start"
        )
    return WriteSwitch(Path(configured))


def render_state_file(
    *,
    writes: str,
    reason: str,
    changed_at: str,
) -> str:
    """The exact bytes an operator tool must write. One writer, one format."""
    if writes not in _RECOGNISED_STATES:
        raise WriteSwitchConfigError(f"unknown write switch state {writes!r}")
    if not reason.strip():
        raise WriteSwitchConfigError(
            "a switch change must carry a reason: the next person to read this "
            "file is the operator during an incident, possibly you, months later"
        )
    return (
        json.dumps(
            {"writes": writes, "reason": reason, "changed_at": changed_at},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
