"""Stop controls must atomically retire authority without inventing termination."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta

import pytest
from sqlalchemy import event, select, func

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import cancel_execution, pause_execution
from personal_agent_dal.machine.execution_control import control_execution
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import Capability, ExecutionGate, ExecutionControlReceipt, ProviderAttempt
from tests.dal.test_p0_02_review_regressions import world, create, claim


def capability(engine):
    now = utc_now()
    with session_factory(engine)() as s, s.begin():
        s.add(Capability(capability_id='cap', approval_id=None, feature_id='f',
            action='provider', scope='test', epoch=1, expires_at=now+timedelta(hours=1),
            max_uses=2, uses_consumed=1, revoked_at=None, created_at=now))


def stop(engine, *, operation='pause', version=1, command='stop-1', actor='operator'):
    return control_execution(engine, feature_id='f', operation=operation,
        expected_gate_version=version, command_id=command, requested_by=actor)


def test_pause_blocks_prepared_and_cancel_accepts_paused(world):
    a = create(world)
    capability(world)
    assert pause_execution(world, feature_id='f', expected_gate_version=1).code == 'PAUSED'
    assert claim(world, a).code == 'EXECUTION_AUTHORIZATION_STALE'
    assert cancel_execution(world, feature_id='f', expected_gate_version=2).code == 'CANCELLED'
    with session_factory(world)() as s:
        gate = s.get(ExecutionGate, 'f')
        assert (gate.mode, gate.version, gate.approval_epoch) == ('cancelled', 3, 3)
        assert s.get(Capability, 'cap').revoked_at is not None
        assert s.get(Capability, 'cap').uses_consumed == 1
        assert s.get(ProviderAttempt, a.attempt_id).state == 'prepared'


def test_control_replay_binds_actor_operation_and_version(world):
    create(world)
    first = stop(world)
    assert first.code == 'PAUSED'
    assert first.receipt_id
    replay = stop(world)
    assert replay.duplicate and replay.receipt_id == first.receipt_id
    for change in ({'actor': 'another'}, {'operation': 'cancel'}, {'version': 2}):
        assert stop(world, **change).code == 'IDEMPOTENCY_CONFLICT'
    assert stop(world, command='new').code == 'EXECUTION_GATE_STALE'


def test_receipt_failure_rolls_back_gate_and_capabilities(world):
    create(world)
    capability(world)
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO execution_control_receipts'):
            raise RuntimeError('receipt-storage-failed')
    event.listen(world, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='receipt-storage-failed'):
            stop(world)
    finally:
        event.remove(world, 'before_cursor_execute', fail)
    with session_factory(world)() as s:
        assert s.get(ExecutionGate, 'f').mode == 'open'
        assert s.get(Capability, 'cap').revoked_at is None
    assert stop(world).code == 'PAUSED'


def test_pause_cancel_race_has_one_winner(world):
    a = create(world)
    claim(world, a)
    barrier = Barrier(2)
    def run(operation):
        barrier.wait(timeout=5)
        return stop(world, operation=operation, command=operation).code
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(run, ['pause', 'cancel']))
    assert codes.count('EXECUTION_GATE_STALE') == 1
    assert len(set(codes) & {'PAUSED', 'CANCELLED'}) == 1
    with session_factory(world)() as s:
        assert s.get(ExecutionGate, 'f').version == 2
        assert s.get(ProviderAttempt, a.attempt_id).state == 'dispatching'
        assert s.scalar(select(func.count()).select_from(ExecutionControlReceipt)) == 1


@pytest.mark.parametrize('mode', ['cancelled', 'delivered'])
def test_terminal_gate_cannot_be_paused(world, mode):
    create(world)
    with session_factory(world)() as s, s.begin():
        s.get(ExecutionGate, 'f').mode = mode
    assert stop(world).code == 'EXECUTION_GATE_STALE'


@pytest.mark.parametrize('operation', ['resume', 'replacement', 'open'])
def test_stop_surface_cannot_grant_authority(world, operation):
    create(world)
    with pytest.raises(ValueError):
        stop(world, operation=operation)


def post_control(engine, *, extra=None, tamper=False, read_only=False, kill_switch=None):
    import hashlib
    import json
    import time
    from fastapi.testclient import TestClient
    from personal_agent_dal.service.app import create_app, BODY_DIGEST_HEADER
    from personal_agent_dal.service.operator_tokens import issue_operator_token
    key = b'test-control-service-key'
    app = create_app(engine, service_key=key, enrollment_secret=b'test-enroll', kill_switch_path=kill_switch)
    token = issue_operator_token(key=key, operator_id='control-operator',
        capabilities=['read'] if read_only else ['read', 'control'],
        expires_at_epoch=int(time.time())+600)
    body = json.dumps({'schema_version': 'dal.operator-transport/1.0', 'request_id': 'control-http-1',
        'feature_id': 'f', 'operation': 'pause', 'expected_gate_version': 1, **(extra or {})}).encode()
    with TestClient(app) as client:
        return client.post('/operator/features/f/execution-control', content=body,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                BODY_DIGEST_HEADER: '0'*64 if tamper else hashlib.sha256(body).hexdigest()})


def test_control_endpoint_persists_and_replays_even_under_kill_switch(world, tmp_path):
    create(world)
    switch = tmp_path / 'STOP'
    switch.touch()
    first = post_control(world, kill_switch=switch)
    assert first.status_code == 200, first.text
    assert first.json()['code'] == 'PAUSED'
    replay = post_control(world, kill_switch=switch)
    assert replay.status_code == 200
    assert replay.json()['receipt_id'] == first.json()['receipt_id']
    assert replay.json()['duplicate']
    assert replay.json()['gate_version'] == 2
    with session_factory(world)() as s:
        receipt = s.scalar(select(ExecutionControlReceipt))
        assert receipt.requested_by == 'control-operator'


@pytest.mark.parametrize('mutation', ['extra', 'bool', 'digest', 'read_role', 'path', 'resume'])
def test_control_endpoint_refuses_invalid_requests_without_writes(world, mutation):
    create(world)
    extras = {'extra': {'approved': True}, 'bool': {'expected_gate_version': True},
        'path': {'feature_id': 'other'}, 'resume': {'operation': 'resume'}}
    response = post_control(world, extra=extras.get(mutation),
        tamper=mutation == 'digest', read_only=mutation == 'read_role')
    assert response.status_code in (400, 403), response.text
    with session_factory(world)() as s:
        assert s.get(ExecutionGate, 'f').mode == 'open'
        assert s.scalar(select(func.count()).select_from(ExecutionControlReceipt)) == 0


def test_control_endpoint_distinguishes_missing_and_stale(world):
    assert post_control(world).status_code == 404
    create(world)
    assert post_control(world, extra={'expected_gate_version': 2}).status_code == 409


def test_pause_retains_late_result_as_observation_only(world):
    from personal_agent_dal.machine.action_lifecycle import record_result
    from personal_agent_dal.storage.machine_models import ProviderResultObservation
    a = create(world)
    claim(world, a)
    stop(world)
    outcome = record_result(world, attempt_id=a.attempt_id, expected_version=2,
        owner_id='w', fence=1, digest='a'*64)
    assert outcome.code == 'EXECUTION_AUTHORIZATION_STALE'
    with session_factory(world)() as s:
        assert s.scalar(select(ProviderResultObservation)).digest == 'a'*64
        row = s.get(ProviderAttempt, a.attempt_id)
        assert row.state == 'dispatching' and row.result_digest is None
        assert row.owner_id == 'w' and row.fence == 1


def test_pause_claim_race_never_leaves_authorized_old_attempt(world):
    a = create(world)
    barrier = Barrier(2)
    def dispatch():
        barrier.wait(timeout=5)
        return claim(world, a).code
    def pause():
        barrier.wait(timeout=5)
        return stop(world).code
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatched = pool.submit(dispatch)
        paused = pool.submit(pause)
        assert paused.result() == 'PAUSED'
        assert dispatched.result() in ('DISPATCH_GRANTED', 'EXECUTION_AUTHORIZATION_STALE')
    with session_factory(world)() as s:
        gate = s.get(ExecutionGate, 'f')
        row = s.get(ProviderAttempt, a.attempt_id)
        assert gate.approval_epoch > row.approval_epoch
        assert row.state in ('prepared', 'dispatching')


def test_downgrade_preserves_stop_and_revocation(world):
    from personal_agent_dal.storage import db
    from sqlalchemy import inspect
    create(world)
    capability(world)
    stop(world)
    db.downgrade(world, '0013')
    assert 'execution_control_receipts' not in inspect(world).get_table_names()
    with session_factory(world)() as s:
        assert s.get(ExecutionGate, 'f').mode == 'paused'
        assert s.get(Capability, 'cap').revoked_at is not None
    db.upgrade(world)
    assert stop(world).code == 'EXECUTION_GATE_STALE'
