"""The Worker transport port and its local SQLite adapter (DAL-R06).

Before this module the runnable worker called `personal_agent_dal.worker.queue`
directly, which meant a production worker had to open the whole Workflow SQLite
database. On the Mac mini that is the wrong trust boundary: the worker would
hold write access to every feature, run, approval and audit row in order to
claim one job. This port is the seam that removes it — `poll_once` now talks to
a `WorkerTransport`, and only the *local* adapter is backed by the database.

Two rules shape the interface:

* **Uniform signatures** (CLAUDE.md §5.2). Both adapters are dispatched from the
  same call sites, so both implement every method with the identical signature,
  including the ones a given adapter has nothing to do (`reclaim_expired` on the
  remote side). Adapting at the call site is what leaves one branch un-adapted.
* **Every outcome is a closed value, never a bare bool.** "The lease is gone"
  and "the job was cancelled" are different facts that the caller must be able
  to distinguish, and a transport may not collapse them into a falsy return.

Lease reclamation is deliberately asymmetric and that asymmetry is the point: it
is ECS authority. The local adapter reclaims because there the worker *is* the
authority; the remote adapter returns nothing and contacts no endpoint, because
a remote worker expiring another worker's lease would be a second authority over
the same rows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from sqlalchemy import Engine

from personal_agent_core.manifest import canonical_json, sha256_of

from personal_agent_dal.worker import checkpoint as checkpoint_mod
from personal_agent_dal.worker import queue
from personal_agent_dal.worker.checkpoint import CheckpointBundle

#: Terminal states a worker may report. `expired` and `cancelled` are authority
#: transitions the worker never submits.
WORKER_TERMINAL_STATES: Final[tuple[str, str]] = ("succeeded", "failed")

#: Sensitivity class for a checkpoint artifact, per the frozen transport contract.
CHECKPOINT_SENSITIVITY: Final[str] = "checkpoint"


class TransportError(RuntimeError):
    """A transport-level refusal the caller must fail closed on.

    `reason` is a bounded, non-secret category (never a response body, URL or
    credential); `job_id` is present when the failure is bound to a claimed job.
    """

    def __init__(self, reason: str, *, job_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.job_id = job_id


class TransportDisabledError(TransportError):
    """The authority side is refusing all work (kill switch, revoked worker)."""


@dataclass(frozen=True)
class JobLease:
    """One claimed job and the fence that authorises writing about it.

    `feature_id` is authoritative from the claim, not parsed out of
    `branch_name`; the caller still checks the two agree.
    """

    job_id: str
    feature_id: str
    repository_id: str
    base_sha: str
    branch_name: str
    toolchain_ref: str
    lease_epoch: int
    attempt: int
    deadline: datetime | None


@dataclass(frozen=True)
class HeartbeatOutcome:
    """Whether the lease survived, and whether a cancel was observed.

    `alive=False, cancel_requested=True` is a cancel; `alive=False,
    cancel_requested=False` is a lost or expired lease. The caller must not
    treat them as the same event.
    """

    alive: bool
    cancel_requested: bool


@dataclass(frozen=True)
class CheckpointOutcome:
    """Whether the authority accepted this checkpoint.

    `conflict` means the same `(job_id, sequence)` was already recorded with a
    different digest — an integrity failure, never an idempotent replay.
    """

    recorded: bool
    stale: bool
    conflict: bool


@dataclass(frozen=True)
class ResultOutcome:
    """Whether the authority accepted this result.

    `replay` is a committed-response-loss retry of the identical result;
    `conflict` is the same job with a different digest, which is refused rather
    than overwritten.
    """

    accepted: bool
    replay: bool
    receipt_id: str | None
    conflict: bool
    cancelled: bool
    stale: bool


def checkpoint_artifact(bundle: CheckpointBundle) -> tuple[str, int]:
    """Return the `(sha256, size_bytes)` the transport reports for a bundle.

    Both are derived from the same canonical encoding the local file uses, so
    the digest an authority stores identifies the exact bytes on the worker's
    disk — a checkpoint whose recorded digest cannot be recomputed from the
    stored file is detectable rather than plausible.
    """
    body = asdict(bundle)
    return sha256_of(body), len(canonical_json(body).encode("utf-8"))


class WorkerTransport(ABC):
    """The queue operations a worker needs, with no database assumption."""

    @abstractmethod
    def reclaim_expired(self) -> tuple[str, ...]:
        """Reclaim expired leases where that is this side's authority."""

    @abstractmethod
    def claim(self) -> JobLease | None:
        """Claim the next pending job, or None when there is nothing to do."""

    @abstractmethod
    def mark_running(self, lease: JobLease) -> HeartbeatOutcome:
        """Announce execution start against this fence.

        Returns the same outcome shape as `heartbeat` because start and refresh
        can fail for the same two distinct reasons, and a cancel observed at
        start must not be reported as a lost lease.
        """

    @abstractmethod
    def heartbeat(self, lease: JobLease) -> HeartbeatOutcome:
        """Refresh the lease and observe cancellation in the same round trip."""

    @abstractmethod
    def record_checkpoint(
        self, lease: JobLease, bundle: CheckpointBundle, *, sequence: int
    ) -> CheckpointOutcome:
        """Persist the resume point locally and register it with the authority."""

    @abstractmethod
    def submit_result(
        self,
        lease: JobLease,
        *,
        state: str,
        result_sha256: str | None,
        last_error: str | None,
    ) -> ResultOutcome:
        """Submit the terminal result for this fenced lease."""

    @abstractmethod
    def close(self) -> None:
        """Release whatever this transport holds open."""


