"""DAL-R05 Dev Workflow Service — the thin-http transport composition root.

This is the server half of the frozen `worker-transport-v1` contract. It wraps
the queue/lease primitives in `personal_agent_dal.worker.queue` (CAS claim,
exact-epoch heartbeat, idempotent result with digest-conflict detection) behind
FastAPI endpoints, and adds the transport-specific bounds: enrollment + HMAC
token auth, kill switch, bounded request body, per-worker rate limit, and a
redacted audit append.

It owns no business state machine: `worker_jobs` is the authority for "which
worker runs which job now"; feature/run authority stays with the business layer
this transport serves. Result acceptance happens inside `queue.finish_job`, whose
receipt insert and terminal transition share one server-side transaction — the
exact-epoch + digest-conflict fencing is therefore atomic with the write.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
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
    SENSITIVITY_CLASSES,
    WORKER_CAPABILITIES,
    WorkerCheckpoint,
    WorkerEnrollment,
)
from personal_agent_dal.worker import queue

SCHEMA_VERSION = "dal.worker-transport/1.0"
MAX_BODY_BYTES = 1_048_576  # 1 MiB transport envelope cap, distinct from artifact cap
ARTIFACT_MAX_BYTES = 104_857_600
CHANGED_FILES_MAX = 10_000
TOKEN_TTL_SECONDS = 3600
RATE_LIMIT_MAX_REQUESTS = 120
RATE_LIMIT_WINDOW_SECONDS = 60.0


# --- closed request shapes (extra="forbid") ---------------------------------


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrollRequest(_Closed):
    schema_version: str = Field(default=SCHEMA_VERSION)
    request_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    machine_id: str = Field(min_length=1)
    capabilities: list[str] = Field(min_length=1)


class ClaimRequest(_Closed):
    schema_version: str = Field(default=SCHEMA_VERSION)
    request_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)


class HeartbeatRequest(_Closed):
    schema_version: str = Field(default=SCHEMA_VERSION)
    request_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=0)


class CheckpointRequest(_Closed):
    schema_version: str = Field(default=SCHEMA_VERSION)
    request_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=0)
    sequence: int = Field(ge=0)
    artifact_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    artifact_size_bytes: int = Field(ge=1)
    changed_files: list[str] = Field(default_factory=list)
    sensitivity: str


class ResultRequest(_Closed):
    schema_version: str = Field(default=SCHEMA_VERSION)
    request_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=0)
    result_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    state: str = Field(pattern="^(succeeded|failed)$")
    last_error: str | None = None


# --- store helpers ----------------------------------------------------------


def _enrollment(engine: Engine, worker_id: str) -> WorkerEnrollment | None:
    sessions = session_factory(engine)
    with sessions() as session:
        return session.get(WorkerEnrollment, worker_id)


def _upsert_enrollment(
    engine: Engine, worker_id: str, capabilities: list[str]
) -> None:
    sessions = session_factory(engine)

    def _body(session: Any) -> None:
        row = session.get(WorkerEnrollment, worker_id)
        caps = json.dumps(sorted(capabilities))
        if row is None:
            session.add(
                WorkerEnrollment(
                    worker_id=worker_id,
                    capabilities=caps,
                    created_at=utc_now(),
                )
            )
        else:
            if row.revoked_at is not None:
                raise _http(403, "worker revoked")
            row.capabilities = caps

    with sessions() as session:
        run_write_transaction(session, lambda: _body(session))


def _record_checkpoint(
    engine: Engine,
    *,
    job_id: str,
    lease_epoch: int,
    sequence: int,
    artifact_sha256: str,
    artifact_size_bytes: int,
    changed_files: list[str],
    sensitivity: str,
) -> str:
    """Insert one checkpoint; idempotent on (job_id, sequence)."""
    sessions = session_factory(engine)

    def _body(session: Any) -> str:
        existing = session.execute(
            select(WorkerCheckpoint.checkpoint_id).where(
                WorkerCheckpoint.job_id == job_id,
                WorkerCheckpoint.sequence == sequence,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
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


def _http(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


# --- transport auth ---------------------------------------------------------


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
        kill_switch_path: Path | None,
        rate_limiter: RateLimiter,
    ) -> None:
        self.engine = engine
        self.service_key = service_key
        self.kill_switch_path = kill_switch_path
        self.rate_limiter = rate_limiter

    @property
    def kill_switch(self) -> bool:
        return self.kill_switch_path is not None and self.kill_switch_path.exists()

    def auth(self, request: Request) -> tuple[str, list[str]]:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise _http(401, "missing bearer token")
        token = header[len("Bearer "):].strip()
        try:
            worker_id, capabilities = verify_token(
                token, key=self.service_key, now_epoch=int(time.time())
            )
        except TokenError as exc:
            raise _http(401, str(exc)) from exc
        enrollment = _enrollment(self.engine, worker_id)
        if enrollment is None:
            raise _http(403, "unknown worker")
        if enrollment.revoked_at is not None:
            raise _http(403, "worker revoked")
        if not self.rate_limiter.allow(worker_id, time.time()):
            raise _http(429, "rate limited")
        return worker_id, capabilities


def _check_worker(body: BaseModel, token_worker_id: str, path_job_id: str) -> None:
    if getattr(body, "worker_id", None) != token_worker_id:
        raise _http(403, "worker_id mismatch")
    if getattr(body, "job_id", None) != path_job_id:
        raise _http(400, "job_id mismatch")


# --- app factory ------------------------------------------------------------


def create_app(
    engine: Engine,
    *,
    service_key: bytes,
    kill_switch_path: Path | None = None,
    token_ttl_seconds: int = TOKEN_TTL_SECONDS,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    service = Service(
        engine,
        service_key=service_key,
        kill_switch_path=kill_switch_path,
        rate_limiter=rate_limiter or RateLimiter(RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS),
    )
    app = FastAPI(title="DAL Worker Transport", version="1.0.0")

    @app.middleware("http")
    async def _bound_body(request: Request, call_next: Callable):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > MAX_BODY_BYTES:
                    return JSONResponse({"detail": "body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "bad content-length"}, status_code=400)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kill_switch": service.kill_switch,
            "revoke_worker": False,
        }

    @app.post("/enroll")
    def enroll(body: EnrollRequest) -> dict[str, Any]:
        if body.schema_version != SCHEMA_VERSION:
            raise _http(400, "wrong schema_version")
        unknown = [c for c in body.capabilities if c not in WORKER_CAPABILITIES]
        if unknown:
            raise _http(403, "unknown_capability")
        _upsert_enrollment(engine, body.worker_id, body.capabilities)
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
            raise _http(403, "worker_id mismatch")
        if service.kill_switch:
            raise _http(503, "kill_switch_active")
        job_id = queue.claim_job(engine, worker_id=worker_id, lease_ttl_seconds=60)
        if job_id is None:
            return JSONResponse(status_code=204)
        record = queue.get_job(engine, job_id=job_id)
        if record is None:
            raise _http(500, "job row missing")
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": record.job_id,
            "repository_id": record.repository_id,
            "base_sha": record.base_sha,
            "branch_name": record.branch_name,
            "toolchain_ref": record.toolchain_ref,
            "lease_epoch": record.lease_epoch,
            "attempt": record.attempt_count,
            "deadline": record.lease_expires_at.isoformat() if record.lease_expires_at else "",
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
            lease_ttl_seconds=60,
        )
        if ok:
            return {"schema_version": SCHEMA_VERSION, "cancel_requested": False, "revoked": False}
        record = queue.get_job(engine, job_id=job_id)
        if record is not None and record.state == "cancelled":
            return {"schema_version": SCHEMA_VERSION, "cancel_requested": True, "revoked": False}
        raise _http(409, "stale")

    @app.post("/jobs/{job_id}/checkpoint")
    def checkpoint(
        job_id: str,
        body: CheckpointRequest,
        identity: tuple[str, list[str]] = Depends(service.auth),
    ) -> dict[str, Any]:
        worker_id, _ = identity
        _check_worker(body, worker_id, job_id)
        if body.sensitivity not in SENSITIVITY_CLASSES:
            raise _http(400, "invalid")
        if body.artifact_size_bytes > ARTIFACT_MAX_BYTES:
            raise _http(413, "oversized")
        if len(body.changed_files) > CHANGED_FILES_MAX:
            raise _http(413, "oversized")
        record = queue.get_job(engine, job_id=job_id)
        if record is None or record.lease_epoch != body.lease_epoch or record.worker_id != worker_id:
            raise _http(409, "stale")
        _record_checkpoint(
            engine,
            job_id=job_id,
            lease_epoch=body.lease_epoch,
            sequence=body.sequence,
            artifact_sha256=body.artifact_sha256,
            artifact_size_bytes=body.artifact_size_bytes,
            changed_files=body.changed_files,
            sensitivity=body.sensitivity,
        )
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
