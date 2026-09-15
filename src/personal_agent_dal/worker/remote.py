"""The remote HTTP worker transport (DAL-R06).

This is the client half of the frozen `worker-transport-v1` contract: the Mac
mini worker reaches the ECS Dev Workflow Service over outbound HTTPS only, and
never opens the Workflow database or listens on a port.

The security properties this module is responsible for, all fail-closed:

* **The endpoint is pinned, not discovered.** The base URL is validated at
  config load; every request path is a fixed template appended to it. A redirect
  is a refusal, never a hop — `follow_redirects` is off and any 3xx raises, so a
  compromised or misconfigured server cannot walk a bearer token to another host.
* **A credential only ever travels to that pinned endpoint.** The enrollment
  secret is read from an owner-only file at the moment of enrollment and is sent
  to `/enroll` alone. The token it returns is cached owner-only on the worker,
  is never logged, and is never placed in a URL.
* **Every request body is digest-bound.** `X-Transport-Body-Digest` is the
  sha256 of the exact bytes written to the socket, computed from the same buffer
  that is sent, so the fenced fields inside (`job_id`, `lease_epoch`) cannot be
  altered in flight without the server rejecting the request.
* **Every response is validated against a closed shape.** A response missing a
  field, carrying an unknown one, or wearing the wrong `schema_version` is a
  refusal. Nothing is defaulted, truncated or repaired.
* **Retries are bounded and idempotent.** A retried write reuses its
  `request_id` and its digest, so a lost response replays as the same fact
  rather than becoming a second one; a 409 conflict is surfaced as a conflict
  and never retried into an overwrite.

Reclamation of an expired lease is absent here on purpose: it is ECS authority,
triggered on the service's own `/jobs/claim` path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json

from personal_agent_dal.worker import checkpoint as checkpoint_mod
from personal_agent_dal.worker.checkpoint import CheckpointBundle
from personal_agent_dal.worker.transport import (
    CHECKPOINT_SENSITIVITY,
    WORKER_TERMINAL_STATES,
    CheckpointOutcome,
    HeartbeatOutcome,
    JobLease,
    ResultOutcome,
    TransportDisabledError,
    TransportError,
    WorkerTransport,
    checkpoint_artifact,
)

SCHEMA_VERSION: Final[str] = "dal.worker-transport/1.0"
TOKEN_CACHE_SCHEMA: Final[str] = "dal.worker-token-cache/1.0"

BODY_DIGEST_HEADER: Final[str] = "X-Transport-Body-Digest"
ENROLLMENT_SECRET_HEADER: Final[str] = "X-Enrollment-Secret"

#: Refresh a token this many seconds before it expires, so a request in flight
#: cannot cross the expiry boundary and be rejected for a clock difference.
TOKEN_REFRESH_SKEW_SECONDS: Final[int] = 60

#: A `/enroll` is attempted at most once per request; a second 401 is a refusal.
_MAX_REAUTH: Final[int] = 1

#: The only shapes accepted off the wire for values that will reach a `git`
#: argv, a URL path or a filesystem path. The 40-hex base SHA is the frozen
#: contract's own pattern; the id charset covers the service's UUIDs.
_SHA40_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")
_SHA64_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_SAFE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

#: The exact field set of every response this client accepts.
_CLAIM_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "job_id",
        "feature_id",
        "repository_id",
        "base_sha",
        "branch_name",
        "toolchain_ref",
        "lease_epoch",
        "attempt",
        "deadline",
        # F7 round-2 finding 6: the intake body and its digest ride the claim
        # response so a remote worker's coder prompt can substitute
        # {task_description} exactly as the local transport does. Optional —
        # a job enqueued without an intake (operator/test seeding) omits
        # them, and an older server omits them for every job.
        "task_description",
        "task_description_sha256",
    }
)
_HEARTBEAT_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "cancel_requested"}
)
_CHECKPOINT_FIELDS: Final[frozenset[str]] = frozenset({"schema_version", "recorded"})
_RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "job_id", "result_sha256", "receipt_id", "replay"}
)
_ENROLL_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "worker_id", "capabilities", "token", "token_expires_at"}
)


class EndpointError(ValueError):
    """The configured endpoint is not a pinnable HTTPS origin."""


def validate_endpoint(endpoint: str) -> str:
    """Return the normalised pinned base URL, or raise `EndpointError`.

    Only an absolute `https` URL with a host and no credentials, query or
    fragment can be pinned. The path is kept (the service may be mounted under a
    prefix) but normalised without a trailing slash so path joining is exact.
    """
    parts = urlsplit(endpoint)
    if parts.scheme != "https":
        raise EndpointError("endpoint must use https")
    if not parts.hostname:
        raise EndpointError("endpoint must have a host")
    if parts.username or parts.password:
        raise EndpointError("endpoint must not carry credentials")
    if parts.query or parts.fragment:
        raise EndpointError("endpoint must not carry a query or fragment")
    path = parts.path.rstrip("/")
    if ".." in path.split("/"):
        raise EndpointError("endpoint path must not be relative")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _read_owner_only(path: Path, label: str) -> bytes:
    """Read an owner-only (0600) secret file, or refuse."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise TransportError(f"{label}_unreadable:{type(error).__name__}") from error
    if mode != 0o600:
        raise TransportError(f"{label}_not_owner_only")
    value = path.read_bytes().strip()
    if not value:
        raise TransportError(f"{label}_empty")
    return value


