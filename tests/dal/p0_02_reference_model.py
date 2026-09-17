"""Independent, in-memory oracle for P0-02 action recovery semantics.

This module intentionally lives under ``tests``: it is a specification model,
not a production controller or a convenient production dependency.  Keeping it
free of DAL runtime imports prevents a driver and its oracle from sharing the
same dispatch, lease, or persistence assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Evidence:
    kind: str
    detail: str


@dataclass(frozen=True)
class Outcome:
    code: str


@dataclass
class ExecutionGate:
    version: int = 1
    mode: str = "open"
    approval_epoch: int = 1


@dataclass
class Attempt:
    attempt_no: int = 1
    state: str = "prepared"
    owner_id: str | None = None
    fence: int = 0
    result_digest: str | None = None
    result_consumed: bool = False
    approval_epoch: int = 1


@dataclass
class ActionWorld:
    """Small state machine with explicit side-effect and authority boundaries."""

    action_id: str
    gate: ExecutionGate
    attempt: Attempt
    policy_lease_epoch: int
    job_lease_epoch: int
    outbound_call_count: int = 0
    feature_advanced: bool = False
    evidence: list[Evidence] = field(default_factory=list)

    @classmethod
    def new(
        cls, *, action_id: str, policy_lease_epoch: int, job_lease_epoch: int
    ) -> ActionWorld:
        return cls(
            action_id=action_id,
            gate=ExecutionGate(),
            attempt=Attempt(),
            policy_lease_epoch=policy_lease_epoch,
            job_lease_epoch=job_lease_epoch,
        )

    def with_job_lease_epoch(self, epoch: int) -> ActionWorld:
        if epoch <= self.job_lease_epoch:
            raise ValueError("queue lease epoch must advance monotonically")
        self.job_lease_epoch = epoch
        self.evidence.append(Evidence("queue_reclaimed", str(epoch)))
        return self

    def claim_dispatch(self, *, owner_id: str, send: bool) -> Outcome:
        if self.gate.mode != "open":
            return Outcome("EXECUTION_GATE_CLOSED")
        if self.attempt.approval_epoch != self.gate.approval_epoch:
            return Outcome("EXECUTION_AUTHORIZATION_STALE")
        if self.attempt.state == "unknown":
            return Outcome("ATTEMPT_UNKNOWN")
        if self.attempt.state != "prepared":
            return Outcome("ATTEMPT_OWNERSHIP_LOST")

        self.attempt.state = "dispatching"
        self.attempt.owner_id = owner_id
        self.attempt.fence += 1
        self.evidence.append(Evidence("dispatch_started", owner_id))
        if send:
            self.outbound_call_count += 1
            self.evidence.append(Evidence("outbound_called", owner_id))
        return Outcome("DISPATCH_GRANTED")

    def recover(self) -> Outcome:
        if self.attempt.state == "dispatching":
            self.attempt.state = "unknown"
            self.evidence.append(Evidence("attempt_parked_unknown", self.action_id))
            return Outcome("ATTEMPT_UNKNOWN")
        return Outcome("RECOVERY_NOT_NEEDED")

    def record_result(
        self,
        *,
        owner_id: str,
        fence: int,
        digest: str,
        job_lease_epoch: int,
        policy_lease_epoch: int,
    ) -> Outcome:
        if not digest:
            raise ValueError("result digest is required")
        if self.attempt.approval_epoch != self.gate.approval_epoch:
            return self._refuse_result("EXECUTION_AUTHORIZATION_STALE")
        if job_lease_epoch != self.job_lease_epoch:
            return self._refuse_result("JOB_LEASE_STALE")
        if policy_lease_epoch != self.policy_lease_epoch:
            return self._refuse_result("POLICY_LEASE_STALE")
        if self.gate.mode != "open":
            return self._refuse_result("EXECUTION_AUTHORIZATION_STALE")
        if owner_id != self.attempt.owner_id or fence != self.attempt.fence:
            return self._refuse_result("ATTEMPT_FENCE_STALE")
        if self.attempt.state == "result_recorded":
            if digest == self.attempt.result_digest:
                return Outcome("RESULT_REPLAY")
            self.evidence.append(Evidence("result_conflict", digest))
            return Outcome("ATTEMPT_RESULT_CONFLICT")
        if self.attempt.state != "dispatching":
            return self._refuse_result("ATTEMPT_NOT_DISPATCHING")

        self.attempt.state = "result_recorded"
        self.attempt.result_digest = digest
        self.evidence.append(Evidence("result_recorded", digest))
        return Outcome("RESULT_RECORDED")

    def consume_result(self) -> Outcome:
        if self.attempt.result_consumed:
            return Outcome("APPLIED_REPLAY")
        if self.gate.mode != "open":
            return Outcome("EXECUTION_GATE_CLOSED")
        if self.attempt.approval_epoch != self.gate.approval_epoch:
            return Outcome("EXECUTION_AUTHORIZATION_STALE")
        if self.attempt.state != "result_recorded":
            return Outcome("RESULT_NOT_RECORDED")

        self.attempt.result_consumed = True
        self.feature_advanced = True
        self.evidence.append(Evidence("result_consumed", self.attempt.result_digest or ""))
        return Outcome("APPLIED")

    def cancel(self, *, expected_gate_version: int) -> Outcome:
        if expected_gate_version != self.gate.version:
            return Outcome("EXECUTION_GATE_STALE")
        if self.gate.mode == "cancelled":
            return Outcome("CANCELLED_REPLAY")
        if self.gate.mode != "open":
            return Outcome("EXECUTION_GATE_CLOSED")

        self.gate.mode = "cancelled"
        self.gate.version += 1
        self.gate.approval_epoch += 1
        self.evidence.append(Evidence("execution_cancelled", self.action_id))
        return Outcome("CANCELLED")

    def resume(self, *, approval_epoch: int) -> Outcome:
        if self.gate.mode != "cancelled":
            return Outcome("EXECUTION_GATE_CLOSED")
        if approval_epoch != self.gate.approval_epoch:
            return Outcome("EXECUTION_AUTHORIZATION_STALE")

        self.gate.mode = "open"
        self.gate.version += 1
        self.evidence.append(Evidence("execution_resumed", str(approval_epoch)))
        return Outcome("RESUMED")

    def create_replacement(self, *, accepted_duplicate_cost: bool) -> Outcome:
        if self.gate.mode != "open":
            return Outcome("EXECUTION_GATE_CLOSED")
        if self.attempt.state != "unknown":
            return Outcome("ATTEMPT_NOT_UNKNOWN")
        if not accepted_duplicate_cost:
            return Outcome("DUPLICATE_COST_NOT_ACCEPTED")

        self.attempt.state = "superseded"
        self.evidence.append(Evidence("attempt_superseded", str(self.attempt.attempt_no)))
        self.attempt = Attempt(attempt_no=self.attempt.attempt_no + 1,
                               fence=self.attempt.fence, approval_epoch=self.gate.approval_epoch)
        self.evidence.append(Evidence("replacement_prepared", str(self.attempt.attempt_no)))
        return Outcome("REPLACEMENT_CREATED")

    def _refuse_result(self, code: str) -> Outcome:
        self.evidence.append(Evidence("result_refused", code))
        return Outcome(code)
