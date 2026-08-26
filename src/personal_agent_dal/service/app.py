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
from sqlalchemy import Engine, select

from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.service.tokens import TokenError, issue_token, verify_token
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.transport_models import (
    WORKER_CAPABILITIES,
    WorkerCheckpoint,
    WorkerEnrollment,
)
from personal_agent_dal.worker import queue
from personal_agent_dal.worker.queue import ACTIVE_JOB_STATES

SCHEMA_VERSION = "dal.worker-transport/1.0"
MAX_BODY_BYTES = 1_048_576  # 1 MiB transport envelope cap, distinct from artifact cap
ARTIFACT_MAX_BYTES = 104_857_600
CHANGED_FILES_MAX = 10_000
CHANGED_FILE_MAX_LENGTH = 512
ID_MAX_LENGTH = 256
LAST_ERROR_MAX_LENGTH = 4096
TOKEN_TTL_SECONDS = 3600
RATE_LIMIT_MAX_REQUESTS = 120
RATE_LIMIT_WINDOW_SECONDS = 60.0
LEASE_TTL_SECONDS = 60
MAX_ATTEMPTS = 3

BODY_DIGEST_HEADER: Final[str] = "x-transport-body-digest"
ENROLLMENT_SECRET_HEADER: Final[str] = "x-enrollment-secret"
_ENROLL_BUCKET: Final[str] = "__enroll__"  # global rate-limit bucket for /enroll

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


def _error_envelope(status: int, code: str) -> JSONResponse:
    """The contract's closed `Error` shape; `code` carries the fail-closed reason."""
    return JSONResponse(
        status_code=status,
        content={"schema_version": SCHEMA_VERSION, "code": code, "detail": code},
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
    """In-memory per-worker sliding window. Offline-slice stand-in for Nginx."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, worker_id: str, now: float) -> bool:
        hits = self._hits[worker_id]
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
        if not self.rate_limiter.allow(worker_id, time.time()):
            raise _http(429, "rate_limited")
        return worker_id, capabilities


def _check_worker(body: BaseModel, token_worker_id: str, path_job_id: str) -> None:
    if getattr(body, "worker_id", None) != token_worker_id:
        raise _http(403, "worker_mismatch")
    if getattr(body, "job_id", None) != path_job_id:
        raise _http(400, "job_mismatch")


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

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error_envelope(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # A body that is not the closed shape (wrong schema_version included) is
        # the contract's `Invalid` outcome: 400, never a partial parse.
        return _error_envelope(400, "invalid")

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
        return {
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

    return app


def _receipt_id(engine: Engine, job_id: str) -> str:
    sessions = session_factory(engine)
    with sessions() as session:
        row = session.execute(
            select(queue.WorkerResultReceipt.receipt_id).where(
                queue.WorkerResultReceipt.job_id == job_id
            )
        ).scalar_one_or_none()
        return row or ""
