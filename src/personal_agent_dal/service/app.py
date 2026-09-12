"""DAL-R05 Dev Workflow Service — the thin-http transport composition root.

This is the server half of the frozen `worker-transport-v1` contract. It wraps
the queue/lease primitives in `personal_agent_dal.worker.queue` (CAS claim,
exact-epoch heartbeat, idempotent result with digest-conflict detection) behind
FastAPI endpoints, and adds the transport-specific bounds: ECS-held enrollment
secret + HMAC token auth, per-request body digest fencing, kill switch, a
streamed request-body cap, per-worker rate limit, and a redacted audit append.

It owns no business state machine: `worker_jobs` is the authority for "which
worker runs which job now"; feature/run authority stays with the business layer
this transport serves. Result acceptance happens inside `queue.finish_job`,
whose receipt insert and terminal transition share one server-side transaction;
checkpoint recording is likewise fenced and idempotent inside a single
transaction (`_record_checkpoint_fenced`), so a stale epoch is a 409 with zero
writes and a conflicting replay is a 409 conflict, never a silent overwrite.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, func, select

from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.github import executor
from personal_agent_dal.machine.action_recovery import recover_attempt
from personal_agent_dal.service.intake import IntakeRefusal, intake_task
from personal_agent_dal.service.operator_tokens import (
    OperatorTokenError,
    verify_operator_token,
)
from personal_agent_dal.service.tokens import TokenError, issue_token, verify_token
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.transport_models import (
    WORKER_CAPABILITIES,
    WorkerCheckpoint,
    WorkerEnrollment,
)
from personal_agent_dal.storage.worker_models import WORKER_JOB_STATES
from personal_agent_dal.worker import queue
from personal_agent_dal.worker.queue import ACTIVE_JOB_STATES

SCHEMA_VERSION = "dal.worker-transport/1.0"
OPERATOR_SCHEMA_VERSION = "dal.operator-transport/1.0"
OPERATOR_TOKEN_TTL_SECONDS = 3600
#: The single mutating operator action of this slice; pause/resume/
#: request-human/accept-result await the controller-side dispatch executor and
#: are contract-declared 501s (`operator_action_not_available`), not silent
#: gaps.
OPERATOR_MUTATIONS: Final[tuple[str, ...]] = ("cancel",)
#: Contract-declared 501s: these actions exist in the operator vocabulary but
#: await the controller-side dispatch executor. Any other action path is an
#: unknown verb and gets a closed 404, not a 501.
OPERATOR_DEFERRED_ACTIONS: Final[tuple[str, ...]] = (
    "pause",
    "resume",
    "request-human",
    "accept-result",
)
OPERATOR_JOBS_PAGE_MAX = 100
OPERATOR_JOBS_OFFSET_MAX = 10_000
MAX_BODY_BYTES = 1_048_576  # 1 MiB transport envelope cap, distinct from artifact cap
ARTIFACT_MAX_BYTES = 104_857_600
CHANGED_FILES_MAX = 10_000
CHANGED_FILE_MAX_LENGTH = 512
ID_MAX_LENGTH = 256
LAST_ERROR_MAX_LENGTH = 4096
DESCRIPTION_MAX_LENGTH = 8192
TOKEN_TTL_SECONDS = 3600
RATE_LIMIT_MAX_REQUESTS = 120
RATE_LIMIT_WINDOW_SECONDS = 60.0
LEASE_TTL_SECONDS = 60
MAX_ATTEMPTS = 3

BODY_DIGEST_HEADER: Final[str] = "x-transport-body-digest"
ENROLLMENT_SECRET_HEADER: Final[str] = "x-enrollment-secret"
_ENROLL_BUCKET: Final[tuple[str, str]] = ("enroll", "__global__")  # /enroll rate bucket

_SHA64_HEX = re.compile(r"[0-9a-f]{64}")


# --- closed request shapes (extra="forbid") ---------------------------------

_Id = Annotated[str, Field(min_length=1, max_length=ID_MAX_LENGTH)]
_ChangedFile = Annotated[str, Field(min_length=1, max_length=CHANGED_FILE_MAX_LENGTH)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrollRequest(_Closed):
    schema_version: Literal["dal.worker-transport/1.0"]
    request_id: _Id
    worker_id: _Id
    machine_id: _Id
    capabilities: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class ClaimRequest(_Closed):
    schema_version: Literal["dal.worker-transport/1.0"]
    request_id: _Id
    worker_id: _Id


class HeartbeatRequest(_Closed):
    schema_version: Literal["dal.worker-transport/1.0"]
    request_id: _Id
    job_id: _Id
    worker_id: _Id
    lease_epoch: int = Field(ge=0)


class CheckpointRequest(_Closed):
    schema_version: Literal["dal.worker-transport/1.0"]
    request_id: _Id
    job_id: _Id
    worker_id: _Id
    lease_epoch: int = Field(ge=0)
    sequence: int = Field(ge=0)
    artifact_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    artifact_size_bytes: int = Field(ge=1)
    changed_files: list[_ChangedFile]
    sensitivity: Literal["checkpoint", "diff", "log"]


class ResultRequest(_Closed):
    schema_version: Literal["dal.worker-transport/1.0"]
    request_id: _Id
    job_id: _Id
    worker_id: _Id
    lease_epoch: int = Field(ge=0)
    result_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    state: Literal["succeeded", "failed"]
    last_error: str | None = Field(default=None, min_length=1, max_length=LAST_ERROR_MAX_LENGTH)


# --- errors and envelopes ----------------------------------------------------


class StaleLeaseError(RuntimeError):
    """The lease fence failed at write time: unknown job, lost lease, old epoch."""


class CheckpointConflictError(RuntimeError):
    """Same (job_id, sequence) replayed with a different artifact digest."""


def _http(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _error_envelope(status: int, code: str, schema_version: str = SCHEMA_VERSION) -> JSONResponse:
    """The contract's closed `Error` shape; `code` carries the fail-closed reason."""
    return JSONResponse(
        status_code=status,
        content={"schema_version": schema_version, "code": code, "detail": code},
    )


