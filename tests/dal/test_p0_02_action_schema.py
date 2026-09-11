"""P0-02 first production unit: persistent action/attempt/gate schema."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.storage.machine_models import (
    ExecutionGate,
    ProviderAttempt,
    WorkflowAction,
)


def test_p0_02_action_models_have_closed_authority_columns() -> None:
    """Models expose bindings and fences; no action may be an unbound blob."""
    assert set(WorkflowAction.__table__.columns.keys()) == {
        "action_id", "feature_id", "stage_id", "kind", "action_key",
        "input_binding_sha256", "execution_snapshot_sha256", "version",
        "active_attempt_id", "created_at", "updated_at",
    }
    assert set(ProviderAttempt.__table__.columns.keys()) == {
        "attempt_id", "action_id", "attempt_no", "state", "version",
        "owner_id", "fence", "dispatch_started_at", "job_id", "lease_id",
        "job_lease_epoch", "policy_lease_epoch", "approval_epoch",
        "result_digest", "result_recorded_at", "result_consumed_at", "created_at",
        "updated_at", "feature_version", "capability_epoch", "consumption_receipt_id",
    }
    assert set(ExecutionGate.__table__.columns.keys()) == {
        "feature_id", "version", "mode", "approval_epoch", "created_at", "updated_at",
    }

    action_constraints = {constraint.name for constraint in WorkflowAction.__table__.constraints}
    attempt_constraints = {constraint.name for constraint in ProviderAttempt.__table__.constraints}
    gate_constraints = {constraint.name for constraint in ExecutionGate.__table__.constraints}
    assert "uq_workflow_actions_feature_action_key" in action_constraints
    assert "uq_provider_attempts_action_attempt" in attempt_constraints
    assert {
        "ck_execution_gates_version_positive",
        "ck_execution_gates_approval_epoch_non_negative",
        "ck_execution_gates_mode",
    } <= gate_constraints


def test_p0_02_schema_migrates_and_downgrades_cleanly(tmp_path: Path) -> None:
    """0011 must be a real reversible migration, not metadata-only schema."""
    database = tmp_path / "p0-02.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    inspector = inspect(engine)
    assert {"workflow_actions", "provider_attempts", "execution_gates"} <= set(
        inspector.get_table_names()
    )

    db.downgrade(engine, "0010")
    inspector = inspect(engine)
    assert not {"workflow_actions", "provider_attempts", "execution_gates"} & set(
        inspector.get_table_names()
    )

    db.upgrade(engine)
    inspector = inspect(engine)
    assert {"workflow_actions", "provider_attempts", "execution_gates"} <= set(
        inspector.get_table_names()
    )
    engine.dispose()
