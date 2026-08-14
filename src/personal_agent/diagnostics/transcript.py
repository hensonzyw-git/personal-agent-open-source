"""The full input and output of every message, written for diagnosis only.

The durable Timeline stores what a turn *meant*: the user's message, the
operation's state walk, the result, the audit envelope of any write. It does
not store what the model was actually shown or what it actually said, so a turn
that produced a wrong-looking answer cannot be explained after the fact.

This module fills exactly that gap and nothing else. It is a **write-only
sink**: no code path reads a transcript back, and in particular nothing here
ever reaches context assembly. That boundary is what keeps this compatible with
the orchestrator's existing decision not to retain untrusted model prose as a
user-visible result, tool argument or write evidence -- prose recorded here can
become none of those things, because it is never read.

Three properties are load-bearing:

* **It is off unless a directory is configured.** ``PERSONAL_AGENT_TRANSCRIPT_DIR``
  is the switch. The records contain the owner's own expressions, amounts and
  merchant names in plaintext, unlike ``conversation_events``, which are sealed.
  Henson chose plaintext deliberately (2026-08-14) so a transcript can be
  grepped and replayed into an eval set without a key, and bounded the exposure
  with the switch plus retention rather than with encryption.
* **A leak check runs before every write**, never after, and never as a later
  "correctness" step (AGENTS.md §5.2). Secrets in the environment are scrubbed
  from the serialised line even though no known field carries one.
* **Recording never fails an operation.** A diagnostic sink that could refuse a
  write, or slow one down enough to matter, would be a worse defect than the
  blindness it removes. Every failure is caught, and each one is reported on the
  service logger so a gap in the file is visible rather than silent.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Final

from personal_agent_core.timeutil import utc_now

logger = logging.getLogger(__name__)

#: The switch. Unset or blank means no transcript is written at all.
DIRECTORY_ENV: Final = "PERSONAL_AGENT_TRANSCRIPT_DIR"
#: Whole days of transcript kept. Files older than this are deleted on the
#: first record of a new day.
RETENTION_ENV: Final = "PERSONAL_AGENT_TRANSCRIPT_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS: Final = 14

REDACTED: Final = "[REDACTED]"

#: Environment variables whose *values* are scrubbed from every line. The
#: pattern is deliberately broad: a name that merely looks credential-bearing
#: costs one redaction, while a missed one is an exposure.
_SECRET_NAME: Final = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PASSPHRASE")
#: Below this length a "secret" is noise (``"1"``, ``"true"``) and scrubbing it
#: would corrupt unrelated text without protecting anything.
_MIN_SECRET_LENGTH: Final = 8

_FILENAME: Final = re.compile(r"^(?P<service>[a-z0-9-]+)-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl$")
_SERVICE_NAME: Final = re.compile(r"^[a-z0-9-]+$")

# Record kinds. One turn is reassembled by grouping on `turn.operation_id`.
USER_MESSAGE: Final = "user_message"
MODEL_REQUEST: Final = "model_request"
MODEL_RESPONSE: Final = "model_response"
MODEL_FAILURE: Final = "model_failure"
TOOL_CALL: Final = "tool_call"
TOOL_RESULT: Final = "tool_result"
TURN_RESULT: Final = "turn_result"
API_RESPONSE: Final = "api_response"


@dataclass(frozen=True)
class TurnIdentity:
    """The correlation keys every record of one message carries.

    ``operation_id`` is the join key; the rest are what makes a transcript
    searchable against the Timeline and the operation log without opening the
    database.
    """

    operation_id: str | None = None
    client_request_id: str | None = None
    trace_id: str | None = None
    turn_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    device_id: str | None = None


#: Set for the duration of one message by the worker that runs it. A context
#: variable rather than a parameter because the model adapter sits several
#: framework-owned frames below the orchestrator, and threading an identity
#: through ADK's contract would mean changing that contract.
CURRENT_TURN: contextvars.ContextVar[TurnIdentity | None] = contextvars.ContextVar(
    "personal_agent_transcript_turn", default=None
)


class Recorder:
    """The recording seam. See :class:`NullRecorder` for the disabled shape."""

    @property
    def enabled(self) -> bool:
        """Whether a record written now would reach a sink.

        `record` is always safe to call, so this exists for one purpose: a call
        site that must do real work merely to *assemble* a payload -- open a
        database session, re-read an anchor -- can skip that work when nothing
        would be written. Never use it to branch on what gets recorded.
        """
        raise NotImplementedError

    @contextmanager
    def turn(self, identity: TurnIdentity) -> Iterator[None]:
        """Bind every record written inside this block to one message."""
        token = CURRENT_TURN.set(identity)
        try:
            yield
        finally:
            CURRENT_TURN.reset(token)

    def record(self, kind: str, payload: Mapping[str, Any]) -> None:
        raise NotImplementedError


class NullRecorder(Recorder):
    """What every caller gets when no transcript directory is configured.

    A no-op object rather than ``None`` so that call sites keep one uniform
    signature and never grow an ``if recorder is not None`` branch that a later
    change could get wrong on one path only (AGENTS.md §5.2).
    """

    @property
    def enabled(self) -> bool:
        return False

    def record(self, kind: str, payload: Mapping[str, Any]) -> None:
        return None


class TranscriptRecorder(Recorder):
    """Append-only JSONL, one file per service per UTC day, mode ``0600``.

    The file is named for the service because the API, the review job and the
    MCP server are separate processes. Giving each its own file removes any
    question of two processes interleaving halves of a long line, which a single
    shared file could not guarantee for records of this size.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        service: str,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        secrets: frozenset[str] = frozenset(),
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if not _SERVICE_NAME.match(service):
            raise ValueError("transcript service must match [a-z0-9-]+")
        if retention_days <= 0:
            raise ValueError("transcript retention days must be positive")
        self._directory = Path(directory)
        self._service = service
        self._retention_days = retention_days
        # Longest first: a shorter secret that is a substring of a longer one
        # must not leave the longer one's tail behind.
        self._secrets = tuple(
            sorted(
                {value for value in secrets if len(value) >= _MIN_SECRET_LENGTH},
                key=len,
                reverse=True,
            )
        )
        self._now = now
        self._lock = threading.Lock()
        self._expired_on: str | None = None
        # Startup, not first message: a directory that cannot be created is a
        # deployment error and should surface where the service is composed.
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def directory(self) -> Path:
        return self._directory

    def record(self, kind: str, payload: Mapping[str, Any]) -> None:
        try:
            moment = self._now()
            identity = CURRENT_TURN.get()
            envelope = {
                "recorded_at": moment.isoformat(),
                "service": self._service,
                "kind": kind,
                "turn": None if identity is None else asdict(identity),
                "payload": jsonable(payload),
            }
            line = json.dumps(envelope, ensure_ascii=False, default=_opaque)
            # Before the write, always. A credential that reached a payload is
            # an exposure the moment the line lands on disk, so this cannot be
            # deferred to a later validation step (AGENTS.md §5.2).
            line = self._scrubbed(line)
            self._append(line, moment)
        except Exception:  # noqa: BLE001 - a transcript must not break a turn
            # Reported, never swallowed: the gap in the file is now explained by
            # a line in the journal instead of looking like a turn that never ran.
            logger.warning("transcript record dropped kind=%s", kind, exc_info=True)

    def _scrubbed(self, line: str) -> str:
        for secret in self._secrets:
            if secret in line:
                line = line.replace(secret, REDACTED)
            # The same value as JSON would escape it -- a secret containing a
            # quote or backslash reaches disk in this form, not the raw one.
            escaped = json.dumps(secret)[1:-1]
            if escaped != secret and escaped in line:
                line = line.replace(escaped, REDACTED)
        return line

    def _append(self, line: str, moment: datetime) -> None:
        path = self._directory / f"{self._service}-{moment.strftime('%Y-%m-%d')}.jsonl"
        with self._lock:
            self._expire(moment)
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(handle, 0o600)
                data = memoryview(f"{line}\n".encode())
                # POSIX permits a successful short write, notably when the
                # filesystem is close to full. Keep the process-local lock for
                # the whole loop so another record cannot enter between chunks.
                while data:
                    written = os.write(handle, data)
                    if written <= 0:  # pragma: no cover - defensive OS contract
                        raise OSError("transcript write made no progress")
                    data = data[written:]
            finally:
                os.close(handle)

    def _expire(self, moment: datetime) -> None:
        """Delete transcripts older than the retention window.

        Runs at most once per day. The marker is set before the work so a
        failing filesystem produces one warning a day rather than one per
        record; the files are then retried on the next day's first record.
        """
        today = moment.strftime("%Y-%m-%d")
        if self._expired_on == today:
            return
        self._expired_on = today
        try:
            purge_expired_transcripts(
                self._directory,
                retention_days=self._retention_days,
                now=moment,
            )
        except OSError:
            logger.warning(
                "transcript retention sweep failed dir=%s", self._directory, exc_info=True
            )


