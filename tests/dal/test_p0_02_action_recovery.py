"""I3: explicit crash schedules, durable read-only recovery, no redispatch."""
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, func

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine import action_recovery as recovery
from personal_agent_dal.machine.action_lifecycle import cancel_execution, claim_dispatch, record_result
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import ProviderAttempt, ProviderRecoveryReceipt
from personal_agent_dal.storage.worker_models import WorkerJob
from tests.dal.test_p0_02_review_regressions import world, create, claim


def expire(engine):
    with session_factory(engine)() as s, s.begin():
        s.get(WorkerJob, 'j').lease_expires_at = utc_now()-timedelta(seconds=1)


def recover(engine, attempt, *, probe=None, command_id='recover-1', version=2):
    return recovery.recover_attempt(engine, attempt_id=attempt.attempt_id,
        expected_version=version, command_id=command_id, requested_by='operator-test', probe=probe)


def test_live_owner_is_not_probed_or_parked(world):
    a = create(world)
    claim(world, a)
    def forbidden(_):
        pytest.fail('live owner must not be probed')
    assert recover(world, a, probe=forbidden).code == 'RECOVERY_NOT_NEEDED'


@pytest.mark.parametrize('was_sent', [False, True])
def test_crash_on_either_side_of_send_is_unknown_and_never_redispatched(world, was_sent):
    a = create(world)
    claim(world, a)
    outbound_count = int(was_sent)
    world.dispose()  # Reopen persistence after the dispatch boundary.
    expire(world)
    result = recover(world, a)
    assert result.code == 'ATTEMPT_UNKNOWN' and result.receipt_id
    assert claim_dispatch(world, attempt_id=a.attempt_id, expected_version=3,
        owner_id='w', job_id='j', lease_id='l').code == 'ATTEMPT_UNKNOWN'
    with session_factory(world)() as s:
        row = s.get(ProviderAttempt, a.attempt_id)
        assert (row.state, row.version, row.owner_id, row.fence) == ('unknown', 3, 'w', 1)
        assert row.dispatch_started_at is not None and row.result_digest is None
    assert outbound_count == int(was_sent)


@pytest.mark.parametrize('status', ['running', 'stopped', 'partial', 'complete'])
def test_probe_evidence_never_implies_success_or_safe_retry(world, status):
    a = create(world)
    claim(world, a)
    expire(world)
    result = recover(world, a, probe=lambda target: recovery.RecoveryReadback(target, status, 'a'*64))
    assert result.code == 'ATTEMPT_UNKNOWN'
    with session_factory(world)() as s:
        receipt = s.get(ProviderRecoveryReceipt, result.receipt_id)
        assert receipt.probe_status == status and receipt.evidence_sha256 == 'a'*64
        assert s.get(ProviderAttempt, a.attempt_id).result_digest is None


def test_receipt_replay_does_not_repeat_probe_and_rejects_changed_binding(world):
    a = create(world)
    claim(world, a)
    expire(world)
    calls = []
    def probe(target):
        calls.append(target)
        return recovery.RecoveryReadback(target, 'stopped', 'a'*64)
    first = recover(world, a, probe=probe)
    second = recover(world, a, probe=probe)
    assert first.receipt_id == second.receipt_id and second.duplicate
    assert len(calls) == 1
    assert recover(world, a, version=3).code == 'IDEMPOTENCY_CONFLICT'


def test_probe_is_outside_transaction_and_cannot_overwrite_late_result(world):
    a = create(world)
    claim(world, a)
    expire(world)
    def probe(target):
        with session_factory(world)() as s, s.begin():
            s.get(WorkerJob, 'j').lease_expires_at = utc_now()+timedelta(hours=1)
        assert record_result(world, attempt_id=a.attempt_id, expected_version=2,
            owner_id='w', fence=1, digest='b'*64).code == 'RESULT_RECORDED'
        return recovery.RecoveryReadback(target, 'partial', 'a'*64)
    result = recover(world, a, probe=probe)
    assert result.code == 'ATTEMPT_VERSION_STALE'
    with session_factory(world)() as s:
        row = s.get(ProviderAttempt, a.attempt_id)
        assert (row.state, row.result_digest) == ('result_recorded', 'b'*64)
        assert s.get(ProviderRecoveryReceipt, result.receipt_id).evidence_sha256 == 'a'*64


