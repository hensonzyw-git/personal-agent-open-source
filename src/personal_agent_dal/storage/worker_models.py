"""Worker runtime tables — revision `0005_worker_queue` (DAL-016 runnable layer).

The G2 frozen gate closed on the *pure policy* half of DAL-016 (lease / epoch
result-acceptance decisions in `machine/lease.py` and `machine/epoch.py`). What
was still missing is the runnable layer those decisions govern: a durable job
queue a worker process can actually claim, heartbeat and complete. These two
tables are that layer.

`worker_jobs` is the queue. It is deliberately **separate** from the
state-machine `leases` table: `leases` is the policy-level claim bound to a
feature plus its approval/capability epochs (issued by `issue_worker_lease`,
judged by `accept_worker_result`), whereas `worker_jobs` is the lower-level
"which worker process is running this job right now, for how long" record. The
claim is a compare-and-swap on `state='pending'`; expiry reclaims a stale
`leased`/`running` row back to `pending` (or `expired` once the attempt budget
is exhausted).

`worker_result_receipts` is the idempotent result. Its unique `job_id` is the
idempotency key: one result per job, so a replayed completion returns the
original receipt instead of recording a second one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent_core.sqlite import UtcTimestamp

from personal_agent_dal.storage.models import Base, _hex_of_length, _in_set


#: `WorkerJobState`. `succeeded`, `failed`, `expired` and `cancelled` are terminal.
WORKER_JOB_STATES: Final[tuple[str, ...]] = (
    "pending",
    "leased",
    "running",
    "succeeded",
    "failed",
    "expired",
    "cancelled",
)


class WorkerJob(Base):
    """One unit of deterministic work a Home Mac Worker may claim and run.

    ``lease_epoch`` is a monotonic fencing token. It starts at zero and is never
    cleared: every claim increments it, so an old process can never regain
    authority merely because a replacement process uses the same ``worker_id``.
    The remaining lease fields travel together and exist only in active states.
    """

    __tablename__ = "worker_jobs"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    repository_id: Mapped[str] = mapped_column(Text, nullable=False)
    base_sha: Mapped[str] = mapped_column(Text, nullable=False)
    branch_name: Mapped[str] = mapped_column(Text, nullable=False)
    toolchain_ref: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(_in_set("state", WORKER_JOB_STATES), name="state"),
        CheckConstraint(
            _hex_of_length("base_sha", 40, nullable=False), name="base_sha_hex"
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("lease_epoch >= 0", name="lease_epoch_non_negative"),
        CheckConstraint(
            "(worker_id IS NULL) = (lease_expires_at IS NULL) "
            "AND (worker_id IS NULL) = (heartbeat_at IS NULL)",
            name="lease_fields_travel_together",
        ),
        CheckConstraint(
            "((state IN ('leased', 'running')) AND worker_id IS NOT NULL) OR "
            "((state NOT IN ('leased', 'running')) AND worker_id IS NULL)",
            name="active_state_has_lease",
        ),
        Index("ix_worker_jobs_state", "state"),
        Index("ix_worker_jobs_feature_id", "feature_id"),
    )


class WorkerResultReceipt(Base):
    """The idempotent result of one job. `job_id` is the idempotency key.

    A result that reuses a job's key returns the original receipt; the unique
    constraint is the database-level backstop that stops a retry from recording
    a second result for one job.
    """

    __tablename__ = "worker_result_receipts"

    receipt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("worker_jobs.job_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    result_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _hex_of_length("result_sha256", 64, nullable=False),
            name="result_sha256_hex",
        ),
        UniqueConstraint("job_id", name="uq_worker_result_receipts_job_id"),
    )
