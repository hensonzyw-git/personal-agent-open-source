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

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import Engine, insert, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import ProviderAttempt
from personal_agent_dal.storage.worker_models import (
    FeatureIntakeRequest,
    WorkerJob,
    WorkerResultReceipt,
)


class EnqueueConflict(Exception):
    """The intake key is held by an existing job with a different toolchain.

    F4/F7 (2026-09-07 review): the operator re-submitted the same task under a
    different `toolchain_ref`. The feature identity deliberately does not
    include the toolchain (the feature is the task), so silence here would
    return the old job and express nothing about the change. The caller maps
    this to a typed refusal; the worker-side `toolchain_ref_mismatch` fence
    stays as the second line of defence.
    """

    def __init__(self, *, intake_key: str, existing_toolchain_ref: str) -> None:
        super().__init__(
            f"intake key {intake_key} is held by a job with toolchain_ref "
            f"{existing_toolchain_ref!r}"
        )
        self.intake_key = intake_key
        self.existing_toolchain_ref = existing_toolchain_ref


RECEIPT_SCHEMA: Final[str] = "dal.worker-result-receipt/1.0"

#: The states a live lease may occupy. `pending` has no lease; terminal states
#: are never heartbeated or reclaimed. Public so the transport layer can fence
#: its own writes (checkpoint) against the same set instead of duplicating it.
ACTIVE_JOB_STATES: Final[tuple[str, str]] = ("leased", "running")


def _jobs_table():
    return WorkerJob.__table__


def _receipts_table():
    return WorkerResultReceipt.__table__


@dataclass(frozen=True)
class JobRecord:
    """A read-only view of one `worker_jobs` row.

    ``task_description``/``task_description_sha256`` are the persisted intake
    body and its digest (F7, 2026-09-07 review), resolved through the job's
    ``intake_key``. Both are ``None`` for jobs enqueued without an intake —
    the operator/test seeding path — and that is exactly the boundary the
    worker's prompt substitution and digest fence check for.
    """

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
    task_description: str | None = None
    task_description_sha256: str | None = None


