"""I3 read-only observation and durable parking, never provider redispatch.

The probe is a trusted control-plane readback adapter, not a Worker-supplied
callback. It runs without an open database transaction. The default records
that no probe is composed; even a complete observation is evidence only until
an artifact resolver and renewed authority can validate consumption.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import (
    ActionLifecycleRefusal, _authority, _issued_leases,
)
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import (
    ExecutionGate, ProviderAttempt, ProviderRecoveryReceipt, WorkflowAction,
)

_SHA256 = re.compile(r'\A[0-9a-f]{64}\Z')
_STATUSES = frozenset({'unavailable', 'running', 'stopped', 'partial', 'complete'})


@dataclass(frozen=True)
class RecoveryTarget:
    attempt_id: str
    action_id: str
    feature_id: str
    version: int
    owner_id: str
    fence: int
    job_id: str | None
    lease_id: str | None
    job_lease_epoch: int | None
    policy_lease_epoch: int | None
    approval_epoch: int | None
    input_binding_sha256: str
    execution_snapshot_sha256: str


@dataclass(frozen=True)
class RecoveryReadback:
    target: RecoveryTarget
    status: str
    evidence_sha256: str | None


@dataclass(frozen=True)
class RecoveryOutcome:
    code: str
    receipt_id: str | None = None
    duplicate: bool = False
    probe_status: str | None = None
    probe_code: str | None = None


ReadbackProbe = Callable[[RecoveryTarget], RecoveryReadback]


def _replay(session: Session, command_id: str, digest: str) -> RecoveryOutcome | None:
    receipt = session.scalar(select(ProviderRecoveryReceipt).where(
        ProviderRecoveryReceipt.command_id == command_id))
    if receipt is None:
        return None
    if receipt.request_sha256 != digest:
        return RecoveryOutcome('IDEMPOTENCY_CONFLICT')
    return RecoveryOutcome(receipt.code, receipt.receipt_id, True,
                           receipt.probe_status, receipt.probe_code)


def _eligible(session: Session, row: ProviderAttempt, now: datetime) -> str | None:
    from personal_agent_dal.machine.execution_results import has_complete_evidence
    if row.result_consumed_at or row.report_receipt_id or row.result_digest is not None or has_complete_evidence(session, row.attempt_id):
        return 'RECOVERY_NOT_NEEDED'
    if row.state not in ('dispatching', 'unknown'):
        return 'RECOVERY_NOT_NEEDED'
    if row.owner_id is None or row.fence < 1 or row.dispatch_started_at is None:
        return 'ATTEMPT_BINDING_INCOMPLETE'
    action = session.get(WorkflowAction, row.action_id)
    if action is None or action.active_attempt_id != row.attempt_id:
        return 'ATTEMPT_OWNERSHIP_LOST'
    gate = session.get(ExecutionGate, action.feature_id)
    if row.state == 'dispatching' and not _authority(session, row, action, gate):
        if not _issued_leases(session, row, action, now):
            return 'RECOVERY_NOT_NEEDED'
    return None


def _target(session: Session, row: ProviderAttempt) -> RecoveryTarget:
    action = session.get(WorkflowAction, row.action_id)
    return RecoveryTarget(
        row.attempt_id, row.action_id, action.feature_id, row.version, row.owner_id,
        row.fence, row.job_id, row.lease_id, row.job_lease_epoch, row.policy_lease_epoch,
        row.approval_epoch, action.input_binding_sha256, action.execution_snapshot_sha256,
    )


def _observe(target: RecoveryTarget, probe: ReadbackProbe | None) -> tuple[RecoveryReadback, str]:
    missing = RecoveryReadback(target, 'unavailable', None)
    if probe is None:
        return missing, 'PROBE_NOT_COMPOSED'
    try:
        result = probe(target)
    except Exception:
        # Provider/worker exception text may contain paths, output or credentials.
        return missing, 'PROBE_FAILED'
    if (not isinstance(result, RecoveryReadback) or result.target != target
            or not isinstance(result.status, str) or result.status not in _STATUSES
            or (result.evidence_sha256 is not None and
                (not isinstance(result.evidence_sha256, str) or
                 _SHA256.fullmatch(result.evidence_sha256) is None))
            or (result.status != 'unavailable' and result.evidence_sha256 is None)):
        return missing, 'PROBE_INVALID'
    return result, 'PROBE_OBSERVED'


def recover_attempt(
    engine: Engine, *, attempt_id: str, expected_version: int, command_id: str,
    requested_by: str, probe: ReadbackProbe | None = None, now: datetime | None = None,
) -> RecoveryOutcome:
    """Observe an abandoned dispatch/unknown attempt and persist a CAS receipt.

    An active owner is not disturbed. Lost authority permits stopping, never
    redispatch or promotion to success. The same command replays its receipt;
    a new command may gather further evidence for an already unknown attempt.
    """
    if (any(not isinstance(value, str) or not value for value in
            (attempt_id, command_id, requested_by))
            or type(expected_version) is not int or expected_version < 1):
        raise ActionLifecycleRefusal('INVALID_ARGUMENT')
    digest = hashlib.sha256(canonical_json({
        'attempt_id': attempt_id, 'expected_version': expected_version,
        'requested_by': requested_by, 'operation': 'provider.recover/1',
    }).encode('utf-8')).hexdigest()
    with session_factory(engine)() as session:
        replay = _replay(session, command_id, digest)
        if replay is not None:
            return replay
        row = session.get(ProviderAttempt, attempt_id)
        if row is None:
            return RecoveryOutcome('ATTEMPT_NOT_FOUND')
        if row.version != expected_version:
            return RecoveryOutcome('ATTEMPT_VERSION_STALE')
        refusal = _eligible(session, row, now or utc_now())
        if refusal:
            return RecoveryOutcome(refusal)
        target = _target(session, row)

    observed, probe_code = _observe(target, probe)

    def finish(session: Session) -> RecoveryOutcome:
        replay = _replay(session, command_id, digest)
        if replay is not None:
            return replay
        timestamp = now or utc_now()
        row = session.get(ProviderAttempt, attempt_id)
        if row is None:
            return RecoveryOutcome('ATTEMPT_NOT_FOUND')
        if row.version != target.version:
            code = 'ATTEMPT_VERSION_STALE'
        elif _target(session, row) != target:
            code = 'ATTEMPT_BINDING_STALE'
        else:
            code = _eligible(session, row, timestamp) or 'ATTEMPT_UNKNOWN'
        if code == 'ATTEMPT_UNKNOWN' and row.state == 'dispatching':
            row.state = 'unknown'
            row.version += 1
            row.updated_at = timestamp
        receipt_id = new_id()
        session.add(ProviderRecoveryReceipt(
            receipt_id=receipt_id, command_id=command_id, request_sha256=digest,
            attempt_id=attempt_id, expected_version=expected_version, requested_by=requested_by,
            owner_id=target.owner_id, fence=target.fence, probe_status=observed.status,
            probe_code=probe_code, evidence_sha256=observed.evidence_sha256,
            code=code, recorded_at=timestamp,
        ))
        return RecoveryOutcome(code, receipt_id, False, observed.status, probe_code)

    with session_factory(engine)() as session:
        return run_write_transaction(session, lambda: finish(session))