class LocalSQLiteAdapter(WorkerTransport):
    """The G2 behaviour, unchanged, expressed through the port.

    Every method is the same `queue` primitive the runnable worker already used,
    so the offline acceptance for DAL-014..020 continues to exercise the exact
    code path it did before; the only new thing is the interface it arrives
    through.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        worker_id: str,
        lease_ttl_seconds: int,
        max_attempts: int,
        checkpoint_root: Path,
    ) -> None:
        self._engine = engine
        self._worker_id = worker_id
        self._lease_ttl_seconds = lease_ttl_seconds
        self._max_attempts = max_attempts
        self._checkpoint_root = checkpoint_root

    def reclaim_expired(self) -> tuple[str, ...]:
        return tuple(
            queue.reclaim_expired(self._engine, max_attempts=self._max_attempts)
        )

    def claim(self) -> JobLease | None:
        job_id = queue.claim_job(
            self._engine,
            worker_id=self._worker_id,
            lease_ttl_seconds=self._lease_ttl_seconds,
        )
        if job_id is None:
            return None
        record = queue.get_job(self._engine, job_id=job_id)
        if record is None:
            # The row was claimed and then vanished: refuse rather than invent a
            # lease, and name the job so the caller can report which one it was.
            raise TransportError("job_row_missing", job_id=job_id)
        return JobLease(
            job_id=record.job_id,
            feature_id=record.feature_id,
            repository_id=record.repository_id,
            base_sha=record.base_sha,
            branch_name=record.branch_name,
            toolchain_ref=record.toolchain_ref,
            lease_epoch=record.lease_epoch,
            attempt=record.attempt_count,
            deadline=record.lease_expires_at,
        )

    def mark_running(self, lease: JobLease) -> HeartbeatOutcome:
        started = queue.mark_running(
            self._engine,
            job_id=lease.job_id,
            worker_id=self._worker_id,
            lease_epoch=lease.lease_epoch,
        )
        if started:
            return HeartbeatOutcome(alive=True, cancel_requested=False)
        record = queue.get_job(self._engine, job_id=lease.job_id)
        cancelled = record is not None and record.state == "cancelled"
        return HeartbeatOutcome(alive=False, cancel_requested=cancelled)

    def heartbeat(self, lease: JobLease) -> HeartbeatOutcome:
        alive = queue.heartbeat(
            self._engine,
            job_id=lease.job_id,
            worker_id=self._worker_id,
            lease_epoch=lease.lease_epoch,
            lease_ttl_seconds=self._lease_ttl_seconds,
        )
        if alive:
            return HeartbeatOutcome(alive=True, cancel_requested=False)
        # A refused heartbeat is either a cancel or a lost lease. Read the row to
        # tell them apart, so the local adapter reports the same distinction the
        # remote contract carries in its response body.
        record = queue.get_job(self._engine, job_id=lease.job_id)
        cancelled = record is not None and record.state == "cancelled"
        return HeartbeatOutcome(alive=False, cancel_requested=cancelled)

    def record_checkpoint(
        self, lease: JobLease, bundle: CheckpointBundle, *, sequence: int
    ) -> CheckpointOutcome:
        checkpoint_mod.write_checkpoint(self._checkpoint_root, bundle)
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
        try:
            accepted = queue.finish_job(
                self._engine,
                job_id=lease.job_id,
                worker_id=self._worker_id,
                lease_epoch=lease.lease_epoch,
                state=state,
                result_sha256=result_sha256,
                last_error=last_error,
            )
        except queue.ResultConflictError:
            return ResultOutcome(
                accepted=False,
                replay=False,
                receipt_id=None,
                conflict=True,
                cancelled=False,
                stale=False,
            )
        if accepted:
            return ResultOutcome(
                accepted=True,
                replay=False,
                receipt_id=None,
                conflict=False,
                cancelled=False,
                stale=False,
            )
        record = queue.get_job(self._engine, job_id=lease.job_id)
        cancelled = record is not None and record.state == "cancelled"
        return ResultOutcome(
            accepted=False,
            replay=False,
            receipt_id=None,
            conflict=False,
            cancelled=cancelled,
            stale=not cancelled,
        )

    def close(self) -> None:
        # The engine is owned by the caller that built it; disposing it here
        # would close a connection pool the caller may still be using.
        return None
