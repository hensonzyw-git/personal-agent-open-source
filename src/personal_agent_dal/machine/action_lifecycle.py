"""P0-02 durable action lifecycle, before any provider composition.

Each function performs only durable state changes.  In particular,
``claim_dispatch`` grants the sole caller permission but does not make an
outward call; composition must commit that grant before invoking a provider.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import Engine, select, update

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_dal.machine.engine import TransitionCommand, apply_transition
from personal_agent_dal.machine.guards import GuardFacts
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import (
    Capability,
    ExecutionGate,
    Lease,
    TransitionReceipt,
    ProviderResultObservation,
    ProviderAttempt,
    WorkflowAction,
)
from personal_agent_dal.storage.models import Feature
from personal_agent_dal.storage.worker_models import WorkerJob


_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class ActionLifecycleRefusal(RuntimeError):
    """Stable failure code for a zero-write lifecycle refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ActionCreated:
    action_id: str
    attempt_id: str


@dataclass(frozen=True)
class LifecycleOutcome:
    code: str
    receipt_id: str | None = None


def create_provider_action(
    engine: Engine,
    *,
    feature_id: str,
    action_key: str,
    input_binding_sha256: str,
    execution_snapshot_sha256: str,
    now: datetime | None = None,
) -> ActionCreated:
    _non_empty(feature_id, "feature_id")
    _non_empty(action_key, "action_key")
    _digest(input_binding_sha256, "input_binding_sha256")
    _digest(execution_snapshot_sha256, "execution_snapshot_sha256")
    recorded_at = now or utc_now()

    def work(session) -> ActionCreated:  # noqa: ANN001
        feature = session.get(Feature, feature_id)
        if feature is None:
            raise ActionLifecycleRefusal("FEATURE_NOT_FOUND")
        existing = session.scalar(
            select(WorkflowAction).where(
                WorkflowAction.feature_id == feature_id,
                WorkflowAction.action_key == action_key,
            )
        )
        if existing is not None:
            if (
                existing.kind != "provider"
                or existing.input_binding_sha256 != input_binding_sha256
                or existing.execution_snapshot_sha256 != execution_snapshot_sha256
                or existing.active_attempt_id is None
            ):
                raise ActionLifecycleRefusal("IDEMPOTENCY_CONFLICT")
            return ActionCreated(existing.action_id, existing.active_attempt_id)
        gate = session.get(ExecutionGate, feature_id)
        if gate is None:
            gate = ExecutionGate(
                feature_id=feature_id, version=1, mode="open", approval_epoch=1,
                created_at=recorded_at, updated_at=recorded_at,
            )
            session.add(gate)
        action_id, attempt_id = new_id(), new_id()
        session.add(WorkflowAction(
            action_id=action_id, feature_id=feature_id, stage_id=None, kind="provider",
            action_key=action_key, input_binding_sha256=input_binding_sha256,
            execution_snapshot_sha256=execution_snapshot_sha256, version=1,
            active_attempt_id=attempt_id, created_at=recorded_at, updated_at=recorded_at,
        ))
        session.flush()  # Parent must exist before the immediate attempt FK.
        session.add(ProviderAttempt(
            attempt_id=attempt_id, action_id=action_id, attempt_no=1, state="prepared",
            version=1, owner_id=None, fence=0, dispatch_started_at=None, job_id=None,
            lease_id=None, job_lease_epoch=None, policy_lease_epoch=None,
            approval_epoch=gate.approval_epoch, feature_version=feature.version,
            capability_epoch=feature.capability_epoch,
            result_digest=None, result_recorded_at=None,
            result_consumed_at=None, created_at=recorded_at, updated_at=recorded_at,
        ))
        return ActionCreated(action_id, attempt_id)

    return _transaction(engine, work)


