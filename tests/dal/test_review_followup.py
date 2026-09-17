"""Offline migrated queue boundaries; no physical isolation/provider acceptance."""
from datetime import timedelta
import logging

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect, select, text

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.resume_authority import ResumeRequest, resume
from personal_agent_dal.machine.resume_dispatch import consume_intent, consume_pending
from personal_agent_dal.service.app import create_app
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory
from personal_agent_dal.storage.machine_models import DispatchIntent, ProviderAttempt, ResumeEpisode
from personal_agent_dal.storage.worker_models import WorkerJob, WorkerResultReceipt
from personal_agent_dal.worker.cli import _local_problems
from personal_agent_dal.worker.config import LocalTransportConfig
from personal_agent_dal.worker.queue import claim_job, enqueue_job, reclaim_expired
from tests.dal.test_p0_02_review_regressions import world
from tests.dal.test_p0_02_resume_authority import setup_resume, approve
from tests.dal.test_resume_dispatch import replacement


@pytest.fixture
def migrated(tmp_path):
    engines = []

    def build(revision='head'):
        path = tmp_path / f'{revision}.db'
        engine = create_database_engine(path)
        db.upgrade(engine, revision)
        engine.dispose()
        engine = create_database_engine(path)
        engines.append(engine)
        with engine.connect() as conn:
            assert conn.scalar(text('PRAGMA foreign_keys')) == 1
        return engine, LocalTransportConfig(database_path=path)

    yield build
    for engine in engines:
        engine.dispose()


@pytest.mark.parametrize('revision', ['head', '0006'])
def test_healthcheck_uses_bundled_heads_without_migrating(migrated, revision):
    engine, transport = migrated(revision)
    with engine.connect() as conn:
        before = set(conn.scalars(text('SELECT version_num FROM alembic_version')))
    heads = set(ScriptDirectory.from_config(db.alembic_config(engine)).get_heads())
    problems = _local_problems(transport)
    assert problems == ([] if revision == 'head' else ['database schema is not at current migration heads'])
    assert (before == heads) == (revision == 'head')
    with engine.connect() as conn:
        assert set(conn.scalars(text('SELECT version_num FROM alembic_version'))) == before


def test_healthcheck_rejects_extra_revision_row(migrated):
    engine, transport = migrated()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO alembic_version VALUES ('unexpected')"))
    assert _local_problems(transport) == ['database schema is not at current migration heads']


def test_healthcheck_missing_and_unreadable(tmp_path):
    path = tmp_path / 'missing.db'
    transport = LocalTransportConfig(database_path=path)
    assert _local_problems(transport) == ['database_path missing']
    assert not path.exists()
    path.write_bytes(b'not a SQLite database')
    assert _local_problems(transport) == ['database schema unreadable']


def snapshot(engine):
    """All persisted rows: detects counts, authority versions and lease mutations."""
    with engine.connect() as conn:
        assert conn.scalar(text('PRAGMA foreign_keys')) == 1
        return {name: tuple(sorted(conn.execute(text(f'SELECT * FROM "{name}"')).all(), key=repr))
                for name in inspect(conn).get_table_names()}


def test_resume_replay_under_kill_switch_preserves_all_state(world):
    _, _, key, claims = setup_resume(world)
    approval = approve(world, key, claims)
    body = ResumeRequest(request_id='resume', approval_id=approval['approval_id'])
    receipt = resume(world, feature_id='f', body=body)
    world.dispose()
    before = snapshot(world)
    assert resume(world, feature_id='f', body=body, kill_switch=lambda: True) == receipt
    assert snapshot(world) == before
    with pytest.raises(ValueError, match='^KILL_SWITCH_ACTIVE$'):
        resume(world, feature_id='f', body=ResumeRequest(
            request_id='fresh', approval_id=approval['approval_id']), kill_switch=lambda: True)
    assert snapshot(world) == before


def mount(engine, config=None):
    return create_app(engine, service_key=b'synthetic-service-key',
                      enrollment_secret=b'synthetic-enrollment', resume_config=config)


