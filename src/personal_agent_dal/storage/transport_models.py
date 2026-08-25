"""Worker Transport tables — revision `0006_worker_transport` (DAL-R04/R05).

These are the server-side (DWS) records the thin-http transport needs, kept
separate from the state-machine tables and from `worker_models`:

- `worker_enrollments` is the durable "which machine may call this transport"
  registry. A worker is enrolled by `POST /enroll`; revocation is a `revoked_at`
  timestamp the auth check refuses. The capability list is the closed transport
  vocabulary, not a business authority.
- `worker_checkpoints` is the ECS-side recovery evidence: checkpoint metadata +
  content-addressed artifact reference, fenced by `(job_id, lease_epoch,
  sequence)` and idempotent on `(job_id, sequence)`. The artifact bytes are not
  stored here — only the reference the result receipt binds to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent_core.sqlite import UtcTimestamp

from personal_agent_dal.storage.models import Base, _hex_of_length, _in_set


#: Closed transport capability vocabulary (`dal.worker-transport/1.0`).
WORKER_CAPABILITIES: Final[tuple[str, ...]] = ("coding", "verification", "checkpoint")

#: Closed artifact sensitivity classification.
SENSITIVITY_CLASSES: Final[tuple[str, ...]] = ("checkpoint", "diff", "log")


class WorkerEnrollment(Base):
    """A machine identity the transport will accept requests from."""

    __tablename__ = "worker_enrollments"

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    capabilities: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)

    __table_args__ = (
        CheckConstraint("length(worker_id) >= 1", name="worker_id_nonempty"),
    )


class WorkerCheckpoint(Base):
    """One checkpoint's metadata + content-addressed artifact reference."""

    __tablename__ = "worker_checkpoints"

    checkpoint_id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("worker_jobs.job_id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_files: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_worker_checkpoints_job_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("lease_epoch >= 0", name="lease_epoch_nonnegative"),
        CheckConstraint("artifact_size_bytes >= 1", name="artifact_size_positive"),
        CheckConstraint(
            _hex_of_length("artifact_sha256", 64, nullable=False),
            name="artifact_sha256_hex",
        ),
        CheckConstraint(
            _in_set("sensitivity", SENSITIVITY_CLASSES), name="sensitivity"
        ),
        Index("ix_worker_checkpoints_job_id", "job_id"),
    )
