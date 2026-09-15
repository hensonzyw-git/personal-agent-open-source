"""Synthetic signed worker requests against migrated, reopened FK-on SQLite."""
from datetime import timedelta
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, text

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.resume_dispatch import prelaunch_context
from personal_agent_dal.service.app import create_app
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.worker_models import WorkerJob
from tests.dal.test_p0_02_review_regressions import world
from tests.integration.test_dal_supervised_dispatch import composed


def snapshot(engine):
    with engine.connect() as c:
        assert c.scalar(text('PRAGMA foreign_keys')) == 1
        assert c.execute(text('PRAGMA foreign_key_check')).all() == []
        return {name: sorted(map(repr, c.execute(text(f'SELECT * FROM "{name}"')).all()))
                for name in inspect(c).get_table_names()}


def client_for(world, transport, enabled):
    if enabled:
        return transport._client
    return TestClient(create_app(world, service_key=b'synthetic-service',
        enrollment_secret=b'synthetic-enroll', resume_config=None))


def request(client, transport, job_id, epoch):
    body = json.dumps({'job_lease_epoch': epoch}).encode()
    return client.post(f'/worker/jobs/{job_id}/prelaunch-context',
        content=body,
        headers={'Authorization': 'Bearer ' + transport._token.token,
                 'Content-Type': 'application/json',
                 'X-Transport-Body-Digest': hashlib.sha256(body).hexdigest()})


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('episode', [True, False])
@pytest.mark.parametrize('invalid', [
    'foreign', 'missing', 'stale', 'expired', 'expiry_equal',
    'pending', 'succeeded', 'failed', 'expired_state', 'cancelled',
])
def test_invalid_authority_has_no_episode_oracle(composed, world, monkeypatch,
                                               enabled, episode, invalid):
    transport, lease, *_ = composed
    job_id, epoch = (lease.job_id, lease.lease_epoch) if episode else ('j', 3)
    now = utc_now()
    monkeypatch.setattr('personal_agent_dal.machine.resume_dispatch.utc_now', lambda: now)
    with session_factory(world)() as s, s.begin():
        job = s.get(WorkerJob, job_id)
        if invalid == 'foreign':
            job.worker_id = 'synthetic-other-worker'
        elif invalid == 'missing':
            job_id = 'synthetic-missing-job'
        elif invalid == 'stale':
            epoch += 1
        elif invalid in ('expired', 'expiry_equal'):
            job.lease_expires_at = now - timedelta(seconds=invalid == 'expired')
        else:
            job.state = 'expired' if invalid == 'expired_state' else invalid
            job.worker_id = job.lease_expires_at = job.heartbeat_at = None
    client = client_for(world, transport, enabled)
    world.dispose()
    before = snapshot(world)
    statements = []
    def observe(conn, cursor, statement, *args):
        statements.append(statement.lower())
    event.listen(world, 'before_cursor_execute', observe)
    try:
        response = request(client, transport, job_id, epoch)
        assert (response.status_code, response.json()) == (409, {
            'schema_version': 'dal.worker-transport/1.0',
            'code': 'JOB_LEASE_STALE', 'detail': 'JOB_LEASE_STALE'})
        with pytest.raises(ValueError, match='^JOB_LEASE_STALE$'):
            prelaunch_context(world, job_id=job_id, worker_id='w',
                              job_lease_epoch=epoch, enabled=enabled)
    finally:
        event.remove(world, 'before_cursor_execute', observe)
    assert not any('resume_episodes' in sql for sql in statements)
    assert not any(sql.lstrip().startswith(('insert ', 'update ', 'delete ')) for sql in statements)
    assert snapshot(world) == before


@pytest.mark.parametrize('enabled', [True, False])
def test_valid_plain_job_retains_legacy_context(composed, world, enabled):
    transport, *_ = composed
    # Only explicitly classified non-provider producers retain context=None.
    from personal_agent_dal.storage.worker_models import WorkerJob
    from personal_agent_dal.storage.engine import session_factory
    with session_factory(world)() as s, s.begin():
        s.get(WorkerJob, 'j').execution_mode = 'legacy_non_provider'
    client = client_for(world, transport, enabled)
    before = snapshot(world)
    response = request(client, transport, 'j', 3)
    assert (response.status_code, response.json()) == (200, {'context': None})
    assert prelaunch_context(world, job_id='j', worker_id='w',
                             job_lease_epoch=3, enabled=enabled) is None
    assert snapshot(world) == before


@pytest.mark.parametrize('enabled', [True, False])
def test_valid_episode_context_never_dispatches(composed, world, monkeypatch, enabled):
    transport, lease, *_ = composed
    def forbidden(*args, **kwargs):
        pytest.fail('context retrieval attempted dispatch or legacy execution')
    monkeypatch.setattr('personal_agent_dal.machine.action_lifecycle.claim_dispatch', forbidden)
    monkeypatch.setattr('subprocess.Popen', forbidden)
    client = client_for(world, transport, enabled)
    before = snapshot(world)
    response = request(client, transport, lease.job_id, lease.lease_epoch)
    if not enabled:
        assert (response.status_code, response.json()) == (503, {
            'schema_version': 'dal.worker-transport/1.0',
            'code': 'DAL_RESUME_UNAVAILABLE', 'detail': 'DAL_RESUME_UNAVAILABLE'})
        with pytest.raises(ValueError, match='^DAL_RESUME_UNAVAILABLE$'):
            prelaunch_context(world, job_id=lease.job_id, worker_id='w',
                              job_lease_epoch=lease.lease_epoch, enabled=False)
        assert snapshot(world) == before
    else:
        assert response.status_code == 200
        context = response.json()['context']
        assert context['job_id'] == lease.job_id
        assert context['job_lease_epoch'] == lease.lease_epoch
        assert prelaunch_context(world, job_id=lease.job_id, worker_id='w',
                                 job_lease_epoch=lease.lease_epoch) == context
        after = snapshot(world)
        assert {name for name in before if before[name] != after[name]} == {
            'leases', 'resume_lease_issuances'}


def test_cached_job_cannot_hide_committed_reclaim(composed, world):
    from concurrent.futures import ThreadPoolExecutor
    from personal_agent_dal.machine.resume_dispatch import _context
    from personal_agent_dal.worker.queue import claim_job, reclaim_expired
    _, lease, *_ = composed
    with session_factory(world)(expire_on_commit=False) as session:
        cached = session.get(WorkerJob, lease.job_id)
        expiry = cached.lease_expires_at
        original_epoch = cached.lease_epoch
        session.commit()  # Release the snapshot but deliberately retain the identity map.
        def reclaim():
            now = expiry + timedelta(seconds=1)
            assert lease.job_id in reclaim_expired(world, max_attempts=3, now=now)
            assert claim_job(world, worker_id='w', lease_ttl_seconds=60, now=now) == lease.job_id
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(reclaim).result()
        assert cached.lease_epoch == original_epoch
        with pytest.raises(ValueError, match='^JOB_LEASE_STALE$'):
            _context(session, job_id=lease.job_id, worker_id='w',
                     job_lease_epoch=original_epoch, enabled=False)
        assert cached.lease_epoch == original_epoch + 1