@pytest.mark.parametrize('state', ['empty', 'pending', 'materialized', 'enabled'])
def test_disabled_startup_diagnostic(world, caplog, state):
    if state != 'empty':
        intent, _ = replacement(world)
        if state == 'materialized':
            consume_intent(world, intent_id=intent)
            with session_factory(world)() as session:
                assert session.get(DispatchIntent, intent).status == 'awaiting_episode'
    before = snapshot(world)
    with caplog.at_level(logging.ERROR, logger='personal_agent_dal.service.resume_routes'):
        app = mount(world, {'profiles': []} if state == 'enabled' else None)
    assert app is not None
    records = [r for r in caplog.records if r.name == 'personal_agent_dal.service.resume_routes']
    assert [(r.levelno, r.getMessage()) for r in records] == (
        [(logging.ERROR, 'DAL_RESUME_DISABLED_PENDING_INTENTS')] if state == 'pending' else [])
    assert all(not r.args and r.exc_info is None for r in records)
    assert snapshot(world) == before


@pytest.mark.parametrize('revision', ['0006', '0015'])
def test_disabled_startup_legacy_tables_absent(migrated, caplog, revision):
    engine, _ = migrated(revision)
    before = snapshot(engine)
    with caplog.at_level(logging.ERROR, logger='personal_agent_dal.service.resume_routes'):
        assert mount(engine) is not None
    assert not [r for r in caplog.records if r.name == 'personal_agent_dal.service.resume_routes']
    assert snapshot(engine) == before


def test_startup_does_not_swallow_unrelated_storage_failure(world):
    def fail(conn, cursor, statement, *args):
        if 'dispatch_intents' in statement and statement.lstrip().upper().startswith('SELECT'):
            raise RuntimeError('synthetic storage failure')
    event.listen(world, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic storage failure'):
            mount(world)
    finally:
        event.remove(world, 'before_cursor_execute', fail)


def test_leased_first_pending_job_does_not_block_second(world):
    intent, _ = replacement(world)
    first = consume_intent(world, intent_id=intent)
    with session_factory(world)() as session:
        created = session.get(WorkerJob, first).created_at
    second = enqueue_job(world, feature_id='f', repository_id='repo-placeholder',
                         base_sha='0'*40, branch_name='second', toolchain_ref='test',
                         now=created + timedelta(seconds=1))
    now = utc_now()
    assert claim_job(world, worker_id='w', lease_ttl_seconds=60, now=now) == first
    assert claim_job(world, worker_id='w', lease_ttl_seconds=60, now=now) == second
    with session_factory(world)() as session:
        assert session.get(WorkerJob, first).state == 'leased'
        assert session.get(WorkerJob, second).state == 'leased'


def test_expiry_budget_never_recreates_episode_or_fabricates_result(world):
    intent, attempt_id = replacement(world)
    consume_pending(world)
    with session_factory(world)() as session:
        job_id = session.get(ResumeEpisode, intent).job_id
    now = utc_now()
    for epoch in range(1, 4):
        assert claim_job(world, worker_id='w', lease_ttl_seconds=10, now=now) == job_id
        now += timedelta(seconds=11)
        assert reclaim_expired(world, max_attempts=3, now=now) == [job_id]
        before = snapshot(world)
        consume_pending(world)
        assert snapshot(world) == before
        with session_factory(world)() as session:
            job = session.get(WorkerJob, job_id)
            assert (job.attempt_count, job.lease_epoch) == (epoch, epoch)
            assert job.state == ('expired' if epoch == 3 else 'pending')
            assert job.worker_id is job.lease_expires_at is job.heartbeat_at is None
            assert job.result_sha256 is job.last_error is None
            assert session.get(ResumeEpisode, intent).job_id == job_id
            attempt = session.get(ProviderAttempt, attempt_id)
            assert attempt.state == 'prepared'
            assert attempt.dispatch_started_at is attempt.result_digest is None
            assert list(session.scalars(select(WorkerResultReceipt))) == []
            assert len(list(session.scalars(select(ResumeEpisode)))) == 1
            assert len(list(session.scalars(select(WorkerJob)))) == 2
    assert claim_job(world, worker_id='w', lease_ttl_seconds=10, now=now) is None
    assert reclaim_expired(world, max_attempts=3, now=now) == []
