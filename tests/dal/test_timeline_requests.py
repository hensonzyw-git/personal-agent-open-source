"""Timeline request boundary: real SQLite, encrypted bodies, restart and races."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.timeline.requests import RequestService, TimelineRefusal


@pytest.fixture
def world(tmp_path):
    engine = create_database_engine(tmp_path / 'timeline.db')
    db.upgrade(engine)
    keyring = KeyRing([generate_key('synthetic-key')], service='dal')
    service = RequestService(engine, keyring=keyring, cursor_key=b'x' * 32)
    yield engine, keyring, service
    engine.dispose()


def submit(service, number=1, **overrides):
    return service.submit(**dict(dict(command_id=f'cmd-{number}', subject='device:synthetic',
        source_message_ref=f'message-{number}', body=f'Synthetic request {number}'), **overrides))


def test_plaintext_never_reaches_legacy_intake_or_event_storage(world):
    engine, keyring, service = world
    result = submit(service, body='SYNTHETIC PRIVATE REQUEST 123')
    assert result['status'] == 'accepted_not_started'
    assert result['feature_id'] is None
    with engine.connect() as c:
        for table in ['features', 'worker_jobs', 'feature_intake_requests']:
            assert c.execute(text(f'SELECT count(*) FROM {table}')).scalar() == 0
        for table in ['development_requests', 'development_request_revisions', 'development_events', 'development_commands']:
            rows = c.execute(text(f'SELECT * FROM {table}')).all()
            assert 'SYNTHETIC PRIVATE' not in repr(rows)
    assert service.detail(result['request_id'])['text'] == 'SYNTHETIC PRIVATE REQUEST 123'


def test_same_source_and_command_replay_but_changed_text_refused(world):
    engine, _, service = world
    first = submit(service)
    assert submit(service) == first
    assert submit(service, command_id='different-transport-id') == first
    with pytest.raises(TimelineRefusal, match='IDEMPOTENCY_CONFLICT'):
        submit(service, body='different')
    with pytest.raises(TimelineRefusal, match='IDEMPOTENCY_CONFLICT'):
        submit(service, source_message_ref='different-message')


def test_concurrent_submit_converges_on_one_request_and_event(world):
    engine, _, service = world
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(service), range(8)))
    assert all(r == results[0] for r in results)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM development_events')).scalar() == 1
        assert c.execute(text('SELECT count(*) FROM development_requests')).scalar() == 1


def test_failed_event_append_rolls_back_entire_command(world, monkeypatch):
    engine, _, service = world
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(service, '_append_event', fail)
    with pytest.raises(RuntimeError):
        submit(service)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM development_requests')).scalar() == 0
        assert c.execute(text('SELECT count(*) FROM development_commands')).scalar() == 0


def test_snapshot_is_complete_and_stable_across_new_requests_and_restart(world):
    engine, ring, service = world
    for n in range(4): submit(service, n)
    page = service.list_tasks(subject='device:synthetic', limit=2)
    assert page['total'] == 4 and not page['complete']
    submit(service, 5)
    restarted = RequestService(engine, keyring=ring, cursor_key=b'x' * 32)
    next_page = restarted.list_tasks(subject='device:synthetic', limit=2, cursor=page['next_cursor'])
    assert next_page['total'] == 4 and next_page['complete']
    assert len({r['request_id'] for r in page['items'] + next_page['items']}) == 4
    assert restarted.list_tasks(subject='device:synthetic')['total'] == 5


def test_cursor_binds_identity_filter_and_expiry(world):
    engine, ring, service = world
    for n in range(2): submit(service, n)
    page = service.list_tasks(subject='device:synthetic', limit=1)
    for kwargs in [dict(subject='device:other'), dict(subject='device:synthetic', view='all'),
                   dict(subject='device:synthetic', cursor=page['next_cursor'] + 'x')]:
        with pytest.raises(TimelineRefusal):
            service.list_tasks(**dict(dict(cursor=page['next_cursor']), **kwargs))
    future = RequestService(engine, keyring=ring, cursor_key=b'x' * 32, now=lambda: utc_now()+timedelta(minutes=16))
    with pytest.raises(TimelineRefusal, match='CURSOR_EXPIRED'):
        future.list_tasks(subject='device:synthetic', cursor=page['next_cursor'])


def test_missing_keyring_and_tampered_envelope_fail_closed(world):
    engine, ring, service = world
    with pytest.raises(TimelineRefusal, match='INPUT_DECRYPT_UNAVAILABLE'):
        RequestService(engine, keyring=None, cursor_key=b'x'*32)
    result = submit(service)
    other = RequestService(engine, keyring=KeyRing([generate_key('wrong')], service='dal'), cursor_key=b'x'*32)
    with pytest.raises(TimelineRefusal, match='INPUT_INTEGRITY_FAILED'):
        other.detail(result['request_id'])


@pytest.mark.parametrize('body', ['', '   ', 'x'*32769, None])
def test_empty_oversize_or_nontext_rejected(world, body):
    with pytest.raises(TimelineRefusal, match='INVALID_ARGUMENT'):
        submit(world[2], body=body)


def test_migration_roundtrip_preserves_legacy_schema(world):
    from sqlalchemy import inspect
    engine, _, _ = world
    db.downgrade(engine, '0018')
    assert 'development_requests' not in inspect(engine).get_table_names()
    assert 'features' in inspect(engine).get_table_names()
    db.upgrade(engine)
    assert 'development_requests' in inspect(engine).get_table_names()


def test_long_summary_is_explicit_and_detail_remains_complete(world):
    _,_,service=world
    body='Synthetic long content ' * 100
    result=submit(service,body=body)
    page=service.list_tasks(subject='device:synthetic')
    assert page['items'][0]['summary_truncated'] is True
    assert len(page['items'][0]['summary'])==120
    assert service.detail(result['request_id'])['text']==body


def test_completed_workflow_does_not_reappear_as_legacy_intake(world):
    from personal_agent_dal.storage.engine import session_factory
    from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow
    from tests.dal.factories import feature_row
    engine,ring,service=world
    result=submit(service)
    with session_factory(engine)() as s,s.begin():
        s.add(feature_row(feature_id='legacy-linked',version=1,state='intake'))
        s.flush()
        row=s.get(DevelopmentWorkflow,result['request_id'])
        row.feature_id='legacy-linked'
        row.phase='accepted'
        row.status='completed'
    reader=RequestService(engine,keyring=ring,cursor_key=b'x'*32,allowed_repository_ids=['repo-placeholder'])
    assert reader.list_tasks(subject='device:synthetic')['total']==0
    assert reader.list_tasks(subject='device:synthetic',view='all')['total']==1


def test_cursor_is_invalidated_when_repository_policy_changes(world):
    engine, ring, service = world
    for n in range(2):
        submit(service, n)
    page = service.list_tasks(subject='device:synthetic', limit=1)
    changed = RequestService(engine, keyring=ring, cursor_key=b'x'*32,
                             allowed_repository_ids=['new-repository'])
    with pytest.raises(TimelineRefusal, match='CURSOR_INVALID'):
        changed.list_tasks(subject='device:synthetic', cursor=page['next_cursor'])


def test_detail_uses_workflow_authority(world):
    from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow
    _, _, service = world
    result = submit(service)
    with service.sessions() as s, s.begin():
        row = s.get(DevelopmentWorkflow, result['request_id'])
        row.phase, row.status, row.version = 'accepted', 'completed', 3
    detail = service.detail(result['request_id'])
    assert (detail['phase'], detail['status'], detail['version']) == ('accepted', 'completed', 3)
    assert detail['request_version'] == 1
    assert detail['execution_started'] is None
