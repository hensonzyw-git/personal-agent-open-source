"""Version-bound stop commands. No resume or new execution authority is issued."""
from dataclasses import dataclass
from datetime import datetime
import hashlib

from sqlalchemy import Engine, select

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import _integer, _non_empty, _stop_gate, _transaction
from personal_agent_dal.storage.machine_models import ExecutionControlReceipt, ExecutionGate


@dataclass(frozen=True)
class ControlOutcome:
    code: str
    receipt_id: str | None = None
    duplicate: bool = False
    gate_version: int | None = None


def control_execution(
    engine: Engine, *, feature_id: str, operation: str, expected_gate_version: int,
    command_id: str, requested_by: str, now: datetime | None = None,
) -> ControlOutcome:
    for name, value in [('feature_id', feature_id), ('command_id', command_id),
                        ('requested_by', requested_by)]:
        _non_empty(value, name)
    _integer(expected_gate_version)
    if operation not in ('pause', 'cancel'):
        raise ValueError('unsupported execution control')
    digest = hashlib.sha256(canonical_json(dict(
        schema_version='dal.execution-control/1.0', feature_id=feature_id,
        operation=operation, expected_gate_version=expected_gate_version,
        requested_by=requested_by,
    )).encode()).hexdigest()

    def work(session):
        previous = session.scalar(select(ExecutionControlReceipt).where(
            ExecutionControlReceipt.command_id == command_id))
        if previous is not None:
            if previous.request_sha256 != digest:
                return ControlOutcome('IDEMPOTENCY_CONFLICT')
            return ControlOutcome(previous.code, previous.receipt_id, True, previous.gate_version)
        timestamp = now or utc_now()
        outcome = _stop_gate(session, feature_id, expected_gate_version,
            'paused' if operation == 'pause' else 'cancelled', timestamp)
        if outcome.code not in ('PAUSED', 'CANCELLED'):
            return ControlOutcome(outcome.code)
        gate = session.get(ExecutionGate, feature_id)
        receipt = ExecutionControlReceipt(
            receipt_id=new_id(), command_id=command_id, request_sha256=digest,
            feature_id=feature_id, operation=operation, requested_by=requested_by,
            expected_gate_version=expected_gate_version, gate_version=gate.version,
            approval_epoch=gate.approval_epoch, code=outcome.code, recorded_at=timestamp,
        )
        session.add(receipt)
        return ControlOutcome(outcome.code, receipt.receipt_id, False, gate.version)
    return _transaction(engine, work)