@dataclass(frozen=True)
class CachedToken:
    """A worker-local bearer token and its non-secret expiry."""

    worker_id: str
    token: str
    expires_at: int


def read_token_expiry(path: Path) -> tuple[str, int] | None:
    """Return `(worker_id, expires_at)` from the cache without reading the token.

    The healthcheck uses this: it proves a usable credential is present and
    unexpired while never touching the secret itself.
    """
    try:
        body = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("schema_version") != TOKEN_CACHE_SCHEMA:
        return None
    worker_id = body.get("worker_id")
    expires_at = body.get("expires_at")
    if not isinstance(worker_id, str) or not worker_id:
        return None
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return None
    return worker_id, expires_at


def _load_token(path: Path) -> CachedToken | None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode != 0o600:
        # A world- or group-readable token cache is treated as absent: re-enroll
        # rather than use a credential whose custody is already broken.
        return None
    try:
        body = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("schema_version") != TOKEN_CACHE_SCHEMA:
        return None
    worker_id, token, expires_at = (
        body.get("worker_id"),
        body.get("token"),
        body.get("expires_at"),
    )
    if not isinstance(worker_id, str) or not worker_id:
        return None
    if not isinstance(token, str) or not token:
        return None
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return None
    return CachedToken(worker_id=worker_id, token=token, expires_at=expires_at)


def _store_token(path: Path, cached: CachedToken) -> None:
    """Write the token cache owner-only, atomically, never through a symlink."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.parent / f".{path.name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": TOKEN_CACHE_SCHEMA,
                    "worker_id": cached.worker_id,
                    "expires_at": cached.expires_at,
                    "token": cached.token,
                },
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _closed(payload: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    """Validate a response against its exact field set, or refuse.

    Unknown keys are always refused (the closed-set guarantee). The claim's
    two intake-body keys may be *absent* — a job without an intake, or an
    older server, omits them — so the claim path validates against the base
    set plus any subset of the optional keys.
    """
    if not isinstance(payload, dict):
        raise TransportError(f"{what}_not_an_object")
    if what == "claim":
        optional = {"task_description", "task_description_sha256"}
        if not set(payload) <= fields or not (fields - optional) <= set(payload):
            raise TransportError(f"{what}_shape")
    elif set(payload) != fields:
        raise TransportError(f"{what}_shape")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise TransportError(f"{what}_schema_version")
    return payload


def _error_code(response: httpx.Response) -> str:
    """Extract the contract's bounded error code; never echo a response body."""
    try:
        body = response.json()
    except ValueError:
        return "unparseable"
    if not isinstance(body, dict):
        return "unparseable"
    code = body.get("code")
    if not isinstance(code, str) or not code or len(code) > 64:
        return "unparseable"
    # The code is printed into logs and exception text. Restrict it to printable
    # ASCII so a control character or ANSI escape in a hostile response cannot
    # forge log lines.
    if not code.isascii() or not code.isprintable():
        return "unparseable"
    return code