# --- store helpers ----------------------------------------------------------


def _enrollment(engine: Engine, worker_id: str) -> WorkerEnrollment | None:
    sessions = session_factory(engine)
    with sessions() as session:
        return session.get(WorkerEnrollment, worker_id)


def _upsert_enrollment(
    engine: Engine, worker_id: str, machine_id: str, capabilities: list[str]
) -> None:
    sessions = session_factory(engine)

    def _body(session: Any) -> None:
        row = session.get(WorkerEnrollment, worker_id)
        caps = json.dumps(sorted(capabilities))
        if row is None:
            session.add(
                WorkerEnrollment(
                    worker_id=worker_id,
                    machine_id=machine_id,
                    capabilities=caps,
                    created_at=utc_now(),
                )
            )
        else:
            if row.revoked_at is not None:
                raise _http(403, "worker_revoked")
            row.machine_id = machine_id
            row.capabilities = caps

    with sessions() as session:
        run_write_transaction(session, lambda: _body(session))


def _record_checkpoint_fenced(
    engine: Engine,
    *,
    job_id: str,
    worker_id: str,
    lease_epoch: int,
    sequence: int,
    artifact_sha256: str,
    artifact_size_bytes: int,
    changed_files: list[str],
    sensitivity: str,
) -> str:
    """Insert one checkpoint atomically fenced by the live lease.

    The lease check and the insert share one `run_write_transaction`, so an
    epoch that goes stale mid-request is a `StaleLeaseError` with zero writes —
    the contract's "old epoch fails closed (409, zero write)" holds at the
    write, not at a stale pre-read. Idempotent on `(job_id, sequence)`: an
    identical replay returns the original checkpoint id; a different digest
    for the same sequence is a `CheckpointConflictError`, never an overwrite.
    """
    sessions = session_factory(engine)

    def _body(session: Any) -> str:
        jobs = queue.WorkerJob.__table__
        lease = session.execute(
            select(jobs.c.state, jobs.c.worker_id, jobs.c.lease_epoch).where(
                jobs.c.job_id == job_id
            )
        ).one_or_none()
        if (
            lease is None
            or lease.state not in ACTIVE_JOB_STATES
            or lease.worker_id != worker_id
            or lease.lease_epoch != lease_epoch
        ):
            raise StaleLeaseError(job_id)
        existing = session.execute(
            select(
                WorkerCheckpoint.checkpoint_id, WorkerCheckpoint.artifact_sha256
            ).where(
                WorkerCheckpoint.job_id == job_id,
                WorkerCheckpoint.sequence == sequence,
            )
        ).one_or_none()
        if existing is not None:
            if existing.artifact_sha256 != artifact_sha256:
                raise CheckpointConflictError(f"{job_id}#{sequence}")
            return existing.checkpoint_id
        checkpoint_id = new_id()
        session.add(
            WorkerCheckpoint(
                checkpoint_id=checkpoint_id,
                job_id=job_id,
                sequence=sequence,
                lease_epoch=lease_epoch,
                artifact_sha256=artifact_sha256,
                artifact_size_bytes=artifact_size_bytes,
                changed_files=json.dumps(changed_files),
                sensitivity=sensitivity,
                recorded_at=utc_now(),
            )
        )
        return checkpoint_id

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


def _append_redacted_audit(engine: Engine, *, event_type: str, outcome: str) -> None:
    """Best-effort redacted audit append (never carries ids, tokens or bodies)."""
    try:
        sessions = session_factory(engine)

        def _body(session: Any) -> None:
            append_audit_event(
                session,
                event_id=new_id(),
                trace_id=new_id(),
                event_type=event_type,
                redacted_summary=outcome,
                now=utc_now(),
            )

        with sessions() as session:
            run_write_transaction(session, lambda: _body(session))
    except Exception:  # noqa: BLE001 - audit is witness, not authority
        pass


# --- transport bounds and auth ----------------------------------------------


