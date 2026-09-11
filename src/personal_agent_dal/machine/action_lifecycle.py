"""P0-02 durable action lifecycle, before any provider composition.

Each function performs only durable state changes.  In particular,
``claim_dispatch`` grants the sole caller permission but does not make an
outward call; composition must commit that grant before invoking a provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import Engine, select, update

from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import (
    ExecutionGate,
    ProviderAttempt,
    WorkflowAction,
)
from personal_agent_dal.storage.models import Feature


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
        if session.get(Feature, feature_id) is None:
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
            session.add(ExecutionGate(
                feature_id=feature_id, version=1, mode="open", approval_epoch=1,
                created_at=recorded_at, updated_at=recorded_at,
            ))
        action_id, attempt_id = new_id(), new_id()
        session.add(WorkflowAction(
            action_id=action_id, feature_id=feature_id, stage_id=None, kind="provider",
            action_key=action_key, input_binding_sha256=input_binding_sha256,
            execution_snapshot_sha256=execution_snapshot_sha256, version=1,
            active_attempt_id=attempt_id, created_at=recorded_at, updated_at=recorded_at,
        ))
        session.add(ProviderAttempt(
            attempt_id=attempt_id, action_id=action_id, attempt_no=1, state="prepared",
            version=1, owner_id=None, fence=0, dispatch_started_at=None, job_id=None,
            lease_id=None, job_lease_epoch=None, policy_lease_epoch=None,
            approval_epoch=None, result_digest=None, result_recorded_at=None,
            result_consumed_at=None, created_at=recorded_at, updated_at=recorded_at,
        ))
        return ActionCreated(action_id, attempt_id)

    return _transaction(engine, work)


def claim_dispatch(
    engine: Engine, *, attempt_id: str, expected_version: int, owner_id: str,
    now: datetime | None = None,
) -> LifecycleOutcome:
    _non_empty(attempt_id, "attempt_id"); _non_empty(owner_id, "owner_id")
    return _mutate_attempt(
        engine, attempt_id, expected_version, now,
        lambda session, row, gate, timestamp: _claim(session, row, gate, owner_id, timestamp),
    )


def record_result(
    engine: Engine, *, attempt_id: str, expected_version: int, owner_id: str,
    fence: int, digest: str, now: datetime | None = None,
) -> LifecycleOutcome:
    _non_empty(attempt_id, "attempt_id"); _non_empty(owner_id, "owner_id"); _digest(digest, "digest")
    if not isinstance(fence, int) or isinstance(fence, bool) or fence < 0:
        raise ActionLifecycleRefusal("INVALID_ARGUMENT")
    return _mutate_attempt(
        engine, attempt_id, expected_version, now,
        lambda session, row, gate, timestamp: _record(session, row, gate, owner_id, fence, digest, timestamp),
    )


def consume_result(
    engine: Engine, *, attempt_id: str, expected_version: int,
    now: datetime | None = None,
) -> LifecycleOutcome:
    return _mutate_attempt(
        engine, attempt_id, expected_version, now,
        lambda session, row, gate, timestamp: _consume(session, row, gate, timestamp),
    )


def cancel_execution(
    engine: Engine, *, feature_id: str, expected_gate_version: int,
    now: datetime | None = None,
) -> LifecycleOutcome:
    _non_empty(feature_id, "feature_id")
    if not isinstance(expected_gate_version, int) or expected_gate_version < 1:
        raise ActionLifecycleRefusal("INVALID_ARGUMENT")
    timestamp = now or utc_now()
    def work(session):  # noqa: ANN001
        gate = session.get(ExecutionGate, feature_id)
        if gate is None:
            return LifecycleOutcome("EXECUTION_GATE_MISSING")
        result = session.execute(update(ExecutionGate).where(
            ExecutionGate.feature_id == feature_id, ExecutionGate.version == expected_gate_version,
            ExecutionGate.mode == "open",
        ).values(mode="cancelled", version=expected_gate_version + 1,
                 approval_epoch=ExecutionGate.approval_epoch + 1, updated_at=timestamp))
        return LifecycleOutcome("CANCELLED" if result.rowcount == 1 else "EXECUTION_GATE_STALE")
    return _transaction(engine, work)


def _mutate_attempt(engine: Engine, attempt_id: str, expected_version: int, now: datetime | None,
                    mutation: Callable) -> LifecycleOutcome:
    if not isinstance(expected_version, int) or expected_version < 1:
        raise ActionLifecycleRefusal("INVALID_ARGUMENT")
    timestamp = now or utc_now()
    def work(session):  # noqa: ANN001
        row = session.get(ProviderAttempt, attempt_id)
        if row is None:
            return LifecycleOutcome("ATTEMPT_NOT_FOUND")
        if row.version != expected_version:
            return LifecycleOutcome("ATTEMPT_VERSION_STALE")
        action = session.get(WorkflowAction, row.action_id)
        gate = session.get(ExecutionGate, action.feature_id) if action else None
        if action is None or gate is None:
            return LifecycleOutcome("EXECUTION_GATE_MISSING")
        return mutation(session, row, gate, timestamp)
    return _transaction(engine, work)


def _claim(session, row, gate, owner_id, timestamp):  # noqa: ANN001
    if gate.mode != "open": return LifecycleOutcome("EXECUTION_GATE_CLOSED")
    if row.state == "unknown": return LifecycleOutcome("ATTEMPT_UNKNOWN")
    if row.state != "prepared": return LifecycleOutcome("ATTEMPT_OWNERSHIP_LOST")
    result = session.execute(update(ProviderAttempt).where(
        ProviderAttempt.attempt_id == row.attempt_id, ProviderAttempt.version == row.version,
        ProviderAttempt.state == "prepared",
    ).values(state="dispatching", version=row.version + 1, owner_id=owner_id,
             fence=row.fence + 1, dispatch_started_at=timestamp, updated_at=timestamp))
    return LifecycleOutcome("DISPATCH_GRANTED" if result.rowcount == 1 else "ATTEMPT_VERSION_STALE")


def _record(session, row, gate, owner_id, fence, digest, timestamp):  # noqa: ANN001
    if gate.mode != "open": return LifecycleOutcome("EXECUTION_AUTHORIZATION_STALE")
    if row.owner_id != owner_id or row.fence != fence: return LifecycleOutcome("ATTEMPT_FENCE_STALE")
    if row.state == "result_recorded":
        return LifecycleOutcome("RESULT_REPLAY" if row.result_digest == digest else "ATTEMPT_RESULT_CONFLICT")
    if row.state != "dispatching": return LifecycleOutcome("ATTEMPT_NOT_DISPATCHING")
    result = session.execute(update(ProviderAttempt).where(
        ProviderAttempt.attempt_id == row.attempt_id, ProviderAttempt.version == row.version,
        ProviderAttempt.state == "dispatching",
    ).values(state="result_recorded", version=row.version + 1, result_digest=digest,
             result_recorded_at=timestamp, updated_at=timestamp))
    return LifecycleOutcome("RESULT_RECORDED" if result.rowcount == 1 else "ATTEMPT_VERSION_STALE")


def _consume(session, row, gate, timestamp):  # noqa: ANN001
    if row.result_consumed_at is not None: return LifecycleOutcome("APPLIED_REPLAY")
    if gate.mode != "open": return LifecycleOutcome("EXECUTION_GATE_CLOSED")
    if row.state != "result_recorded": return LifecycleOutcome("RESULT_NOT_RECORDED")
    result = session.execute(update(ProviderAttempt).where(
        ProviderAttempt.attempt_id == row.attempt_id, ProviderAttempt.version == row.version,
        ProviderAttempt.result_consumed_at.is_(None),
    ).values(version=row.version + 1, result_consumed_at=timestamp, updated_at=timestamp))
    return LifecycleOutcome("APPLIED" if result.rowcount == 1 else "ATTEMPT_VERSION_STALE")


def _transaction(engine: Engine, work):  # noqa: ANN001
    with session_factory(engine)() as session:
        return run_write_transaction(session, lambda: work(session))


def _non_empty(value: object, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ActionLifecycleRefusal("INVALID_ARGUMENT")


def _digest(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ActionLifecycleRefusal("INVALID_ARGUMENT")
