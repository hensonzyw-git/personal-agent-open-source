"""Review regressions: real FK connections, stale authority and crash atomicity."""
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, func, update

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import (
    create_provider_action, claim_dispatch, record_result, consume_result, cancel_execution,
)
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory
from personal_agent_dal.storage.machine_models import ExecutionGate, ProviderAttempt, Lease
from personal_agent_dal.storage.worker_models import WorkerJob
from tests.dal.factories import feature_row, EMPTY_SHA256
from tests.dal.p0_02_reference_model import ActionWorld


@pytest.fixture
def world(tmp_path: Path):
    path = tmp_path / 'review.db'
    engine = create_database_engine(path)
    db.upgrade(engine)
    engine.dispose()
    engine = create_database_engine(path)
    now = utc_now()
    with session_factory(engine)() as s, s.begin():
        s.add(feature_row(feature_id='f', version=1, state='coding', now=now))
        s.flush()
        s.add(WorkerJob(job_id='j', feature_id='f', repository_id='repo-placeholder',
            base_sha='0'*40, branch_name='review', toolchain_ref='test', state='running',
            attempt_count=1, lease_epoch=3, worker_id='w', lease_expires_at=now+timedelta(hours=1),
            heartbeat_at=now, created_at=now, updated_at=now))
        s.add(Lease(lease_id='l', feature_id='f', job_id='j', worker_id='w', epoch=4,
            expires_at=now+timedelta(hours=1), created_at=now))
    yield engine
    engine.dispose()


def create(engine):
    return create_provider_action(engine, feature_id='f', action_key='coding:1',
        input_binding_sha256=EMPTY_SHA256, execution_snapshot_sha256=EMPTY_SHA256)


def claim(engine, a):
    return claim_dispatch(engine, attempt_id=a.attempt_id, expected_version=1,
        owner_id='w', job_id='j', lease_id='l')


def test_reopened_database_can_create(world):
    a = create(world)
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id) is not None


@pytest.mark.parametrize('authority', ['approval', 'job', 'policy', 'expired', 'revoked'])
def test_invalidated_authority_refuses_result(world, authority):
    a = create(world)
    assert claim(world, a).code == 'DISPATCH_GRANTED'
    with session_factory(world)() as s, s.begin():
        if authority == 'approval':
            s.get(ExecutionGate, 'f').approval_epoch += 1
        elif authority == 'job':
            s.get(WorkerJob, 'j').lease_epoch += 1
        elif authority == 'policy':
            s.get(Lease, 'l').epoch += 1
        elif authority == 'expired':
            s.get(Lease, 'l').expires_at = utc_now()-timedelta(seconds=1)
        else:
            s.get(Lease, 'l').revoked_at = utc_now()
    result = record_result(world, attempt_id=a.attempt_id, expected_version=2,
        owner_id='w', fence=1, digest='a'*64)
    assert result.code != 'RESULT_RECORDED'


def test_cancelled_late_result_keeps_observation(world):
    from personal_agent_dal.storage.machine_models import ProviderResultObservation
    a = create(world)
    claim(world, a)
    cancel_execution(world, feature_id='f', expected_gate_version=1)
    result = record_result(world, attempt_id=a.attempt_id, expected_version=2,
        owner_id='w', fence=1, digest='a'*64)
    assert result.code == 'EXECUTION_AUTHORIZATION_STALE'
    with session_factory(world)() as s:
        obs = s.scalar(select(ProviderResultObservation))
        assert (obs.attempt_id, obs.digest, obs.code) == (a.attempt_id, 'a'*64, result.code)
        assert s.get(ProviderAttempt, a.attempt_id).result_digest is None


def test_reference_resume_does_not_revive_prepared():
    w = ActionWorld.new(action_id='a', policy_lease_epoch=1, job_lease_epoch=1)
    w.cancel(expected_gate_version=1)
    w.resume(approval_epoch=2)
    assert w.claim_dispatch(owner_id='old', send=True).code != 'DISPATCH_GRANTED'


def test_consume_requires_business_transition(world):
    a = create(world)
    claim(world, a)
    record_result(world, attempt_id=a.attempt_id, expected_version=2,
        owner_id='w', fence=1, digest='a'*64)
    assert consume_result(world, attempt_id=a.attempt_id, expected_version=3).code == 'TRANSITION_REQUIRED'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).result_consumed_at is None


def provider_command():
    from personal_agent_dal.machine.engine import TransitionCommand
    return TransitionCommand(aggregate_type='feature', aggregate_id='f',
        command_type='record_provider_result', command_parameters={'target_state': 'verifying'},
        actor_type='service', evidence_source_types=('worker',),
        evidence_schema_versions=('dal.evidence.patch/1.0',), decision_action=None,
        reason_code=None, expected_version=1, idempotency_key='ignored-by-consumer')