async def transport_body_guard(request: Request) -> None:
    """Bound the body (streamed, 1 MiB) and verify `X-Transport-Body-Digest`.

    Runs before auth on every POST. The cap is enforced on the actual received
    bytes, so a chunked body without a truthful `Content-Length` cannot bypass
    it; the buffered body is cached on the request for downstream parsing. The
    digest is the sha256 of the exact received bytes and binds every fenced
    field (request_id/job_id/lease_epoch) against tampering; a missing,
    malformed or mismatching digest is a 400, never a silent repair.
    """
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise _http(400, "invalid_content_length") from None
        if declared > MAX_BODY_BYTES:
            raise _http(413, "oversized_body")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise _http(413, "oversized_body")
    # Starlette caches `_body` for `request.body()`; downstream parsing reuses it.
    request._body = bytes(body)  # noqa: SLF001
    digest = request.headers.get(BODY_DIGEST_HEADER, "")
    if not _SHA64_HEX.fullmatch(digest):
        raise _http(400, "body_digest")
    if not hmac.compare_digest(hashlib.sha256(bytes(body)).hexdigest(), digest):
        raise _http(400, "body_digest")


class RateLimiter:
    """In-memory sliding window keyed by a structured identity bucket.

    The key is a tuple (identity domain, id), e.g. ``("worker", id)`` or
    ``("operator", id)``, so the two identity domains can never collide on
    one bucket string (a worker literally named like an operator id, or vice
    versa). Offline-slice stand-in for Nginx.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, bucket: tuple[str, str], now: float) -> bool:
        hits = self._hits[bucket]
        cutoff = now - self._window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self._max:
            return False
        hits.append(now)
        return True


class Service:
    """Holds the per-process transport state and the auth dependency factory."""

    def __init__(
        self,
        engine: Engine,
        *,
        service_key: bytes,
        enrollment_secret: bytes,
        kill_switch_path: Path | None,
        rate_limiter: RateLimiter,
        lease_ttl_seconds: int = LEASE_TTL_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if not service_key:
            raise ValueError("service key must be non-empty")
        if not enrollment_secret:
            raise ValueError("enrollment secret must be non-empty")
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.engine = engine
        self.service_key = service_key
        self.enrollment_secret = enrollment_secret
        self.kill_switch_path = kill_switch_path
        self.rate_limiter = rate_limiter
        # Lease duration and attempt budget are ECS authority: a worker never
        # proposes its own TTL, and never decides when its own lease expires.
        self.lease_ttl_seconds = lease_ttl_seconds
        self.max_attempts = max_attempts

    @property
    def kill_switch(self) -> bool:
        return self.kill_switch_path is not None and self.kill_switch_path.exists()

    def check_enrollment_secret(self, request: Request) -> None:
        """`/enroll` is operator-gated, never self-service: the ECS-held
        enrollment secret authenticates the registrar (constant-time)."""
        presented = request.headers.get(ENROLLMENT_SECRET_HEADER, "")
        if not presented or not hmac.compare_digest(
            presented.encode("utf-8"), self.enrollment_secret
        ):
            raise _http(401, "enrollment_secret_invalid")

    def auth(
        self, request: Request, _: None = Depends(transport_body_guard)
    ) -> tuple[str, list[str]]:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise _http(401, "missing_bearer_token")
        token = header[len("Bearer "):].strip()
        try:
            worker_id, capabilities = verify_token(
                token, key=self.service_key, now_epoch=int(time.time())
            )
        except TokenError as exc:
            raise _http(401, "token_invalid") from exc
        enrollment = _enrollment(self.engine, worker_id)
        if enrollment is None:
            raise _http(403, "unknown_worker")
        if enrollment.revoked_at is not None:
            # Revocation is observed here, fail-closed at the auth boundary.
            raise _http(403, "worker_revoked")
        if not self.rate_limiter.allow(("worker", worker_id), time.time()):
            raise _http(429, "rate_limited")
        return worker_id, capabilities


def _check_worker(body: BaseModel, token_worker_id: str, path_job_id: str) -> None:
    if getattr(body, "worker_id", None) != token_worker_id:
        raise _http(403, "worker_mismatch")
    if getattr(body, "job_id", None) != path_job_id:
        raise _http(400, "job_mismatch")


# --- operator identity and endpoints (DAL-R08, first slice) ------------------
#
# The operator plane is a separate identity domain on the same service: worker
# tokens and operator tokens are different schemas, each verifier refuses the
# foreign schema first, and no endpoint accepts both. Operator reads are
# paginated server-side; the only mutation of this slice is `cancel`, which is
# the existing ECS authority action (`queue.cancel_job`), bound to the job's
# live state by a CAS so a stale operator view cannot cancel a job that has
# already terminalized.


class OperatorActionRequest(_Closed):
    schema_version: Literal["dal.operator-transport/1.0"]
    request_id: _Id
    job_id: _Id
    action: Literal["cancel"]
    # State binding: the caller asserts the state its view showed; a mismatch
    # is 409, never a write based on a stale projection.
    expected_state: Literal["pending", "leased", "running"]


class EffectWakeRequest(_Closed):
    """The F5 wake body: the request IS the binding, nothing more.

    The closed schema is the structural guarantee behind the frozen scope:
    an operator cannot submit a branch, SHA, body, action or idempotency key
    because no field exists to carry them — extra keys are a 400 (the
    ``extra="forbid"`` base), not fields to ignore.
    """

    schema_version: Literal["dal.operator-transport/1.0"]
    request_id: _Id
    effect_id: _Id
    # The caller asserts the lifecycle state its view showed and the version
    # it was shown at; a mismatch is a refusal, never a write on a stale
    # projection.
    expected_state: Literal[
        "intent_recorded", "claimed", "unknown", "reconciling"
    ]
    expected_version: int = Field(ge=1)


class ProviderRecoveryRequest(_Closed):
    """An operator can request observation, never assert provider success."""

    schema_version: Literal["dal.operator-transport/1.0"]
    request_id: _Id
    attempt_id: _Id
    expected_version: int = Field(ge=1, strict=True)


class IntakeRequest(_Closed):
    """The intake body: a task request, nothing more.

    Behind ``extra="forbid"`` so an operator cannot smuggle a branch, SHA,
    action or idempotency key in — the closed schema is the structural
    guarantee that ``intake_task`` derives every such field server-side. The
    ``task_description`` is bounded to a real (non-empty) payload; the server
    content-addresses the feature id and derives the branch and idempotency
    key, so the request carries only what the operator knows about the task.
    """

    schema_version: Literal["dal.operator-transport/1.0"]
    request_id: _Id
    repository_id: _Id
    # The exact base commit the job is built from; must be a 40-hex SHA.
    base_sha: str = Field(min_length=40, max_length=40)
    # The repo-relative manifest the worker's toolchain is pinned to (DAL-019).
    toolchain_ref: _Id
    task_description: str = Field(min_length=1, max_length=DESCRIPTION_MAX_LENGTH)


class OperatorTokenIssueRequest(_Closed):
    schema_version: Literal["dal.operator-transport/1.0"]
    request_id: _Id
    operator_id: _Id
    capabilities: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class _OperatorAuth:
    """Auth dependency for operator endpoints (schema-separated, fail-closed)."""

    def __init__(self, service: "Service", required_capability: str) -> None:
        self._service = service
        self._required = required_capability

    def __call__(self, request: Request) -> str:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise _http(401, "missing_bearer_token")
        token = header[len("Bearer "):].strip()
        try:
            operator_id, capabilities = verify_operator_token(
                token, key=self._service.service_key, now_epoch=int(time.time())
            )
        except OperatorTokenError as exc:
            raise _http(401, "token_invalid") from exc
        if self._required not in capabilities:
            raise _http(403, "capability_missing")
        if not self._service.rate_limiter.allow(("operator", operator_id), time.time()):
            raise _http(429, "rate_limited")
        return operator_id


def _operator_identity_rows(engine: Engine, *, limit: int, offset: int) -> tuple[int, list[dict[str, Any]]]:
    """Server-side paginated job listing (count + one page), fresh reads."""
    sessions = session_factory(engine)
    with sessions() as session:
        table = queue.WorkerJob.__table__
        total = session.execute(
            select(func.count()).select_from(table)
        ).scalar_one()
        rows = session.execute(
            select(
                table.c.job_id,
                table.c.feature_id,
                table.c.repository_id,
                table.c.branch_name,
                table.c.state,
                table.c.attempt_count,
                table.c.lease_epoch,
                table.c.worker_id,
                table.c.result_sha256,
                table.c.updated_at,
            )
            .order_by(table.c.created_at.desc(), table.c.job_id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return (
            total,
            [
                {
                    "job_id": r.job_id,
                    "feature_id": r.feature_id,
                    "repository_id": r.repository_id,
                    "branch_name": r.branch_name,
                    "state": r.state,
                    "attempt": r.attempt_count,
                    "lease_epoch": r.lease_epoch,
                    "worker_id": r.worker_id,
                    "result_sha256": r.result_sha256,
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in rows
            ],
        )


# --- app factory ------------------------------------------------------------


def create_app(
    engine: Engine,
    *,
    service_key: bytes,
    enrollment_secret: bytes,
    kill_switch_path: Path | None = None,
    token_ttl_seconds: int = TOKEN_TTL_SECONDS,
    rate_limiter: RateLimiter | None = None,
    lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
    github_adapter: Any | None = None,
) -> FastAPI:
    service = Service(
        engine,
        service_key=service_key,
        enrollment_secret=enrollment_secret,
        kill_switch_path=kill_switch_path,
        rate_limiter=rate_limiter or RateLimiter(RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS),
        lease_ttl_seconds=lease_ttl_seconds,
        max_attempts=max_attempts,
    )
    app = FastAPI(title="DAL Worker Transport", version="1.0.0")

    def _envelope_for(request: Request) -> str:
        # Operator endpoints answer with the operator envelope; everything else
        # (including unmatched paths) with the worker transport envelope.
        if request.url.path.startswith("/operator"):
            return OPERATOR_SCHEMA_VERSION
        return SCHEMA_VERSION

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error_envelope(exc.status_code, str(exc.detail), _envelope_for(request))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # A body that is not the closed shape (wrong schema_version included) is
        # the contract's `Invalid` outcome: 400, never a partial parse.
        return _error_envelope(400, "invalid", _envelope_for(request))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kill_switch": service.kill_switch,
        }

    @app.post("/enroll")
    def enroll(
        body: EnrollRequest,
        request: Request,
        _: None = Depends(transport_body_guard),
    ) -> dict[str, Any]:
        if not service.rate_limiter.allow(_ENROLL_BUCKET, time.time()):
            raise _http(429, "rate_limited")
        service.check_enrollment_secret(request)
        if len(set(body.capabilities)) != len(body.capabilities):
            raise _http(400, "invalid")
        unknown = [c for c in body.capabilities if c not in WORKER_CAPABILITIES]
        if unknown:
            raise _http(403, "unknown_capability")
        _upsert_enrollment(engine, body.worker_id, body.machine_id, body.capabilities)
        expires_at = int(time.time()) + token_ttl_seconds
        token = issue_token(
            worker_id=body.worker_id,
            capabilities=body.capabilities,
            expires_at_epoch=expires_at,
            key=service_key,
        )
        _append_redacted_audit(engine, event_type="worker.enroll", outcome="accepted")
        return {
            "schema_version": SCHEMA_VERSION,
            "worker_id": body.worker_id,
            "capabilities": body.capabilities,
            "token": token,
            "token_expires_at": expires_at,
        }

    @app.post("/jobs/claim")
    def claim(
        body: ClaimRequest, identity: tuple[str, list[str]] = Depends(service.auth)
    ) -> dict[str, Any]:
        worker_id, _ = identity
        if body.worker_id != worker_id:
            raise _http(403, "worker_mismatch")
        if service.kill_switch:
            raise _http(503, "kill_switch_active")
        # Lease reclamation is ECS authority and this is its only trigger: a remote
        # worker has no reclaim endpoint and must never expire another worker's
        # lease. Reclaiming before the CAS claim is what lets a job whose worker
        # was killed become claimable again without a shared SQLite file.
        queue.reclaim_expired(engine, max_attempts=service.max_attempts)
        job_id = queue.claim_job(
            engine, worker_id=worker_id, lease_ttl_seconds=service.lease_ttl_seconds
        )
        if job_id is None:
            return Response(status_code=204)
        record = queue.get_job(engine, job_id=job_id)
        if record is None or record.lease_expires_at is None:
            raise _http(500, "lease_missing")
        response: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "job_id": record.job_id,
            "feature_id": record.feature_id,
            "repository_id": record.repository_id,
            "base_sha": record.base_sha,
            "branch_name": record.branch_name,
            "toolchain_ref": record.toolchain_ref,
            "lease_epoch": record.lease_epoch,
            "attempt": record.attempt_count,
            "deadline": record.lease_expires_at.isoformat(),
        }
        # F7 round-2 finding 6: the intake body and its digest ride the claim
        # response so a remote worker's coder prompt can substitute
        # {task_description} exactly as the local transport does. The keys are
        # omitted (not null) for a job enqueued without an intake — the
        # operator/test seeding path — keeping the closed shape honest.
        if record.task_description is not None:
            response["task_description"] = record.task_description
            response["task_description_sha256"] = record.task_description_sha256
        return response

    @app.post("/jobs/{job_id}/heartbeat")
    def heartbeat(
        job_id: str,
        body: HeartbeatRequest,
        identity: tuple[str, list[str]] = Depends(service.auth),
    ) -> dict[str, Any]:
        worker_id, _ = identity
        _check_worker(body, worker_id, job_id)
        ok = queue.heartbeat(
            engine,
            job_id=job_id,
            worker_id=worker_id,
            lease_epoch=body.lease_epoch,
            lease_ttl_seconds=service.lease_ttl_seconds,
        )
        if ok:
            return {"schema_version": SCHEMA_VERSION, "cancel_requested": False}
        record = queue.get_job(engine, job_id=job_id)
        if record is not None and record.state == "cancelled":
            return {"schema_version": SCHEMA_VERSION, "cancel_requested": True}
        raise _http(409, "stale")

    @app.post("/jobs/{job_id}/checkpoint")
    def checkpoint(
        job_id: str,
        body: CheckpointRequest,
        identity: tuple[str, list[str]] = Depends(service.auth),
    ) -> dict[str, Any]:
        worker_id, _ = identity
        _check_worker(body, worker_id, job_id)
        if body.artifact_size_bytes > ARTIFACT_MAX_BYTES:
            raise _http(413, "oversized")
        if len(body.changed_files) > CHANGED_FILES_MAX:
            raise _http(413, "oversized")
        try:
            _record_checkpoint_fenced(
                engine,
                job_id=job_id,
                worker_id=worker_id,
                lease_epoch=body.lease_epoch,
                sequence=body.sequence,
                artifact_sha256=body.artifact_sha256,
                artifact_size_bytes=body.artifact_size_bytes,
                changed_files=body.changed_files,
                sensitivity=body.sensitivity,
            )
        except StaleLeaseError as exc:
            raise _http(409, "stale") from exc
        except CheckpointConflictError as exc:
            raise _http(409, "conflict") from exc
        return {"schema_version": SCHEMA_VERSION, "recorded": True}

    @app.post("/jobs/{job_id}/result")
    def result(
        job_id: str,
        body: ResultRequest,
        identity: tuple[str, list[str]] = Depends(service.auth),
    ) -> dict[str, Any]:
        worker_id, _ = identity
        _check_worker(body, worker_id, job_id)
        # Idempotent replay is classified before the write: a job already terminal
        # with the identical result returns the original receipt without a second
        # transition. `finish_job` remains the atomic authority for the fresh path.
        record = queue.get_job(engine, job_id=job_id)
        if record is None:
            raise _http(409, "stale")
        if (
            record.state == body.state
            and record.state in ("succeeded", "failed")
            and record.result_sha256 == body.result_sha256
        ):
            return {
                "schema_version": SCHEMA_VERSION,
                "job_id": job_id,
                "result_sha256": body.result_sha256,
                "receipt_id": _receipt_id(engine, job_id),
                "replay": True,
            }
        try:
            accepted = queue.finish_job(
                engine,
                job_id=job_id,
                worker_id=worker_id,
                lease_epoch=body.lease_epoch,
                state=body.state,
                result_sha256=body.result_sha256,
                last_error=body.last_error,
            )
        except queue.ResultConflictError as exc:
            raise _http(409, "conflict") from exc
        if accepted:
            _append_redacted_audit(engine, event_type="worker.result", outcome=body.state)
            return {
                "schema_version": SCHEMA_VERSION,
                "job_id": job_id,
                "result_sha256": body.result_sha256,
                "receipt_id": _receipt_id(engine, job_id),
                "replay": False,
            }
        record = queue.get_job(engine, job_id=job_id)
        if record is not None and record.state == "cancelled":
            raise _http(409, "cancelled")
        raise _http(409, "stale")

    # --- operator plane (DAL-R08 first slice) ---------------------------------
    #
    # Same body-digest fence and rate limiter as the worker plane; a separate
    # token schema and capability check. Read endpoints are GET (no body, so
    # the digest fence does not apply); the single mutation re-verifies the
    # job's live state inside one CAS write.

    operator_read = _OperatorAuth(service, "read")
    operator_control = _OperatorAuth(service, "control")

    @app.get("/operator/jobs")
    def operator_jobs(
        limit: int = 20,
        offset: int = 0,
        _: str = Depends(operator_read),
    ) -> dict[str, Any]:
        if (
            limit < 1
            or limit > OPERATOR_JOBS_PAGE_MAX
            or offset < 0
            or offset > OPERATOR_JOBS_OFFSET_MAX
        ):
            raise _http(400, "invalid")
        total, rows = _operator_identity_rows(engine, limit=limit, offset=offset)
        return {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "total": total,
            "offset": offset,
            "limit": limit,
            "jobs": rows,
        }

    @app.get("/operator/jobs/{job_id}")
    def operator_job_detail(
        job_id: str, _: str = Depends(operator_read)
    ) -> dict[str, Any]:
        record = queue.get_job(engine, job_id=job_id)
        if record is None:
            raise _http(404, "job_not_found")
        return {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "job_id": record.job_id,
            "feature_id": record.feature_id,
            "repository_id": record.repository_id,
            "base_sha": record.base_sha,
            "branch_name": record.branch_name,
            "state": record.state,
            "attempt": record.attempt_count,
            "lease_epoch": record.lease_epoch,
            "worker_id": record.worker_id,
            "lease_expires_at": (
                record.lease_expires_at.isoformat() if record.lease_expires_at else None
            ),
            "result_sha256": record.result_sha256,
            "last_error": record.last_error,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    @app.get("/operator/jobs/{job_id}/checkpoints")
    def operator_job_checkpoints(
        job_id: str, _: str = Depends(operator_read)
    ) -> dict[str, Any]:
        if queue.get_job(engine, job_id=job_id) is None:
            raise _http(404, "job_not_found")
        sessions = session_factory(engine)
        with sessions() as session:
            rows = session.execute(
                select(
                    WorkerCheckpoint.sequence,
                    WorkerCheckpoint.lease_epoch,
                    WorkerCheckpoint.artifact_sha256,
                    WorkerCheckpoint.artifact_size_bytes,
                    WorkerCheckpoint.changed_files,
                    WorkerCheckpoint.sensitivity,
                    WorkerCheckpoint.recorded_at,
                )
                .where(WorkerCheckpoint.job_id == job_id)
                .order_by(WorkerCheckpoint.sequence.asc())
            ).all()
        return {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "job_id": job_id,
            "checkpoints": [
                {
                    "sequence": r.sequence,
                    "lease_epoch": r.lease_epoch,
                    "artifact_sha256": r.artifact_sha256,
                    "artifact_size_bytes": r.artifact_size_bytes,
                    "changed_files": json.loads(r.changed_files),
                    "sensitivity": r.sensitivity,
                    "recorded_at": r.recorded_at.isoformat(),
                }
                for r in rows
            ],
        }

    @app.post("/operator/jobs/{job_id}/{action}")
    def operator_action(
        job_id: str,
        action: str,
        body: OperatorActionRequest,
        operator_id: str = Depends(operator_control),
        _: None = Depends(transport_body_guard),
    ) -> dict[str, Any]:
        if action in OPERATOR_DEFERRED_ACTIONS:
            # pause/resume/request-human/accept-result await the controller
            # dispatch executor; the contract declares them 501, not silent.
            raise _http(501, "operator_action_not_available")
        if action not in OPERATOR_MUTATIONS:
            # A closed vocabulary: unknown verbs are a contract violation
            # (404), not "implemented someday" (501).
            raise _http(404, "unknown_action")
        if body.job_id != job_id:
            raise _http(400, "job_mismatch")
        if service.kill_switch:
            raise _http(503, "kill_switch_active")
        record = queue.get_job(engine, job_id=job_id)
        if record is None:
            raise _http(404, "job_not_found")
        if record.state != body.expected_state:
            # Stale operator projection: refuse before any write.
            raise _http(409, "state_mismatch")
        ok = queue.cancel_job(engine, job_id=job_id, expected_state=body.expected_state)
        if not ok:
            # Lost a race between the pre-read and the CAS write: the fenced
            # CAS returns False for any post-pre-read state change, including
            # an active→active transition or another authority's cancel.
            raise _http(409, "state_mismatch")
        _append_redacted_audit(
            engine,
            event_type="operator.action",
            outcome=f"{operator_id}:cancel",
        )
        return {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "job_id": job_id,
            "action": action,
            "state": "cancelled",
        }

    @app.post("/operator/provider-attempts/{attempt_id}/recover")
    def operator_recover_provider_attempt(
        attempt_id: str,
        body: ProviderRecoveryRequest,
        operator_id: str = Depends(operator_control),
        _: None = Depends(transport_body_guard),
    ) -> dict[str, Any]:
        if body.attempt_id != attempt_id:
            raise _http(400, "attempt_mismatch")
        if service.kill_switch:
            raise _http(503, "kill_switch_active")
        # No provider probe is composed yet. The recovery boundary explicitly
        # records that absence and only parks abandoned dispatches as unknown.
        # Neither the request nor this route has a redispatch/success knob.
        outcome = recover_attempt(
            engine, attempt_id=attempt_id, expected_version=body.expected_version,
            command_id=body.request_id, requested_by=operator_id,
        )
        if outcome.code == "ATTEMPT_NOT_FOUND":
            raise _http(404, outcome.code)
        if outcome.code not in ("ATTEMPT_UNKNOWN", "RECOVERY_NOT_NEEDED"):
            raise _http(409, outcome.code)
        _append_redacted_audit(
            engine, event_type="operator.provider_recover",
            outcome=f"{operator_id}:{outcome.code}",
        )
        return {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "code": outcome.code,
            "receipt_id": outcome.receipt_id,
            "duplicate": outcome.duplicate,
            "probe_status": outcome.probe_status,
            "probe_code": outcome.probe_code,
        }

    # --- effect wake + reconciliation sweep (R09-B F5) ------------------------
    #
    # The durable GitHub dispatch executor lives inside this service: the
    # operator's wake request IS the state/version binding (the closed
    # ``EffectWakeRequest`` has no field that could carry a target), and
    # everything the write needs is derived from persistence inside
    # ``executor.wake_effect``. When no GitHub adapter is composed, the
    # endpoints are declared 501, never silently absent — mirroring the
    # deferred-actions contract.

    @app.get("/operator/effects")
    def operator_unknown_effects(
        limit: int = 20,
        _: str = Depends(operator_read),
    ) -> dict[str, Any]:
        if github_adapter is None:
            raise _http(501, "executor_not_composed")
        if limit < 1 or limit > OPERATOR_JOBS_PAGE_MAX:
            raise _http(400, "invalid")
        return {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "effects": executor.unknown_effects(engine, limit=limit),
            # F2 (2026-09-07 review): claims in flight are operator-visible
            # too — a crashed reconciler used to be invisible to this
            # listing, recoverable only by a manual wake with a known id.
            # Additive key: existing consumers reading `effects` are
            # unaffected.
            "reconciling": executor.reconciling_effects(engine, limit=limit),
        }

    @app.post("/operator/effects/{effect_id}/wake")
    def operator_wake_effect(
        effect_id: str,
        body: EffectWakeRequest,
        operator_id: str = Depends(operator_control),
        _: None = Depends(transport_body_guard),
    ) -> dict[str, Any]:
        if github_adapter is None:
            raise _http(501, "executor_not_composed")
        if body.effect_id != effect_id:
            raise _http(400, "effect_mismatch")
        if service.kill_switch:
            raise _http(503, "kill_switch_active")
        try:
            outcome = executor.wake_effect(
                engine,
                github_adapter,
                effect_id=effect_id,
                expected_state=body.expected_state,
                expected_version=body.expected_version,
            )
        except executor.ExecutorRefusal as error:
            status, code = _executor_refusal_status(error.code)
            raise _http(status, code) from error
        _append_redacted_audit(
            engine,
            event_type="operator.effect_wake",
            outcome=f"{operator_id}:{body.expected_state}->{outcome.effect_state}",
        )
        response: dict[str, Any] = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "effect_id": effect_id,
            "effect_state": outcome.effect_state,
            "refusal": (
                {"code": outcome.refusal.code, "detail": outcome.refusal.detail}
                if outcome.refusal is not None
                else None
            ),
        }
        if outcome.reconciled is not None:
            response["authoritative_result"] = outcome.reconciled.authoritative_result
        return response

    @app.post("/operator/intake")
    def operator_intake(
        body: IntakeRequest,
        operator_id: str = Depends(operator_control),
        _: None = Depends(transport_body_guard),
    ) -> dict[str, Any]:
        """Create a Feature at intake and enqueue its pending Job.

        The source-agnostic producer (Phase A entry: a human via the operator
        console). The operator names the repo, the base SHA and the toolchain
        manifest; the server content-addresses the feature id and derives the
        branch and idempotency key, so nothing the operator must not control is
        accepted. Idempotent on the task identity — a re-run is a replay.
        """
        if service.kill_switch:
            raise _http(503, "kill_switch_active")
        try:
            outcome = intake_task(
                engine,
                task_description=body.task_description,
                repository_id=body.repository_id,
                base_sha=body.base_sha,
                toolchain_ref=body.toolchain_ref,
            )
        except IntakeRefusal as error:
            raise _http(_intake_refusal_status(error.code), error.code) from error
        _append_redacted_audit(
            engine,
            event_type="operator.intake",
            outcome=f"{operator_id}:{outcome.feature_state}:{outcome.job_id}",
        )
        return {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "feature_id": outcome.feature_id,
            "job_id": outcome.job_id,
            "feature_state": outcome.feature_state,
            "duplicate": outcome.duplicate,
        }

    @app.post("/operator/effects/reconcile-sweep")
    def operator_reconcile_sweep(
        operator_id: str = Depends(operator_control),
        _: None = Depends(transport_body_guard),
    ) -> dict[str, Any]:
        """One persistence-driven reconciliation pass over unknown effects.

        Read-only over the wire (authoritative GET read-backs only); the
        persistent-task driver (systemd timer calling the operator CLI)
        invokes this on its schedule — the sweep itself never re-fires a
        parked write.
        """
        if github_adapter is None:
            raise _http(501, "executor_not_composed")
        if service.kill_switch:
            raise _http(503, "kill_switch_active")
        outcomes = executor.run_unknown_sweep(engine, github_adapter)
        _append_redacted_audit(
            engine,
            event_type="operator.reconcile_sweep",
            outcome=f"{operator_id}:{len(outcomes)}",
        )
        return {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "swept": [
                {
                    "effect_id": o.effect_id,
                    "effect_state": o.effect_state,
                    "authoritative_result": (
                        o.reconciled.authoritative_result
                        if o.reconciled is not None
                        else None
                    ),
                    "refusal_code": o.refusal.code if o.refusal is not None else None,
                }
                for o in outcomes
            ],
        }

    return app


def _intake_refusal_status(code: str) -> tuple[int, str]:
    """An intake refusal's HTTP face, closed by code.

    A malformed request is the caller's error (400); a state the intake reached
    that the spec forbids (unexpected_state / no_job) is a server-invariant
    violation surfaced as 409 so it is never mistaken for a valid refusal; the
    engine's own closed receipt codes (POLICY_DENIED, VERSION_CONFLICT,
    ILLEGAL_TRANSITION, IDEMPOTENCY_CONFLICT) pass through as 409.
    """
    mapping: dict[str, tuple[int, str]] = {
        "invalid": (400, "invalid"),
        "no_job": (409, "no_job"),
        "unexpected_state": (409, "unexpected_intake_state"),
    }
    return mapping.get(code, (409, code))


def _executor_refusal_status(code: str) -> tuple[int, str]:
    """An executor refusal's HTTP face, closed by code.

    Binding mismatches are the caller's stale projection (409); a missing
    effect or target record is 404; an illegal wake state is 409; the rest
    are the executor's own closed vocabulary and surface as 409 — never 500,
    which would claim a service fault the rows deny.
    """
    mapping: dict[str, tuple[int, str]] = {
        "STATE_MISMATCH": (409, "state_mismatch"),
        "VERSION_MISMATCH": (409, "version_mismatch"),
        "NOT_FOUND": (404, "effect_not_found"),
        "TARGET_MISSING": (404, "target_missing"),
        "ILLEGAL_STATE": (409, "illegal_state"),
        "FINGERPRINT_MISMATCH": (409, "fingerprint_mismatch"),
        "INVALID_ARGUMENT": (400, "invalid"),
    }
    return mapping.get(code, (409, "executor_refused"))


def _receipt_id(engine: Engine, job_id: str) -> str:
    sessions = session_factory(engine)
    with sessions() as session:
        row = session.execute(
            select(queue.WorkerResultReceipt.receipt_id).where(
                queue.WorkerResultReceipt.job_id == job_id
            )
        ).scalar_one_or_none()
        return row or ""