def claim_dispatch(
    engine: Engine, *, attempt_id: str, expected_version: int, owner_id: str,
    job_id: str | None = None, lease_id: str | None = None, now: datetime | None = None,
    manifest_sha256: str | None = None,
) -> LifecycleOutcome:
    """Claim an action for a currently authorised Worker; omitted leases deny."""
    _non_empty(owner_id, 'owner_id')
    def work(session, row, action, gate, timestamp):
        refusal = _authority(session, row, action, gate)
        if refusal:
            return LifecycleOutcome(refusal)
        from personal_agent_dal.storage.machine_models import ReplacementBudget
        replacement = session.scalar(select(ReplacementBudget).where(ReplacementBudget.new_attempt_id == row.attempt_id))
        if replacement and manifest_sha256 is None:
            return LifecycleOutcome('REPLACEMENT_EPISODE_REQUIRED')
        if row.state != 'prepared':
            return LifecycleOutcome('ATTEMPT_UNKNOWN' if row.state == 'unknown' else 'ATTEMPT_OWNERSHIP_LOST')
        job = session.get(WorkerJob, job_id) if job_id else None
        lease = session.get(Lease, lease_id) if lease_id else None
        refusal = _leases(job, lease, action.feature_id, owner_id, timestamp)
        if refusal:
            return LifecycleOutcome(refusal)
        if replacement:
            from personal_agent_dal.machine.resume_dispatch import validate_manifest
            from personal_agent_dal.storage.transport_models import SupervisorLaunchManifest
            manifest = session.get(SupervisorLaunchManifest, row.attempt_id)
            if not manifest or manifest.sha256 != manifest_sha256:
                return LifecycleOutcome('MANIFEST_ACKNOWLEDGEMENT_REQUIRED')
            refusal = validate_manifest(session, attempt=row, job=job, lease=lease, owner=owner_id)
            if refusal:
                return LifecycleOutcome(refusal)
        row.state = 'dispatching'
        row.version += 1
        row.owner_id = owner_id
        row.fence += 1
        row.job_id, row.lease_id = job.job_id, lease.lease_id
        row.job_lease_epoch, row.policy_lease_epoch = job.lease_epoch, lease.epoch
        row.dispatch_started_at = row.updated_at = timestamp
        return LifecycleOutcome('DISPATCH_GRANTED')
    return _mutate_attempt(engine, attempt_id, expected_version, now, work)


def record_result(
    engine: Engine, *, attempt_id: str, expected_version: int, owner_id: str,
    fence: int, digest: str, now: datetime | None = None,
) -> LifecycleOutcome:
    """Persist every valid arrival for a known attempt, including refusal evidence."""
    _non_empty(owner_id, 'owner_id')
    _digest(digest, 'digest')
    _integer(fence, minimum=0)
    def work(session, row, action, gate, timestamp):
        # Conflicting evidence must remain visible even after authority expires.
        if row.result_digest is not None and row.result_digest != digest:
            return LifecycleOutcome('ATTEMPT_RESULT_CONFLICT')
        refusal = _authority(session, row, action, gate)
        if refusal:
            return LifecycleOutcome(refusal)
        if (row.owner_id, row.fence) != (owner_id, fence):
            return LifecycleOutcome('ATTEMPT_FENCE_STALE')
        refusal = _issued_leases(session, row, action, timestamp)
        if refusal:
            return LifecycleOutcome(refusal)
        if row.state == 'result_recorded':
            return LifecycleOutcome('RESULT_REPLAY')
        if row.state != 'dispatching':
            return LifecycleOutcome('ATTEMPT_NOT_DISPATCHING')
        row.state = 'result_recorded'
        row.version += 1
        row.result_digest = digest
        row.result_recorded_at = row.updated_at = timestamp
        return LifecycleOutcome('RESULT_RECORDED')
    return _mutate_attempt(engine, attempt_id, expected_version, now, work,
                           observation=(owner_id, fence, digest))