@dataclass(frozen=True)
class RemoteTransportSettings:
    """Everything the remote adapter needs, already validated by the loader."""

    endpoint: str
    worker_id: str
    machine_id: str
    capabilities: tuple[str, ...]
    enrollment_secret_path: Path
    token_cache_path: Path
    checkpoint_root: Path
    ca_bundle_path: Path | None
    request_timeout_seconds: float
    retry_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float


class RemoteHttpAdapter(WorkerTransport):
    """The `WorkerTransport` backed by the pinned Dev Workflow Service."""

    def __init__(
        self,
        settings: RemoteTransportSettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now_epoch: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._now_epoch = now_epoch
        self._owns_client = client is None
        # The production client is built here, from the pinned endpoint: TLS
        # verification on, redirects off, one bounded timeout. Tests may inject a
        # client to reach an in-process app, but they cannot make production skip
        # this construction, because production passes no client.
        self._client = client or httpx.Client(
            base_url=settings.endpoint,
            verify=str(settings.ca_bundle_path) if settings.ca_bundle_path else True,
            follow_redirects=False,
            timeout=settings.request_timeout_seconds,
            # A tampered HTTPS_PROXY / *_PROXY env var must not reroute the
            # connection (CLAUDE.md §5.1): trust_env=False ignores them.
            trust_env=False,
        )
        self._token: CachedToken | None = None

    # --- credentials ---------------------------------------------------------

    def _current_token(self) -> str:
        """Return a usable bearer token, enrolling only when one is needed."""
        if self._token is None:
            self._token = _load_token(self._settings.token_cache_path)
        cached = self._token
        if (
            cached is not None
            and cached.worker_id == self._settings.worker_id
            and cached.expires_at - TOKEN_REFRESH_SKEW_SECONDS > self._now_epoch()
        ):
            return cached.token
        return self._enroll()

    def _enroll(self) -> str:
        """Exchange the operator-delivered enrollment secret for a token."""
        secret = _read_owner_only(
            self._settings.enrollment_secret_path, "enrollment_secret"
        )
        body = {
            "schema_version": SCHEMA_VERSION,
            "request_id": new_id(),
            "worker_id": self._settings.worker_id,
            "machine_id": self._settings.machine_id,
            "capabilities": list(self._settings.capabilities),
        }
        response = self._send(
            "/enroll",
            body,
            headers={ENROLLMENT_SECRET_HEADER: secret.decode("utf-8")},
        )
        if response.status_code != 200:
            raise TransportError(f"enroll_refused:{_error_code(response)}")
        payload = _closed(_json(response), _ENROLL_FIELDS, "enroll")
        token, expires_at = payload["token"], payload["token_expires_at"]
        if not isinstance(token, str) or not token:
            raise TransportError("enroll_token_shape")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise TransportError("enroll_expiry_shape")
        if payload["worker_id"] != self._settings.worker_id:
            raise TransportError("enroll_worker_mismatch")
        cached = CachedToken(
            worker_id=self._settings.worker_id, token=token, expires_at=expires_at
        )
        _store_token(self._settings.token_cache_path, cached)
        self._token = cached
        return token

    # --- transport primitives ------------------------------------------------

    def _send(
        self, path: str, body: dict[str, Any], *, headers: dict[str, str]
    ) -> httpx.Response:
        """One request: exact bytes, digest of those bytes, no redirect.

        The digest is computed from `payload` — the same object handed to
        `client.post` — so the header and the socket cannot disagree.
        """
        payload = canonical_json(body).encode("utf-8")
        request_headers = {
            "content-type": "application/json",
            BODY_DIGEST_HEADER: hashlib.sha256(payload).hexdigest(),
            **headers,
        }
        try:
            response = self._client.post(path, content=payload, headers=request_headers)
        except httpx.HTTPError as error:
            raise TransportError(f"network:{type(error).__name__}") from error
        if 300 <= response.status_code < 400:
            # A redirect would be the one way a bearer token could reach a host
            # that is not the pinned one. Refuse instead of following.
            raise TransportError("unexpected_redirect")
        return response

    def _authenticated(
        self,
        path: str,
        body: dict[str, Any],
        *,
        job_id: str | None = None,
        retry_server_errors: bool = True,
    ) -> httpx.Response:
        """Send an authenticated request with bounded retries.

        `body` is built once by the caller and reused verbatim across retries,
        so a retry after a lost response is the identical fact — the same
        `request_id`, the same digest — rather than a new one.

        `retry_server_errors=False` is reserved for `/jobs/claim`, the one
        endpoint where replay is *not* idempotent: a 5xx after the server
        committed a claim and lost the response would make a retry claim a
        second job and strand the first. Claim therefore fails on a server
        error and lets the next poll cycle (or lease expiry) recover. The 401
        re-auth path stays in place for every endpoint because it happens before
        any handler runs, so re-sending a claim after a rejected token has not
        claimed anything yet.
        """
        attempts = 0
        reauths = 0
        while True:
            token = self._current_token()
            response = self._send(
                path, body, headers={"authorization": f"Bearer {token}"}
            )
            status = response.status_code
            if status == 401 and reauths < _MAX_REAUTH:
                # The token was rejected: drop it and enroll exactly once more.
                reauths += 1
                self._token = None
                self._enroll()
                continue
            if status == 401:
                raise TransportError("unauthorized", job_id=job_id)
            if status == 403:
                raise TransportDisabledError(
                    f"forbidden:{_error_code(response)}", job_id=job_id
                )
            if status == 503 and _error_code(response) == "kill_switch_active":
                raise TransportDisabledError("kill_switch_active", job_id=job_id)
            if status == 429 or status >= 500:
                if not retry_server_errors:
                    raise TransportError(f"unavailable:{status}", job_id=job_id)
                attempts += 1
                if attempts > self._settings.retry_attempts:
                    raise TransportError(f"unavailable:{status}", job_id=job_id)
                self._sleep(self._backoff(attempts))
                continue
            return response

    def _backoff(self, attempt: int) -> float:
        delay = self._settings.backoff_base_seconds * (2 ** (attempt - 1))
        return min(delay, self._settings.backoff_max_seconds)

    # --- WorkerTransport -----------------------------------------------------

    def reclaim_expired(self) -> tuple[str, ...]:
        """No-op by design: reclaiming an expired lease is ECS authority.

        There is no endpoint for it and this worker must not have one — a remote
        worker able to expire another worker's lease would be a second authority
        over the same rows. The service reclaims on its own `/jobs/claim`.
        """
        return ()

    def claim(self) -> JobLease | None:
        body = {
            "schema_version": SCHEMA_VERSION,
            "request_id": new_id(),
            "worker_id": self._settings.worker_id,
        }
        response = self._authenticated("/jobs/claim", body, retry_server_errors=False)
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise TransportError(f"claim_refused:{_error_code(response)}")
        payload = _closed(_json(response), _CLAIM_FIELDS, "claim")
        return _lease_from(payload)

    def _prelaunch_request(self, lease, operation, **extra):
        response = self._authenticated('/worker/jobs/'+lease.job_id+'/'+operation,
            {'job_lease_epoch':lease.lease_epoch, **extra}, retry_server_errors=False)
        if response.status_code != 200:
            raise TransportError('prelaunch_refused:'+_error_code(response), job_id=lease.job_id)
        body = _json(response)
        fields = {'context'} if operation == 'prelaunch-context' else (
            {'manifest_sha256'} if operation == 'prelaunch-manifest' else {'code'})
        if not isinstance(body, dict) or set(body) != fields:
            raise TransportError('prelaunch_response_shape', job_id=lease.job_id)
        return body

    def prelaunch_context(self, lease):
        return self._prelaunch_request(lease, 'prelaunch-context')['context']

    def acknowledge_prelaunch(self, lease, assertion):
        return self._prelaunch_request(lease, 'prelaunch-manifest', assertion=assertion)

    def dispatch_prelaunch(self, lease, manifest_sha256):
        return self._prelaunch_request(lease, 'prelaunch-dispatch', manifest_sha256=manifest_sha256)

    def mark_running(self, lease: JobLease) -> HeartbeatOutcome:
        # The contract has no separate `running` transition: the first heartbeat
        # is the start announcement, and it exercises the same lease fence, so a
        # lost or cancelled lease is observed here rather than assumed alive.
        return self.heartbeat(lease)

    def heartbeat(self, lease: JobLease) -> HeartbeatOutcome:
        body = {
            "schema_version": SCHEMA_VERSION,
            "request_id": new_id(),
            "job_id": lease.job_id,
            "worker_id": self._settings.worker_id,
            "lease_epoch": lease.lease_epoch,
        }
        response = self._authenticated(
            f"/jobs/{lease.job_id}/heartbeat", body, job_id=lease.job_id
        )
        if response.status_code == 409:
            return HeartbeatOutcome(alive=False, cancel_requested=False)
        if response.status_code != 200:
            raise TransportError(
                f"heartbeat_refused:{_error_code(response)}", job_id=lease.job_id
            )
        payload = _closed(_json(response), _HEARTBEAT_FIELDS, "heartbeat")
        cancelled = payload["cancel_requested"]
        if not isinstance(cancelled, bool):
            raise TransportError("heartbeat_cancel_shape", job_id=lease.job_id)
        return HeartbeatOutcome(alive=not cancelled, cancel_requested=cancelled)

    def record_checkpoint(
        self, lease: JobLease, bundle: CheckpointBundle, *, sequence: int
    ) -> CheckpointOutcome:
        # The local file is written first and is only a crash cache: the
        # authority's record is what makes a checkpoint globally recoverable, so
        # a rejected upload is a failure even though the file exists.
        checkpoint_mod.write_checkpoint(self._settings.checkpoint_root, bundle)
        artifact_sha256, artifact_size = checkpoint_artifact(bundle)
        body = {
            "schema_version": SCHEMA_VERSION,
            "request_id": new_id(),
            "job_id": lease.job_id,
            "worker_id": self._settings.worker_id,
            "lease_epoch": lease.lease_epoch,
            "sequence": sequence,
            "artifact_sha256": artifact_sha256,
            "artifact_size_bytes": artifact_size,
            "changed_files": list(bundle.changed_files),
            "sensitivity": CHECKPOINT_SENSITIVITY,
        }
        response = self._authenticated(
            f"/jobs/{lease.job_id}/checkpoint", body, job_id=lease.job_id
        )
        if response.status_code == 409:
            code = _error_code(response)
            return CheckpointOutcome(
                recorded=False, stale=code == "stale", conflict=code == "conflict"
            )
        if response.status_code != 200:
            raise TransportError(
                f"checkpoint_refused:{_error_code(response)}", job_id=lease.job_id
            )
        payload = _closed(_json(response), _CHECKPOINT_FIELDS, "checkpoint")
        if payload["recorded"] is not True:
            raise TransportError("checkpoint_not_recorded", job_id=lease.job_id)
        return CheckpointOutcome(recorded=True, stale=False, conflict=False)

    def submit_result(
        self,
        lease: JobLease,
        *,
        state: str,
        result_sha256: str | None,
        last_error: str | None,
    ) -> ResultOutcome:
        if state not in WORKER_TERMINAL_STATES:
            raise TransportError("invalid_terminal_state", job_id=lease.job_id)
        if result_sha256 is None:
            # The contract requires a digest on every result. A pre-execution
            # refusal has nothing to receipt, so it is not submitted as one: the
            # lease is left to expire and be reclaimed by the authority.
            raise TransportError("result_requires_digest", job_id=lease.job_id)
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request_id": new_id(),
            "job_id": lease.job_id,
            "worker_id": self._settings.worker_id,
            "lease_epoch": lease.lease_epoch,
            "result_sha256": result_sha256,
            "state": state,
        }
        if last_error is not None:
            body["last_error"] = last_error
        response = self._authenticated(
            f"/jobs/{lease.job_id}/result", body, job_id=lease.job_id
        )
        if response.status_code == 409:
            code = _error_code(response)
            return ResultOutcome(
                accepted=False,
                replay=False,
                receipt_id=None,
                conflict=code == "conflict",
                cancelled=code == "cancelled",
                stale=code == "stale",
            )
        if response.status_code != 200:
            raise TransportError(
                f"result_refused:{_error_code(response)}", job_id=lease.job_id
            )
        payload = _closed(_json(response), _RESULT_FIELDS, "result")
        if payload["job_id"] != lease.job_id:
            raise TransportError("result_job_mismatch", job_id=lease.job_id)
        if payload["result_sha256"] != result_sha256:
            raise TransportError("result_digest_mismatch", job_id=lease.job_id)
        replay = payload["replay"]
        receipt_id = payload["receipt_id"]
        if (
            not isinstance(replay, bool)
            or not isinstance(receipt_id, str)
            or not receipt_id
        ):
            # An empty receipt is not evidence: the worker must not accept a
            # result it cannot point at. Fail closed rather than trust the
            # server's assertion that a receipt exists.
            raise TransportError("result_shape", job_id=lease.job_id)
        return ResultOutcome(
            accepted=True,
            replay=replay,
            receipt_id=receipt_id,
            conflict=False,
            cancelled=False,
            stale=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise TransportError("unparseable_response") from error


def _lease_from(payload: dict[str, Any]) -> JobLease:
    """Build a lease from a validated claim response, refusing bad field types.

    The pattern gates here are the adapter's own fail-closed boundary: a value
    that is about to reach a `git` argv (`base_sha`), a URL path (`job_id`) or a
    filesystem path (`feature_id`) is refused the moment it arrives, rather than
    being let through to be caught by whichever downstream call trips over it.
    """
    for key in ("job_id", "feature_id", "repository_id", "branch_name",
                "toolchain_ref", "deadline"):
        value = payload[key]
        if not isinstance(value, str) or not value:
            raise TransportError(f"claim_field:{key}")
    if _SAFE_ID_RE.fullmatch(payload["job_id"]) is None:
        raise TransportError("claim_field:job_id")
    if _SAFE_ID_RE.fullmatch(payload["feature_id"]) is None:
        raise TransportError("claim_field:feature_id")
    base_sha = payload["base_sha"]
    if not isinstance(base_sha, str) or _SHA40_RE.fullmatch(base_sha) is None:
        raise TransportError("claim_field:base_sha")
    for key in ("lease_epoch", "attempt"):
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TransportError(f"claim_field:{key}")
    try:
        deadline = datetime.fromisoformat(payload["deadline"])
    except ValueError as error:
        raise TransportError("claim_field:deadline") from error
    if deadline.tzinfo is None:
        # A naive deadline cannot fence against clock skew. The authority emits
        # UTC (the `isoformat()` of a tz-aware `utc_now()`), so anything else is
        # off-shape, not a value to repair.
        raise TransportError("claim_field:deadline")
    # The intake body pair is optional: ABSENT for a job enqueued without an
    # intake (or from an older server). Presence is decided by key
    # membership, never by value: an explicit null is a malformed shape, not
    # an omission (round-3 review finding 2 — .get() could not tell them
    # apart, and null-shaped pairs sailed through as bodyless leases,
    # skipping the digest fence). If either key is present, BOTH must be
    # present, non-null, correctly typed, and the digest 64-hex.
    body_present = "task_description" in payload
    body_sha_present = "task_description_sha256" in payload
    if body_present or body_sha_present:
        if not (body_present and body_sha_present):
            raise TransportError("claim_field:task_description")
        body = payload["task_description"]
        body_sha = payload["task_description_sha256"]
        if not isinstance(body, str) or not body:
            raise TransportError("claim_field:task_description")
        if (
            not isinstance(body_sha, str)
            or _SHA64_RE.fullmatch(body_sha) is None
        ):
            raise TransportError("claim_field:task_description_sha256")
    else:
        body = None
        body_sha = None
    return JobLease(
        job_id=payload["job_id"],
        feature_id=payload["feature_id"],
        repository_id=payload["repository_id"],
        base_sha=payload["base_sha"],
        branch_name=payload["branch_name"],
        toolchain_ref=payload["toolchain_ref"],
        lease_epoch=payload["lease_epoch"],
        attempt=payload["attempt"],
        deadline=deadline,
        task_description=body,
        task_description_sha256=body_sha,
    )