def enqueue_job(
    engine: Engine,
    *,
    feature_id: str,
    repository_id: str,
    base_sha: str,
    branch_name: str,
    toolchain_ref: str,
    now: datetime | None = None,
    intake_key: str | None = None,
    task_description: str | None = None,
) -> str:
    """Insert a new `pending` job; returns its `job_id`.

    This is the queue's producer half. The DAL service (or an operator seeding a
    synthetic acceptance run) enqueues; the worker claims and completes.

    With an ``intake_key`` (F4, 2026-09-07 review) the insert becomes a
    find-or-create arbitrated by the unique partial index on ``intake_key``:
    a job already holding the key is returned instead of duplicated, so two
    concurrent identical intakes converge on one job row. The body
    (``task_description``, F7) must be supplied with the key and is persisted
    into ``feature_intake_requests`` in the same transaction — uniqueness and
    body persistence land atomically. Deleting a job row frees its key, so the
    interrupted-first-run recovery re-enqueue still creates the missing job.
    A re-submission under a different ``toolchain_ref`` raises
    ``EnqueueConflict`` rather than silently returning the old job.
    """
    if (intake_key is None) != (task_description is None):
        raise ValueError(
            "intake_key and task_description must be supplied together"
        )
    now = now or utc_now()
    sessions = session_factory(engine)

    def _record_intake_body(session: Session) -> None:
        # Idempotent on the primary key: the body digest is derived from the
        # feature identity, so a legitimate replay writes identical bytes.
        session.execute(
            sqlite_insert(FeatureIntakeRequest)
            .values(
                intake_key=intake_key,
                feature_id=feature_id,
                task_description=task_description,
                task_description_sha256=hashlib.sha256(
                    task_description.encode("utf-8")
                ).hexdigest(),
                toolchain_ref=toolchain_ref,
                recorded_at=now,
            )
            .on_conflict_do_nothing(index_elements=["intake_key"])
        )

    def _body(session: Session) -> str:
        if intake_key is not None:
            existing = session.execute(
                select(_jobs_table().c.job_id, _jobs_table().c.toolchain_ref)
                .where(_jobs_table().c.intake_key == intake_key)
                .limit(1)
            ).first()
            if existing is not None:
                existing_job_id, existing_toolchain = existing
                if existing_toolchain != toolchain_ref:
                    raise EnqueueConflict(
                        intake_key=intake_key,
                        existing_toolchain_ref=existing_toolchain,
                    )
                _record_intake_body(session)
                return existing_job_id

        job_id = new_id()
        result = session.execute(
            sqlite_insert(_jobs_table())
            .values(
                job_id=job_id,
                feature_id=feature_id,
                repository_id=repository_id,
                base_sha=base_sha,
                branch_name=branch_name,
                toolchain_ref=toolchain_ref,
                state="pending",
                attempt_count=0,
                lease_epoch=0,
                intake_key=intake_key,
                created_at=now,
                updated_at=now,
            )
            # The conflict target is a PARTIAL unique index, so SQLite needs
            # the index predicate restated in the upsert clause.
            .on_conflict_do_nothing(
                index_elements=["intake_key"],
                index_where=text("intake_key IS NOT NULL"),
            )
            if intake_key is not None
            else insert(_jobs_table()).values(
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
        if intake_key is not None and result.rowcount != 1:
            # The racing insert lost the unique-index arbitration inside this
            # same transaction; the winner's row is already committed or
            # pending commit, and the re-select reads it consistently.
            winner = session.execute(
                select(
                    _jobs_table().c.job_id, _jobs_table().c.toolchain_ref
                )
                .where(_jobs_table().c.intake_key == intake_key)
                .limit(1)
            ).first()
            if winner is None:
                # on_conflict_do_nothing with rowcount 0 but no visible row
                # should not happen on a single-writer SQLite file; fail
                # closed rather than mint a duplicate.
                raise EnqueueConflict(
                    intake_key=intake_key, existing_toolchain_ref="<unseen>"
                )
            winner_job_id, winner_toolchain = winner
            if winner_toolchain != toolchain_ref:
                raise EnqueueConflict(
                    intake_key=intake_key,
                    existing_toolchain_ref=winner_toolchain,
                )
            _record_intake_body(session)
            return winner_job_id

        if intake_key is not None:
            _record_intake_body(session)
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
        body = None
        body_sha = None
        if row.intake_key is not None:
            intake = session.execute(
                select(
                    FeatureIntakeRequest.task_description,
                    FeatureIntakeRequest.task_description_sha256,
                ).where(FeatureIntakeRequest.intake_key == row.intake_key)
            ).first()
            if intake is not None:
                body, body_sha = intake
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
            task_description=body,
            task_description_sha256=body_sha,
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
        # A transport lease is not permission to repeat a provider effect.
        # Any durable dispatch marker blocks automatic re-claim, including the
        # crash window before a recovery observer has parked it as unknown.
        # An explicitly approved future replacement must get a new execution
        # episode; never erase the old marker to make this predicate pass.
        provider_not_dispatched = ~select(ProviderAttempt.attempt_id).where(
            ProviderAttempt.job_id == table.c.job_id,
            ProviderAttempt.dispatch_started_at.is_not(None),
        ).correlate(table).exists()
        candidate = session.execute(
            select(table.c.job_id)
            .where(table.c.state == "pending")
            .where(provider_not_dispatched)
            .order_by(table.c.created_at)
            .limit(1)
        ).scalar_one_or_none()
        if candidate is None:
            return None
        result = session.execute(
            update(table)
            .where(table.c.job_id == candidate)
            .where(table.c.state == "pending")
            .where(provider_not_dispatched)
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
            .where(table.c.state.in_(ACTIVE_JOB_STATES))
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
            .where(table.c.state.in_(ACTIVE_JOB_STATES))
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

        if row.state not in ACTIVE_JOB_STATES:
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
            .where(table.c.state.in_(ACTIVE_JOB_STATES))
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
    expected_state: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Authority action: cancel a cancellable job, terminal and non-retryable.

    Unlike `finish_job`, this is not worker-fenced — it is the ECS lease owner's
    authority to stop a job regardless of which worker holds it. It moves only a
    `pending` or active (`leased`/`running`) row to `cancelled` (clearing the
    lease; a `pending` row has none, and never had a worker) and is idempotent
    for a job already cancelled; any other terminal state is left untouched
    (False). A cancelled job carries no result receipt.

    When `expected_state` is given, the UPDATE is fenced to it: the CAS only
    fires when the row's state still equals the caller's view, so any state
    change between the caller's pre-read and this write loses with False
    instead of succeeding through an active→active transition. Callers that
    want the old semantics (cancel whichever active state holds now) pass
    None; the operator plane always binds `expected_state`.
    """
    now = now or utc_now()
    sessions = session_factory(engine)

    def _body(session: Session) -> bool:
        table = _jobs_table()
        update_stmt = update(table).where(table.c.job_id == job_id)
        if expected_state is None:
            update_stmt = update_stmt.where(
                table.c.state.in_(("pending",) + ACTIVE_JOB_STATES)
            )
        else:
            # Fenced CAS: the caller's view, not "any active state", gates the
            # write. A state that moved after the caller's pre-read (including
            # active→active pending→leased) is a lost race, not a success.
            update_stmt = update_stmt.where(
                table.c.state == expected_state,
                table.c.state.in_(("pending",) + ACTIVE_JOB_STATES),
            )
        result = session.execute(
            update_stmt.values(
                state="cancelled",
                worker_id=None,
                lease_expires_at=None,
                heartbeat_at=None,
                updated_at=now,
            )
        )
        if result.rowcount == 1:
            return True
        if expected_state is not None:
            # Fenced: the row no longer matches the caller's view (any state
            # change after the pre-read, including another authority's cancel),
            # so this caller loses the race — the operator plane maps False to
            # a 409. Unfenced callers keep the idempotent read-back below.
            return False
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