def _day_of(filename: str) -> date | None:
    matched = _FILENAME.match(filename)
    if matched is None:
        # Not a file this recorder wrote. Retention deletes only its own.
        return None
    try:
        return date.fromisoformat(matched.group("day"))
    except ValueError:  # pragma: no cover - the pattern already fixes the shape
        return None


def purge_expired_transcripts(
    directory: Path | str,
    *,
    retention_days: int,
    now: datetime,
) -> int:
    """Delete recorder-owned files outside the calendar-day retention window.

    This function is also called by the independent daily cleanup service, so
    expiry continues when capture is disabled or no new message arrives.
    ``retention_days=14`` keeps today plus the preceding thirteen UTC file days.
    Unknown files are never touched.
    """
    if retention_days <= 0:
        raise ValueError("transcript retention days must be positive")
    root = Path(directory)
    if not root.exists():
        return 0
    cutoff = now.date() - timedelta(days=retention_days - 1)
    deleted = 0
    for existing in sorted(root.glob("*.jsonl")):
        day = _day_of(existing.name)
        if day is not None and day < cutoff:
            existing.unlink(missing_ok=True)
            deleted += 1
    return deleted


def jsonable(value: Any) -> Any:
    """Render any object as JSON-safe data without ever raising.

    Unknown objects become an explicit ``{"__repr__": ...}`` marker rather than
    being dropped or coerced into something that reads like real data: a
    transcript that quietly loses a field is worse than one that says which
    field it could not render.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        # Never the content. Raw bytes at this boundary are as likely to be key
        # material as anything else, and their length is what diagnosis needs.
        return {"__bytes__": len(value)}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        # Pydantic, which is what ADK's response types are.
        try:
            return jsonable(dump(mode="json"))
        except Exception:  # noqa: BLE001 - fall back to the opaque marker
            pass
    return _opaque(value)


def _opaque(value: Any) -> dict[str, str]:
    try:
        return {"__repr__": repr(value)}
    except Exception:  # noqa: BLE001 - a __repr__ may itself raise
        return {"__repr__": f"<unrenderable {type(value).__name__}>"}


def secrets_from_environment(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Every environment value that looks credential-bearing.

    Collected once, at composition. The model key, the data keys, the signing
    keys and the cursor secret all match by name; anything a later deployment
    adds matches on the same pattern without this list being maintained.
    """
    source = os.environ if environ is None else environ
    return frozenset(
        value
        for name, value in source.items()
        if _SECRET_NAME.search(name.upper()) and len(value) >= _MIN_SECRET_LENGTH
    )


def recorder_from_env(
    *,
    service: str,
    environ: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = utc_now,
) -> Recorder:
    """Build the configured recorder, or the no-op one when the switch is off.

    A malformed retention value raises. Configuration is checked at startup,
    where an operator sees it; only *recording* is best-effort.
    """
    source = os.environ if environ is None else environ
    directory = (source.get(DIRECTORY_ENV) or "").strip()
    if not directory:
        return NullRecorder()
    raw = (source.get(RETENTION_ENV) or "").strip()
    if not raw:
        retention_days = DEFAULT_RETENTION_DAYS
    else:
        try:
            retention_days = int(raw)
        except ValueError as exc:
            raise ValueError(f"{RETENTION_ENV} must be an integer") from exc
    return TranscriptRecorder(
        directory,
        service=service,
        retention_days=retention_days,
        secrets=secrets_from_environment(source),
        now=now,
    )
