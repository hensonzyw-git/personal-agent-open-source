"""Durable job-queue primitives (DAL-016 runnable layer).

Every primitive is one unit of database work with no external side effects, so
each is wrapped in `run_write_transaction`. Claims and state changes are
compare-and-swap: `claim_job` moves only a `pending` row to `leased` and returns
the winner; `heartbeat` and `finish_job` touch only the job this worker owns;
`reclaim_expired` moves only rows whose lease has actually expired. A result
receipt is idempotent on its unique `job_id`, so a replayed completion returns
the original receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.orm import Session

from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.worker_models import WorkerJob, WorkerResultReceipt


RECEIPT_SCHEMA: Final[str] = "dal.worker-result-receipt/1.0"

#: The states a live lease may occupy. `pending` has no lease; terminal states
#: are never heartbeated or reclaimed.
_ACTIVE_STATES: Final[tuple[str, str]] = ("leased", "running")


def _jobs_table():
    return WorkerJob.__table__


def _receipts_table():
    return WorkerResultReceipt.__table__


@dataclass(frozen=True)
class JobRecord:
    """A read-only view of one `worker_jobs` row."""

    job_id: str
    feature_id: str
    repository_id: str
    base_sha: str
    branch_name: str
    toolchain_ref: str
    state: str
    attempt_count: int
    lease_epoch: int
    worker_id: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    result_sha256: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def enqueue_job(
    engine: Engine,
    *,
    feature_id: str,
    repository_id: str,
    base_sha: str,
    branch_name: str,
    toolchain_ref: str,
    now: datetime | None = None,
) -> str:
    """Insert a new `pending` job; returns its `job_id`.

    This is the queue's producer half. The DAL service (or an operator seeding a
    synthetic acceptance run) enqueues; the worker claims and completes.
    """
    now = now or utc_now()
    sessions = session_factory(engine)

    def _body(session: Session) -> str:
        job_id = new_id()
        session.execute(
            insert(_jobs_table()).values(
                job_id=job_id,
                feature_id=feature_id,
                repository_id=repository_id,
                base_sha=base_sha,
                branch_name=branch_name,
                toolchain_ref=toolchain_ref,
                state="pending",
                attempt_count=0,
                lease_epoch=0,
                created_at=now,
                updated_at=now,
            )
        )
        return job_id

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


def get_job(engine: Engine, *, job_id: str) -> JobRecord | None:
    """Read one job row by id (fresh connection; no identity-map cache)."""
    sessions = session_factory(engine)
    with sessions() as session:
        row = session.execute(
            select(WorkerJob).where(WorkerJob.job_id == job_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return JobRecord(
            job_id=row.job_id,
            feature_id=row.feature_id,
            repository_id=row.repository_id,
            base_sha=row.base_sha,
            branch_name=row.branch_name,
            toolchain_ref=row.toolchain_ref,
            state=row.state,
            attempt_count=row.attempt_count,
            lease_epoch=row.lease_epoch,
            worker_id=row.worker_id,
            lease_expires_at=row.lease_expires_at,
            heartbeat_at=row.heartbeat_at,
            result_sha256=row.result_sha256,
            last_error=row.last_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def claim_job(
    engine: Engine,
    *,
    worker_id: str,
    lease_ttl_seconds: int,
    now: datetime | None = None,
) -> str | None:
    """CAS-claim the oldest pending job; return its `job_id`, or None if none.

    The compare-and-swap is the ``WHERE state='pending'``: two workers racing
    for the same row produce exactly one ``rowcount==1`` winner. The persistent
    ``lease_epoch`` increments on every successful claim.
    """
    now = now or utc_now()
    expires_at = now + timedelta(seconds=lease_ttl_seconds)
    sessions = session_factory(engine)

    def _body(session: Session) -> str | None:
        table = _jobs_table()
        candidate = session.execute(
            select(table.c.job_id)
            .where(table.c.state == "pending")
            .order_by(table.c.created_at)
            .limit(1)
        ).scalar_one_or_none()
        if candidate is None:
            return None
        result = session.execute(
            update(table)
            .where(table.c.job_id == candidate)
            .where(table.c.state == "pending")
            .values(
                state="leased",
                lease_epoch=table.c.lease_epoch + 1,
                worker_id=worker_id,
                lease_expires_at=expires_at,
                heartbeat_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        return candidate

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


def mark_running(
    engine: Engine,
    *,
    job_id: str,
    worker_id: str,
    lease_epoch: int,
    now: datetime | None = None,
) -> bool:
    """Move a job this worker owns from `leased` to `running`."""
    now = now or utc_now()
    sessions = session_factory(engine)

    def _body(session: Session) -> bool:
        table = _jobs_table()
        result = session.execute(
            update(table)
            .where(table.c.job_id == job_id)
            .where(table.c.state == "leased")
            .where(table.c.worker_id == worker_id)
            .where(table.c.lease_epoch == lease_epoch)
            .values(state="running", updated_at=now)
        )
        return result.rowcount == 1

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


def heartbeat(
    engine: Engine,
    *,
    job_id: str,
    worker_id: str,
    lease_epoch: int,
    lease_ttl_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Refresh this worker's lease on a job it owns; False when it lost the lease."""
    now = now or utc_now()
    expires_at = now + timedelta(seconds=lease_ttl_seconds)
    sessions = session_factory(engine)

    def _body(session: Session) -> bool:
        table = _jobs_table()
        result = session.execute(
            update(table)
            .where(table.c.job_id == job_id)
            .where(table.c.state.in_(_ACTIVE_STATES))
            .where(table.c.worker_id == worker_id)
            .where(table.c.lease_epoch == lease_epoch)
            .values(heartbeat_at=now, lease_expires_at=expires_at, updated_at=now)
        )
        return result.rowcount == 1

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