def consume_result(
    engine: Engine, *, attempt_id: str, expected_version: int,
    command: TransitionCommand | None = None, facts: GuardFacts | None = None,
    now: datetime | None = None,
) -> LifecycleOutcome:
    """Apply the existing feature transition and consume in one retryable transaction.

    Command and facts come from the trusted workflow resolver, never the Worker.
    No external calls belong in this transaction. Stage transitions are not yet
    supported by this unit and must not be approximated by a consumed marker.
    """
    if command is None:
        return LifecycleOutcome('TRANSITION_REQUIRED')
    command = replace(command, idempotency_key=f'provider-consume:{attempt_id}')
    def work(session, row, action, gate, timestamp):
        bound_command = replace(command, command_parameters={
            **command.command_parameters, 'provider_attempt_id': row.attempt_id,
            'provider_result_digest': row.result_digest,
        })
        if row.result_consumed_at is not None:
            receipt = session.get(TransitionReceipt, row.consumption_receipt_id) if row.consumption_receipt_id else None
            if receipt is None:
                return LifecycleOutcome('CONSUMPTION_RECEIPT_MISSING')
            if receipt.request_payload_sha256 != _command_digest(bound_command):
                return LifecycleOutcome('IDEMPOTENCY_CONFLICT')
            return LifecycleOutcome('APPLIED_REPLAY', receipt.receipt_id)
        refusal = _authority(session, row, action, gate)
        if refusal:
            return LifecycleOutcome(refusal)
        if row.state != 'result_recorded':
            return LifecycleOutcome('RESULT_NOT_RECORDED')
        refusal = _issued_leases(session, row, action, timestamp)
        if refusal:
            return LifecycleOutcome(refusal)
        if (command.aggregate_type != 'feature' or command.aggregate_id != action.feature_id
                or command.expected_version != row.feature_version or action.stage_id is not None):
            return LifecycleOutcome('TRANSITION_BINDING_MISMATCH')
        # Never consume against an unrelated previously committed transition.
        if session.scalar(select(TransitionReceipt).where(
                TransitionReceipt.idempotency_key == command.idempotency_key)) is not None:
            return LifecycleOutcome('CONSUMPTION_RECEIPT_CONFLICT')
        outcome = apply_transition(engine, bound_command, facts=facts, now=timestamp,
                                   transaction_session=session)
        if outcome.receipt_code != 'APPLIED':
            return LifecycleOutcome(outcome.receipt_code)
        session.flush()
        receipt = session.scalar(select(TransitionReceipt).where(
            TransitionReceipt.idempotency_key == command.idempotency_key))
        if receipt is None:
            raise ActionLifecycleRefusal('CONSUMPTION_RECEIPT_MISSING')
        row.result_consumed_at = row.updated_at = timestamp
        row.consumption_receipt_id = receipt.receipt_id
        row.version += 1
        return LifecycleOutcome('APPLIED', receipt.receipt_id)
    return _mutate_attempt(engine, attempt_id, expected_version, now, work, replay_consumption=True)


def cancel_execution(
    engine: Engine, *, feature_id: str, expected_gate_version: int,
    now: datetime | None = None,
) -> LifecycleOutcome:
    return _stop_execution(engine, feature_id=feature_id,
        expected_gate_version=expected_gate_version, mode='cancelled', now=now)


def pause_execution(
    engine: Engine, *, feature_id: str, expected_gate_version: int,
    now: datetime | None = None,
) -> LifecycleOutcome:
    return _stop_execution(engine, feature_id=feature_id,
        expected_gate_version=expected_gate_version, mode='paused', now=now)


def _stop_execution(engine, *, feature_id, expected_gate_version, mode, now):
    _non_empty(feature_id, 'feature_id')
    _integer(expected_gate_version)
    timestamp = now or utc_now()
    def work(session):
        return _stop_gate(session, feature_id, expected_gate_version, mode, timestamp)
    return _transaction(engine, work)


def _stop_gate(session, feature_id, expected_gate_version, mode, timestamp):
    if session.get(ExecutionGate, feature_id) is None:
        return LifecycleOutcome('EXECUTION_GATE_MISSING')
    allowed = ('open', 'paused') if mode == 'cancelled' else ('open',)
    result = session.execute(update(ExecutionGate).where(
        ExecutionGate.feature_id == feature_id, ExecutionGate.version == expected_gate_version,
        ExecutionGate.mode.in_(allowed),
    ).values(mode=mode, version=expected_gate_version + 1,
             approval_epoch=ExecutionGate.approval_epoch + 1, updated_at=timestamp))
    if result.rowcount != 1:
        return LifecycleOutcome('EXECUTION_GATE_STALE')
    # Revoke remaining uses, including partially used capabilities. Evidence
    # of prior use and in-flight attempts is retained; this is not termination.
    session.execute(update(Capability).where(
        Capability.feature_id == feature_id, Capability.revoked_at.is_(None),
        Capability.uses_consumed < Capability.max_uses,
    ).values(revoked_at=timestamp))
    return LifecycleOutcome('CANCELLED' if mode == 'cancelled' else 'PAUSED')


