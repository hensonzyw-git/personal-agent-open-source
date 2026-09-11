"""P0-02 action lifecycle persistence, exercised against real SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from personal_agent_dal.machine.action_lifecycle import (
    ActionLifecycleRefusal,
    cancel_execution,
    claim_dispatch,
    consume_result,
    create_provider_action,
    record_result,
)
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory
from personal_agent_dal.storage.machine_models import ExecutionGate, ProviderAttempt

from tests.dal.factories import EMPTY_SHA256, feature_row


def _engine(tmp_path: Path):  # noqa: ANN201
    engine = create_database_engine(tmp_path / "p0-02-lifecycle.db")
    db.upgrade(engine)
    with session_factory(engine)() as session, session.begin():
        session.add(feature_row(feature_id="feature-1", version=1, state="coding"))
    return engine


def test_create_is_bound_and_idempotent_but_rejects_drift(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    first = create_provider_action(
        engine, feature_id="feature-1", action_key="coding:1",
        input_binding_sha256=EMPTY_SHA256,
        execution_snapshot_sha256=EMPTY_SHA256,
    )
    replay = create_provider_action(
        engine, feature_id="feature-1", action_key="coding:1",
        input_binding_sha256=EMPTY_SHA256,
        execution_snapshot_sha256=EMPTY_SHA256,
    )
    assert replay.action_id == first.action_id
    assert replay.attempt_id == first.attempt_id
    with pytest.raises(ActionLifecycleRefusal, match="IDEMPOTENCY_CONFLICT"):
        create_provider_action(
            engine, feature_id="feature-1", action_key="coding:1",
            input_binding_sha256="a" * 64,
            execution_snapshot_sha256=EMPTY_SHA256,
        )


def test_dispatch_result_and_consume_are_cas_bound(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    created = create_provider_action(
        engine, feature_id="feature-1", action_key="coding:1",
        input_binding_sha256=EMPTY_SHA256,
        execution_snapshot_sha256=EMPTY_SHA256,
    )
    claimed = claim_dispatch(
        engine, attempt_id=created.attempt_id, expected_version=1, owner_id="worker-a"
    )
    assert claimed.code == "DISPATCH_GRANTED"
    assert claim_dispatch(
        engine, attempt_id=created.attempt_id, expected_version=1, owner_id="worker-b"
    ).code == "ATTEMPT_VERSION_STALE"
    assert record_result(
        engine, attempt_id=created.attempt_id, expected_version=2, owner_id="worker-a",
        fence=1, digest="a" * 64,
    ).code == "RESULT_RECORDED"
    assert record_result(
        engine, attempt_id=created.attempt_id, expected_version=3, owner_id="worker-a",
        fence=1, digest="b" * 64,
    ).code == "ATTEMPT_RESULT_CONFLICT"
    assert consume_result(engine, attempt_id=created.attempt_id, expected_version=3).code == "APPLIED"
    assert consume_result(engine, attempt_id=created.attempt_id, expected_version=4).code == "APPLIED_REPLAY"

    with session_factory(engine)() as session:
        attempt = session.scalar(select(ProviderAttempt).where(ProviderAttempt.attempt_id == created.attempt_id))
    assert attempt is not None and attempt.result_digest == "a" * 64


def test_cancel_gate_blocks_dispatch_with_real_cas(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    created = create_provider_action(
        engine, feature_id="feature-1", action_key="coding:1",
        input_binding_sha256=EMPTY_SHA256,
        execution_snapshot_sha256=EMPTY_SHA256,
    )
    assert cancel_execution(engine, feature_id="feature-1", expected_gate_version=1).code == "CANCELLED"
    assert cancel_execution(engine, feature_id="feature-1", expected_gate_version=1).code == "EXECUTION_GATE_STALE"
    assert claim_dispatch(
        engine, attempt_id=created.attempt_id, expected_version=1, owner_id="worker-a"
    ).code == "EXECUTION_GATE_CLOSED"
    with session_factory(engine)() as session:
        gate = session.get(ExecutionGate, "feature-1")
    assert gate is not None and (gate.mode, gate.version, gate.approval_epoch) == ("cancelled", 2, 2)