def test_consume_transition_and_receipt_are_atomic_and_replayable(world, monkeypatch):
    from personal_agent_dal.storage.models import Feature
    from personal_agent_dal.storage.machine_models import TransitionReceipt
    import personal_agent_dal.machine.action_lifecycle as lifecycle
    a = create(world)
    claim(world, a)
    record_result(world, attempt_id=a.attempt_id, expected_version=2,
                  owner_id='w', fence=1, digest='a'*64)
    real_apply = lifecycle.apply_transition
    def crash_after_transition(*args, **kwargs):
        result = real_apply(*args, **kwargs)
        assert result.receipt_code == 'APPLIED'
        raise RuntimeError('crash after transition before consumption')
    monkeypatch.setattr(lifecycle, 'apply_transition', crash_after_transition)
    with pytest.raises(RuntimeError, match='crash after transition'):
        consume_result(world, attempt_id=a.attempt_id, expected_version=3, command=provider_command())
    with session_factory(world)() as s:
        assert s.get(Feature, 'f').version == 1
        assert s.get(ProviderAttempt, a.attempt_id).result_consumed_at is None
        assert s.scalar(select(func.count()).select_from(TransitionReceipt)) == 0
    monkeypatch.setattr(lifecycle, 'apply_transition', real_apply)
    first = consume_result(world, attempt_id=a.attempt_id, expected_version=3, command=provider_command())
    replay = consume_result(world, attempt_id=a.attempt_id, expected_version=3, command=provider_command())
    assert (first.code, replay.code) == ('APPLIED', 'APPLIED_REPLAY')
    assert first.receipt_id == replay.receipt_id and first.receipt_id
    with session_factory(world)() as s:
        assert s.get(Feature, 'f').state == 'verifying'
        assert s.get(Feature, 'f').version == 2
        assert s.get(ProviderAttempt, a.attempt_id).consumption_receipt_id == first.receipt_id
        assert s.scalar(select(func.count()).select_from(TransitionReceipt)) == 1


@pytest.mark.parametrize('drift', ['active', 'feature', 'capability', 'gate', 'job_owner', 'lease_root'])
def test_bound_authority_rechecked_before_dispatch(world, drift):
    from personal_agent_dal.storage.models import Feature
    from personal_agent_dal.storage.machine_models import WorkflowAction
    a = create(world)
    with session_factory(world)() as s, s.begin():
        if drift == 'active':
            s.get(WorkflowAction, a.action_id).active_attempt_id = None
        elif drift == 'feature':
            s.get(Feature, 'f').version += 1
        elif drift == 'capability':
            s.get(Feature, 'f').capability_epoch += 1
        elif drift == 'gate':
            s.get(ExecutionGate, 'f').approval_epoch += 1
        elif drift == 'job_owner':
            s.get(WorkerJob, 'j').worker_id = 'other'
        else:
            s.get(Lease, 'l').feature_id = 'other'
    assert claim(world, a).code != 'DISPATCH_GRANTED'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).dispatch_started_at is None


def test_conflict_and_stale_arrivals_preserve_first_digest(world):
    from personal_agent_dal.storage.machine_models import ProviderResultObservation
    a = create(world)
    claim(world, a)
    def record(version, digest):
        return record_result(world, attempt_id=a.attempt_id, expected_version=version,
            owner_id='w', fence=1, digest=digest)
    assert record(2, 'a'*64).code == 'RESULT_RECORDED'
    assert record(3, 'b'*64).code == 'ATTEMPT_RESULT_CONFLICT'
    assert record(2, 'b'*64).code == 'ATTEMPT_VERSION_STALE'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).result_digest == 'a'*64
        assert s.scalar(select(func.count()).select_from(ProviderResultObservation)) == 3


def test_authority_invalidated_between_record_and_consume(world):
    a = create(world)
    claim(world, a)
    record_result(world, attempt_id=a.attempt_id, expected_version=2, owner_id='w', fence=1, digest='a'*64)
    with session_factory(world)() as s, s.begin():
        s.get(ExecutionGate, 'f').approval_epoch += 1
    assert consume_result(world, attempt_id=a.attempt_id, expected_version=3,
                          command=provider_command()).code == 'EXECUTION_AUTHORIZATION_STALE'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).result_consumed_at is None