def _mutate_attempt(engine, attempt_id, expected_version, now, mutation, *,
                    observation=None, replay_consumption=False):
    _non_empty(attempt_id, 'attempt_id')
    _integer(expected_version)
    def work(session):
        timestamp = now or utc_now()
        row = session.get(ProviderAttempt, attempt_id)
        if row is None:
            return LifecycleOutcome('ATTEMPT_NOT_FOUND')
        action = session.get(WorkflowAction, row.action_id)
        gate = session.get(ExecutionGate, action.feature_id) if action else None
        if row.version != expected_version and not (replay_consumption and row.result_consumed_at):
            outcome = LifecycleOutcome('ATTEMPT_VERSION_STALE')
        else:
            outcome = mutation(session, row, action, gate, timestamp)
        if observation is not None:
            owner, fence, digest = observation
            session.add(ProviderResultObservation(
                observation_id=new_id(), attempt_id=attempt_id, owner_id=owner, fence=fence,
                expected_version=expected_version, digest=digest, code=outcome.code,
                recorded_at=timestamp,
            ))
        return outcome
    return _transaction(engine, work)


def _authority(session, row, action, gate):
    if action is None or gate is None:
        return 'EXECUTION_GATE_MISSING'
    if gate.mode != 'open' or row.approval_epoch is None or row.approval_epoch != gate.approval_epoch:
        return 'EXECUTION_AUTHORIZATION_STALE'
    if action.active_attempt_id != row.attempt_id:
        return 'ATTEMPT_OWNERSHIP_LOST'
    feature = session.get(Feature, action.feature_id)
    if (feature is None or row.feature_version != feature.version
            or row.capability_epoch != feature.capability_epoch
            or feature.state in ('completed', 'cancelled')):
        return 'EXECUTION_AUTHORIZATION_STALE'
    return None


def _leases(job, lease, feature_id, owner, timestamp):
    if (job is None or job.feature_id != feature_id or job.worker_id != owner
            or job.state not in ('leased', 'running') or job.lease_expires_at is None
            or job.lease_expires_at <= timestamp):
        return 'JOB_LEASE_STALE'
    if (lease is None or lease.feature_id != feature_id or lease.job_id != job.job_id
            or lease.worker_id != owner or lease.revoked_at is not None
            or lease.expires_at <= timestamp):
        return 'POLICY_LEASE_STALE'
    return None


def _issued_leases(session, row, action, timestamp):
    job = session.get(WorkerJob, row.job_id) if row.job_id else None
    lease = session.get(Lease, row.lease_id) if row.lease_id else None
    refusal = _leases(job, lease, action.feature_id, row.owner_id, timestamp)
    if refusal:
        return refusal
    if row.job_lease_epoch != job.lease_epoch:
        return 'JOB_LEASE_STALE'
    if row.policy_lease_epoch != lease.epoch:
        return 'POLICY_LEASE_STALE'
    return None


def _command_digest(command):
    return hashlib.sha256(canonical_json({
        'aggregate_type': command.aggregate_type, 'aggregate_id': command.aggregate_id,
        'command_type': command.command_type, 'command_parameters': command.command_parameters,
        'reason_code': command.reason_code, 'decision_action': command.decision_action,
    }).encode('utf-8')).hexdigest()


def _transaction(engine, work):
    with session_factory(engine)() as session:
        return run_write_transaction(session, lambda: work(session))


def _integer(value, minimum=1):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ActionLifecycleRefusal('INVALID_ARGUMENT')


def _non_empty(value, field):
    if not isinstance(value, str) or not value:
        raise ActionLifecycleRefusal('INVALID_ARGUMENT')


def _digest(value, field):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ActionLifecycleRefusal('INVALID_ARGUMENT')