@pytest.mark.parametrize('bad', ['exception', 'shape', 'binding', 'digest'])
def test_bad_probes_fail_closed_with_redacted_evidence(world, bad):
    a = create(world)
    claim(world, a)
    expire(world)
    def probe(target):
        if bad == 'exception':
            raise RuntimeError('private-probe-error-canary')
        if bad == 'shape':
            return {'status': 'complete'}
        if bad == 'binding':
            return recovery.RecoveryReadback(replace(target, fence=999), 'stopped', 'a'*64)
        return recovery.RecoveryReadback(target, 'complete', 'not-a-digest')
    result = recover(world, a, probe=probe)
    assert result.code == 'ATTEMPT_UNKNOWN'
    with session_factory(world)() as s:
        receipt = s.get(ProviderRecoveryReceipt, result.receipt_id)
        assert receipt.probe_status == 'unavailable' and receipt.evidence_sha256 is None
        assert receipt.probe_code in ('PROBE_FAILED', 'PROBE_INVALID')


def test_cancel_during_probe_preserves_unknown_and_approval_epoch(world):
    a = create(world)
    claim(world, a)
    expire(world)
    def probe(target):
        assert cancel_execution(world, feature_id='f', expected_gate_version=1).code == 'CANCELLED'
        return recovery.RecoveryReadback(target, 'running', 'a'*64)
    assert recover(world, a, probe=probe).code == 'ATTEMPT_UNKNOWN'
    assert record_result(world, attempt_id=a.attempt_id, expected_version=3,
        owner_id='w', fence=1, digest='c'*64).code == 'EXECUTION_AUTHORIZATION_STALE'


def test_unknown_can_collect_more_evidence_without_new_attempt(world):
    a = create(world)
    claim(world, a)
    expire(world)
    recover(world, a)
    outcome = recover(world, a, command_id='observe-2', version=3,
        probe=lambda target: recovery.RecoveryReadback(target, 'complete', 'd'*64))
    assert outcome.code == 'ATTEMPT_UNKNOWN'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).version == 3
        assert s.scalar(select(func.count()).select_from(ProviderAttempt)) == 1
        assert s.scalar(select(func.count()).select_from(ProviderRecoveryReceipt)) == 2


def post_recovery(engine, a, *, extra=None, role='operator_control', tamper=False, kill_switch=None):
    import hashlib
    import json
    from fastapi.testclient import TestClient
    from personal_agent_dal.service.app import create_app, BODY_DIGEST_HEADER
    from personal_agent_dal.service.operator_tokens import issue_operator_token
    key = b'test-recovery-service-key'
    app = create_app(engine, service_key=key, enrollment_secret=b'test-enroll', kill_switch_path=kill_switch)
    import time
    token = issue_operator_token(key=key, operator_id='review-operator',
        capabilities=['read', 'control'] if role == 'operator_control' else ['read'],
        expires_at_epoch=int(time.time())+600)
    body = json.dumps({'schema_version': 'dal.operator-transport/1.0', 'request_id': 'recover-http-1',
        'attempt_id': a.attempt_id, 'expected_version': 2, **(extra or {})}).encode()
    with TestClient(app) as client:
        return client.post(f'/operator/provider-attempts/{a.attempt_id}/recover', content=body,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                BODY_DIGEST_HEADER: '0'*64 if tamper else hashlib.sha256(body).hexdigest()})


def test_operator_endpoint_composes_recovery_and_replays_receipt(world):
    a = create(world)
    claim(world, a)
    expire(world)
    first = post_recovery(world, a)
    assert first.status_code == 200, first.text
    assert first.json()['code'] == 'ATTEMPT_UNKNOWN'
    assert first.json()['probe_code'] == 'PROBE_NOT_COMPOSED'
    second = post_recovery(world, a)
    assert second.status_code == 200
    assert second.json()['receipt_id'] == first.json()['receipt_id']
    assert second.json()['duplicate'] is True


