"""Migrated, reopened SQLite; each operation owns a fresh transaction."""
import pytest
from sqlalchemy import select, func, event, text
from personal_agent_dal.machine.resume_dispatch import consume_intent, prelaunch_context
from personal_agent_dal.machine.resume_authority import resume, ResumeRequest
from personal_agent_dal.storage.machine_models import DispatchIntent, ProviderAttempt, ResumeEpisode
from personal_agent_dal.storage.worker_models import WorkerJob
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.machine.action_lifecycle import cancel_execution
from personal_agent_dal.worker.queue import claim_job
from tests.dal.test_p0_02_review_regressions import world
from tests.dal.test_p0_02_resume_authority import setup_resume, approve


def replacement(engine):
    old,_,key,claims=setup_resume(engine)
    # Synthetic source operation binding, no external execution.
    with session_factory(engine)() as s,s.begin(): s.get(ProviderAttempt,old.attempt_id).job_id='j'
    approval=approve(engine,key,claims)
    result=resume(engine,feature_id='f',body=ResumeRequest(request_id='resume',approval_id=approval['approval_id']))
    with session_factory(engine)() as s:
        assert s.scalar(text('PRAGMA foreign_keys'))==1
        intent=s.scalar(select(DispatchIntent.intent_id))
    return intent,result['new_attempt_id']


def test_duplicate_intent_fresh_job_and_reopen(world):
    intent,attempt=replacement(world)
    job=consume_intent(world,intent_id=intent)
    world.dispose()
    assert consume_intent(world,intent_id=intent)==job and job!='j'
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(ResumeEpisode))==1
        assert s.get(WorkerJob,job).intake_key is None
        assert s.get(WorkerJob,'j').state=='running'


def test_atomic_rollback(world):
    intent,_=replacement(world)
    def fail(conn,cursor,statement,*args):
        if statement.startswith('INSERT INTO resume_episodes'): raise RuntimeError('injected')
    event.listen(world,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError):consume_intent(world,intent_id=intent)
    finally:event.remove(world,'before_cursor_execute',fail)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(WorkerJob))==1


def test_cancel_before_consumption(world):
    intent,_=replacement(world)
    cancel_execution(world,feature_id='f',expected_gate_version=3)
    with pytest.raises(ValueError,match='AUTHORIZATION'):consume_intent(world,intent_id=intent)


def test_policy_lease_requires_current_worker_enrollment(world):
    intent,_=replacement(world);job=consume_intent(world,intent_id=intent)
    assert claim_job(world,worker_id='w',lease_ttl_seconds=60)==job
    with pytest.raises(ValueError,match='WORKER_ENROLLMENT_STALE'):
        prelaunch_context(world,job_id=job,worker_id='w',job_lease_epoch=1)


def test_independent_concurrent_consumers_create_one_episode(world):
    from concurrent.futures import ThreadPoolExecutor
    intent,_=replacement(world)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=list(pool.map(lambda _:consume_intent(world,intent_id=intent),range(2)))
    assert jobs[0]==jobs[1]
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(ResumeEpisode))==1
        assert s.scalar(select(func.count()).select_from(WorkerJob))==2


def test_missing_source_operation_is_not_guessed(world):
    intent,_=replacement(world)
    from personal_agent_dal.storage.machine_models import ReplacementBudget
    with session_factory(world)() as s,s.begin():
        budget=s.scalar(select(ReplacementBudget))
        s.get(ProviderAttempt,budget.old_attempt_id).job_id=None
    with pytest.raises(ValueError,match='WORKER_OPERATION_BINDING_UNIMPLEMENTED'):
        consume_intent(world,intent_id=intent)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(ResumeEpisode))==0


def test_consumed_episode_prevents_destructive_downgrade(world):
    from personal_agent_dal.storage import db
    intent,_=replacement(world);job=consume_intent(world,intent_id=intent)
    with pytest.raises(RuntimeError,match='consumed scheduling authority'):
        db.downgrade(world,'0015')
    with session_factory(world)() as s:
        assert s.get(WorkerJob,job) is not None
        assert s.get(ResumeEpisode,intent).job_id==job