def test_real_concurrent_dispatch_has_one_winner(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    a = create(world)
    barrier = Barrier(2)
    def run(_):
        barrier.wait()
        return claim(world, a).code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(results) == ['ATTEMPT_VERSION_STALE', 'DISPATCH_GRANTED']


def test_concurrent_consumption_returns_one_receipt(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    a = create(world)
    claim(world, a)
    record_result(world, attempt_id=a.attempt_id, expected_version=2, owner_id='w', fence=1, digest='a'*64)
    barrier = Barrier(2)
    def run(_):
        barrier.wait()
        return consume_result(world, attempt_id=a.attempt_id, expected_version=3, command=provider_command())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(r.code for r in results) == ['APPLIED', 'APPLIED_REPLAY']
    assert results[0].receipt_id == results[1].receipt_id


def test_failed_migration_rolls_back_and_restores_fk(tmp_path):
    from sqlalchemy import event, inspect
    engine = create_database_engine(tmp_path / 'migration.db')
    db.upgrade(engine, '0011')
    def fail(conn, cursor, statement, parameters, context, executemany):
        if 'CREATE TABLE provider_result_observations' in statement:
            raise RuntimeError('injected migration failure')
    event.listen(engine, 'before_cursor_execute', fail)
    with pytest.raises(RuntimeError, match='injected migration failure'):
        db.upgrade(engine)
    event.remove(engine, 'before_cursor_execute', fail)
    with engine.connect() as conn:
        assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        assert conn.exec_driver_sql('SELECT version_num FROM alembic_version').scalar() == '0011'
    assert 'feature_version' not in {c['name'] for c in inspect(engine).get_columns('provider_attempts')}
    db.upgrade(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
    engine.dispose()


def test_legacy_unbound_attempt_is_not_implicitly_authorized(world):
    a = create(world)
    with session_factory(world)() as s, s.begin():
        s.get(ProviderAttempt, a.attempt_id).feature_version = None
    assert claim(world, a).code == 'EXECUTION_AUTHORIZATION_STALE'


def test_illegal_transition_does_not_consume(world):
    from dataclasses import replace
    a = create(world)
    claim(world, a)
    record_result(world, attempt_id=a.attempt_id, expected_version=2, owner_id='w', fence=1, digest='a'*64)
    assert consume_result(world, attempt_id=a.attempt_id, expected_version=3,
        command=replace(provider_command(), command_type='nonexistent')).code == 'ILLEGAL_TRANSITION'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).result_consumed_at is None


def test_reference_resume_cannot_consume_old_result():
    w = ActionWorld.new(action_id='a', policy_lease_epoch=1, job_lease_epoch=1)
    w.claim_dispatch(owner_id='w', send=True)
    w.record_result(owner_id='w', fence=1, digest='a', job_lease_epoch=1, policy_lease_epoch=1)
    w.cancel(expected_gate_version=1)
    w.resume(approval_epoch=2)
    assert w.consume_result().code == 'EXECUTION_AUTHORIZATION_STALE'


def test_consume_replay_rejects_changed_command(world):
    from dataclasses import replace
    a = create(world)
    claim(world, a)
    record_result(world, attempt_id=a.attempt_id, expected_version=2, owner_id='w', fence=1, digest='a'*64)
    assert consume_result(world, attempt_id=a.attempt_id, expected_version=3,
                          command=provider_command()).code == 'APPLIED'
    assert consume_result(world, attempt_id=a.attempt_id, expected_version=3,
        command=replace(provider_command(), command_type='pause_feature')).code == 'IDEMPOTENCY_CONFLICT'


def test_cancel_racing_claim_never_reopens_gate(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    a = create(world)
    barrier = Barrier(2)
    def run(operation):
        barrier.wait()
        return claim(world, a) if operation == 'claim' else cancel_execution(
            world, feature_id='f', expected_gate_version=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch, cancel = list(pool.map(run, ['claim', 'cancel']))
    assert cancel.code == 'CANCELLED'
    assert dispatch.code in ('DISPATCH_GRANTED', 'EXECUTION_AUTHORIZATION_STALE')
    with session_factory(world)() as s:
        assert s.get(ExecutionGate, 'f').mode == 'cancelled'
        row = s.get(ProviderAttempt, a.attempt_id)
        assert row.state == ('dispatching' if dispatch.code == 'DISPATCH_GRANTED' else 'prepared')


def test_upgrade_preserves_legacy_attempt_without_inventing_authority(tmp_path):
    from sqlalchemy import text
    from personal_agent_core.timeutil import to_rfc3339
    path = tmp_path / 'legacy.db'
    e = create_database_engine(path)
    db.upgrade(e, '0011')
    now = to_rfc3339(utc_now())
    with session_factory(e)() as s, s.begin():
        s.add(feature_row(feature_id='f', version=1, state='coding'))
    with e.begin() as c:
        c.execute(text("INSERT INTO execution_gates VALUES ('f',1,'open',1,:now,:now)"), {'now': now})
        c.execute(text("INSERT INTO workflow_actions VALUES ('a','f',NULL,'provider','coding:1',:sha,:sha,1,'old',:now,:now)"), {'sha': EMPTY_SHA256, 'now': now})
        c.execute(text("INSERT INTO provider_attempts (attempt_id,action_id,attempt_no,state,version,fence,created_at,updated_at) VALUES ('old','a',1,'prepared',1,0,:now,:now)"), {'now': now})
    db.upgrade(e)
    with session_factory(e)() as s:
        row = s.get(ProviderAttempt, 'old')
        assert (row.feature_version, row.capability_epoch, row.approval_epoch) == (None, None, None)
    assert claim_dispatch(e, attempt_id='old', expected_version=1, owner_id='w').code == 'EXECUTION_AUTHORIZATION_STALE'
    db.downgrade(e, '0011')
    with e.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
        assert c.exec_driver_sql('SELECT attempt_id FROM provider_attempts').scalar() == 'old'
    e.dispose()