@pytest.mark.parametrize('mutation', ['extra', 'version_bool', 'digest', 'read_role', 'path_mismatch'])
def test_operator_endpoint_rejects_unbound_requests_without_writes(world, mutation):
    a = create(world)
    claim(world, a)
    expire(world)
    extra = {'probe_status': 'complete'} if mutation == 'extra' else (
        {'expected_version': True} if mutation == 'version_bool' else (
        {'attempt_id': 'different'} if mutation == 'path_mismatch' else None))
    response = post_recovery(world, a, extra=extra, tamper=mutation == 'digest',
        role='operator_read' if mutation == 'read_role' else 'operator_control')
    assert response.status_code in (400, 403), response.text
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).state == 'dispatching'
        assert s.scalar(select(func.count()).select_from(ProviderRecoveryReceipt)) == 0


def test_recovery_receipt_failure_rolls_back_parking(world):
    from sqlalchemy import event
    a = create(world)
    claim(world, a)
    expire(world)
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO provider_recovery_receipts'):
            raise RuntimeError('receipt-storage-failed')
    event.listen(world, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='receipt-storage-failed'):
            recover(world, a)
    finally:
        event.remove(world, 'before_cursor_execute', fail)
    with session_factory(world)() as s:
        row = s.get(ProviderAttempt, a.attempt_id)
        assert (row.state, row.version) == ('dispatching', 2)
    assert recover(world, a).code == 'ATTEMPT_UNKNOWN'


def test_competing_recovery_commands_keep_one_state_transition(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    a = create(world)
    claim(world, a)
    expire(world)
    barrier = Barrier(2)
    def probe(target):
        barrier.wait(timeout=5)
        return recovery.RecoveryReadback(target, 'running', 'a'*64)
    def run(command):
        return recover(world, a, command_id=command, probe=probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, ['first', 'second']))
    assert sorted(o.code for o in outcomes) == ['ATTEMPT_UNKNOWN', 'ATTEMPT_VERSION_STALE']
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).version == 3
        assert s.scalar(select(func.count()).select_from(ProviderRecoveryReceipt)) == 2


def test_renewed_lease_during_probe_prevents_parking(world):
    a = create(world)
    claim(world, a)
    expire(world)
    def probe(target):
        with session_factory(world)() as s, s.begin():
            s.get(WorkerJob, 'j').lease_expires_at = utc_now()+timedelta(hours=1)
        return recovery.RecoveryReadback(target, 'running', 'a'*64)
    assert recover(world, a, probe=probe).code == 'RECOVERY_NOT_NEEDED'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).state == 'dispatching'


def test_kill_switch_blocks_operator_parking(world, tmp_path):
    a = create(world)
    claim(world, a)
    expire(world)
    stop = tmp_path / 'stop'
    stop.write_text('stop')
    assert post_recovery(world, a, kill_switch=stop).status_code == 503
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).state == 'dispatching'
        assert s.scalar(select(func.count()).select_from(ProviderRecoveryReceipt)) == 0


def test_recovery_schema_downgrade_preserves_unknown_attempt(world):
    from personal_agent_dal.storage import db
    from sqlalchemy import inspect
    a = create(world)
    claim(world, a)
    expire(world)
    recover(world, a)
    db.downgrade(world, '0012')
    assert 'provider_recovery_receipts' not in inspect(world).get_table_names()
    # Query only columns present in the deliberately downgraded schema.
    with world.connect() as c:
        assert c.exec_driver_sql('SELECT state FROM provider_attempts WHERE attempt_id=?',
                                 (a.attempt_id,)).scalar_one() == 'unknown'
    db.upgrade(world)
    with world.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('park_first', [False, True])
def test_transport_reclaim_cannot_redispatch_a_provider_job(world, park_first):
    from personal_agent_dal.worker import queue
    a = create(world)
    claim(world, a)
    expire(world)
    if park_first:
        recover(world, a)
    assert queue.reclaim_expired(world, max_attempts=5) == ['j']
    assert queue.claim_job(world, worker_id='replacement', lease_ttl_seconds=60) is None
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).dispatch_started_at is not None


def test_blocked_provider_job_does_not_starve_fresh_jobs(world):
    from personal_agent_dal.worker import queue
    a = create(world)
    claim(world, a)
    expire(world)
    queue.reclaim_expired(world, max_attempts=5)
    fresh = queue.enqueue_job(world, feature_id='f', repository_id='repo-placeholder',
        base_sha='0'*40, branch_name='codex/feature-new', toolchain_ref='test')
    assert queue.claim_job(world, worker_id='replacement', lease_ttl_seconds=60) == fresh
    assert queue.claim_job(world, worker_id='replacement', lease_ttl_seconds=60) is None