def reclaim_expired(
    engine: Engine, *, max_attempts: int, now: datetime | None = None
) -> list[str]:
    """Reclaim every job whose lease has expired; return their `job_id`s.

    A stale job goes back to `pending` (retryable) until its attempt budget is
    exhausted, after which it is `expired` (terminal). Releasing a lease clears
    only its owner/timestamps; ``lease_epoch`` remains monotonic as the fencing
    token for every future attempt.
    """
    now = now or utc_now()
    sessions = session_factory(engine)

    def _body(session: Session) -> list[str]:
        table = _jobs_table()
        stale = session.execute(
            select(table.c.job_id, table.c.attempt_count)
            .where(table.c.state.in_(_ACTIVE_STATES))
            .where(table.c.lease_expires_at < now)
        ).all()
        if not stale:
            return []
        requeue_ids = [
            job_id for job_id, attempts in stale if attempts + 1 < max_attempts
        ]
        expire_ids = [
            job_id for job_id, attempts in stale if attempts + 1 >= max_attempts
        ]
        clear_values = {
            "worker_id": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "updated_at": now,
        }
        if requeue_ids:
            session.execute(
                update(table)
                .where(table.c.job_id.in_(requeue_ids))
                .values(
                    state="pending",
                    attempt_count=table.c.attempt_count + 1,
                    **clear_values,
                )
            )
        if expire_ids:
            session.execute(
                update(table)
                .where(table.c.job_id.in_(expire_ids))
                .values(
                    state="expired",
                    attempt_count=table.c.attempt_count + 1,
                    **clear_values,
                )
            )
        return [job_id for job_id, _ in stale]

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


def finish_job(
    engine: Engine,
    *,
    job_id: str,
    worker_id: str,
    lease_epoch: int,
    state: str,
    result_sha256: str | None = None,
    last_error: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Atomically record a result and terminal state for this fenced lease.

    A retry after a committed response loss is idempotent only when the terminal
    state and result digest are identical. A different digest for the same job
    is an integrity conflict, never an idempotent replay. The receipt insert and
    terminal transition share one transaction, so neither can exist alone.
    """
    if state not in ("succeeded", "failed", "expired", "cancelled"):
        raise ValueError(f"not a terminal state: {state!r}")
    now = now or utc_now()
    sessions = session_factory(engine)

    def _body(session: Session) -> bool:
        table = _jobs_table()
        receipts = _receipts_table()
        row = session.execute(
            select(
                table.c.state,
                table.c.worker_id,
                table.c.lease_epoch,
                table.c.result_sha256,
                table.c.last_error,
            ).where(table.c.job_id == job_id)
        ).one_or_none()
        if row is None:
            return False

        existing_result = session.execute(
            select(receipts.c.result_sha256).where(receipts.c.job_id == job_id)
        ).scalar_one_or_none()
        if existing_result is not None and existing_result != result_sha256:
            raise ResultConflictError(job_id)

        if row.state not in _ACTIVE_STATES:
            return (
                row.state == state
                and row.result_sha256 == result_sha256
                and row.last_error == last_error
                and (result_sha256 is None or existing_result == result_sha256)
            )
        if row.worker_id != worker_id or row.lease_epoch != lease_epoch:
            return False

        if result_sha256 is not None and existing_result is None:
            session.execute(
                insert(receipts).values(
                    receipt_id=new_id(),
                    job_id=job_id,
                    result_sha256=result_sha256,
                    receipt_schema_version=RECEIPT_SCHEMA,
                    recorded_at=now,
                )
            )
        result = session.execute(
            update(table)
            .where(table.c.job_id == job_id)
            .where(table.c.state.in_(_ACTIVE_STATES))
            .where(table.c.worker_id == worker_id)
            .where(table.c.lease_epoch == lease_epoch)
            .values(
                state=state,
                worker_id=None,
                lease_expires_at=None,
                heartbeat_at=None,
                result_sha256=result_sha256,
                last_error=last_error,
                updated_at=now,
            )
        )
        return result.rowcount == 1

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


def cancel_job(
    engine: Engine,
    *,
    job_id: str,
    now: datetime | None = None,
) -> bool:
    """Authority action: cancel a live job, terminal and non-retryable.

    Unlike `finish_job`, this is not worker-fenced — it is the ECS lease owner's
    authority to stop a job regardless of which worker holds it. It moves only
    an active row to `cancelled` (clearing the lease) and is idempotent for a
    job already cancelled; any other terminal state is left untouched (False).
    A cancelled job carries no result receipt.
    """
    now = now or utc_now()
    sessions = session_factory(engine)

    def _body(session: Session) -> bool:
        table = _jobs_table()
        result = session.execute(
            update(table)
            .where(table.c.job_id == job_id)
            .where(table.c.state.in_(_ACTIVE_STATES))
            .values(
                state="cancelled",
                worker_id=None,
                lease_expires_at=None,
                heartbeat_at=None,
                updated_at=now,
            )
        )
        if result.rowcount == 1:
            return True
        row = session.execute(
            select(table.c.state).where(table.c.job_id == job_id)
        ).scalar_one_or_none()
        return row == "cancelled"

    with sessions() as session:
        return run_write_transaction(session, lambda: _body(session))


class ResultConflictError(RuntimeError):
    """The same job id was replayed with a different result digest."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"conflicting result for worker job {job_id}")
