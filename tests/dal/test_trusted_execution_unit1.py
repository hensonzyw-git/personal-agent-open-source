"""Local SQLite evidence for initial authorization, constraints and lost leases."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import event, select, func, text
from sqlalchemy.exc import IntegrityError
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory
from personal_agent_dal.storage.machine_models import (
    WorkflowAction, ProviderAttempt, ExecutionGate, ExecutionJobBinding, ExecutionStartReceipt,
)
from personal_agent_dal.storage.worker_models import WorkerJob, WorkerResultReceipt
from personal_agent_dal.machine.execution_start import (
    PrepareExecutionRequest, StartExecutionRequest, prepare_execution, start_execution,
)
from personal_agent_dal.machine.workflow_selection import register_profile
from personal_agent_dal.worker.queue import claim_job, heartbeat, reclaim_expired, _finish_job_in_session
from tests.dal.factories import feature_row


@pytest.fixture
def world(tmp_path):
    engine = create_database_engine(tmp_path/'unit.db')
    db.upgrade(engine)
    with session_factory(engine)() as s, s.begin():
        s.add(feature_row(feature_id='f', version=1, state='coding'))
    roles = {r: dict(runtime='codex_cli', provider='openai', model=m, reasoning=q,
        placement='home_mac', permission=p, billing='subscription') for r,m,q,p in [
            ('planner','gpt-6-astra','medium','read_only'), ('coder','gpt-5.6-sol','high','workspace_write'),
            ('reviewer','gpt-6-astra','medium','read_only')]}
    register_profile(engine, revision_id='B-1', profile='B', revision=1, roles=roles)
    yield engine
    engine.dispose()


def preparation(**changes):
    body = dict(request_id='select', action_key='task-1', execution_role='coder', profile_revision_id='B-1',
        expected_feature_version=1, expected_gate_version=0, execution_input={
            'schema':'dal.execution-input/1.0','feature_id':'f','task_source':{'kind':'operator'},
            'task_description':'Synthetic task','task_description_sha256':hashlib.sha256(b'Synthetic task').hexdigest(),
            'repository_id':'repo','base_sha':'0'*40,'branch_name':'task','toolchain_ref':'test',
            'toolchain_manifest_sha256':'a'*64})
    body.update(changes)
    return PrepareExecutionRequest.model_validate(body)


def prepared(world):
    return prepare_execution(world, feature_id='f', actor='real-token-operator', body=preparation())


def request(p, **changes):
    body = dict(request_id='start', action_id=p['action_id'], selection_id=p['selection_id'],
        expected_feature_version=1, expected_gate_version=1, expected_action_version=1,
        confirmed_execution_sha256=p['confirmed_execution_sha256'], expires_at=int(utc_now().timestamp())+600)
    body.update(changes)
    return StartExecutionRequest(**body)


def start(world,p,**changes):
    return start_execution(world, feature_id='f', actor='real-token-operator', body=request(p,**changes))


def test_selection_is_not_execution_and_concurrent_start_is_one(world):
    p = prepared(world)
    with session_factory(world)() as s:
        assert s.get(ExecutionGate,'f').mode == 'paused'
        assert s.scalar(select(func.count()).select_from(ProviderAttempt)) == 0
        assert s.scalar(select(func.count()).select_from(WorkerJob)) == 0
    body = request(p)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start_execution(world,feature_id='f',actor='real-token-operator',body=body),range(2)))
    assert results[0] == results[1]
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(ExecutionJobBinding)) == 1
        assert s.get(ExecutionStartReceipt,'start').actor == 'real-token-operator'
        assert s.get(ExecutionStartReceipt,'start').binding_sha256 == body.confirmed_execution_sha256
        assert s.get(WorkerJob,results[0]['job_id']).execution_mode == 'provider_v1'


def test_confirmation_mismatch_and_missing_resolver(world):
    with pytest.raises(ValueError,match='RESOLVER'):
        prepare_execution(world,feature_id='f',actor='operator',body=preparation(completion_mode='feature_transition'))
    p = prepared(world)
    with pytest.raises(ValueError,match='MISMATCH'): start(world,p,confirmed_execution_sha256='b'*64)
    with session_factory(world)() as s:
        assert s.get(ExecutionGate,'f').mode == 'paused'
        assert s.scalar(select(func.count()).select_from(WorkerJob)) == 0


def test_start_rolls_back_gate_job_attempt_and_receipt(world):
    p=prepared(world)
    def fail(conn,cursor,statement,*args):
        if statement.startswith('INSERT INTO execution_start_receipts'): raise RuntimeError('injected')
    event.listen(world,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError,match='injected'): start(world,p)
    finally: event.remove(world,'before_cursor_execute',fail)
    with session_factory(world)() as s:
        assert s.get(ExecutionGate,'f').mode == 'paused'
        assert s.get(WorkflowAction,p['action_id']).active_attempt_id is None
        assert s.scalar(select(func.count()).select_from(WorkerJob)) == 0
        assert s.scalar(select(func.count()).select_from(ProviderAttempt)) == 0


def test_expired_heartbeat_and_dispatched_reclaim(world):
    p=prepared(world); result=start(world,p); now=utc_now()
    claim_job(world,worker_id='w',lease_ttl_seconds=1,now=now)
    later=now+timedelta(seconds=2)
    assert not heartbeat(world,job_id=result['job_id'],worker_id='w',lease_epoch=1,lease_ttl_seconds=60,now=later)
    with session_factory(world)() as s,s.begin():
        a=s.get(ProviderAttempt,result['attempt_id']);a.state='dispatching';a.dispatch_started_at=now
    assert reclaim_expired(world,max_attempts=5,now=later)==[result['job_id']]
    with session_factory(world)() as s:
        assert s.get(WorkerJob,result['job_id']).state=='expired'
        assert s.get(ProviderAttempt,result['attempt_id']).state=='unknown'
        assert s.get(ExecutionGate,'f').mode=='paused'


def test_partial_contract_rejected_by_database(world):
    p=prepared(world)
    with session_factory(world)() as s:
        with pytest.raises(IntegrityError):
            s.execute(text('UPDATE workflow_actions SET execution_role=NULL WHERE action_id=:id'),{'id':p['action_id']})
            s.commit()


def test_same_session_finish_rollback(world):
    p=prepared(world); result=start(world,p)
    claim_job(world,worker_id='w',lease_ttl_seconds=60)
    with session_factory(world)() as s:
        assert _finish_job_in_session(s,job_id=result['job_id'],worker_id='w',lease_epoch=1,state='succeeded',result_sha256='a'*64)
        s.rollback()
    with session_factory(world)() as s:
        assert s.get(WorkerJob,result['job_id']).state=='leased'
        assert s.scalar(select(func.count()).select_from(WorkerResultReceipt))==0


def test_upgrade_0017_preserves_legacy_rows_and_downgrade(tmp_path):
    engine=create_database_engine(tmp_path/'upgrade.db')
    db.upgrade(engine,'0017')
    # Raw insert is deliberate: current ORM has columns absent at the old revision.
    with engine.begin() as c:
        c.execute(text("INSERT INTO worker_jobs (job_id,feature_id,repository_id,base_sha,branch_name,toolchain_ref,state,attempt_count,lease_epoch,created_at,updated_at) VALUES ('legacy','f','repo',:sha,'task','test','pending',0,0,:now,:now)"),
                  {'sha':'0'*40,'now':utc_now().isoformat()})
    db.upgrade(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT execution_mode FROM worker_jobs WHERE job_id='legacy'")).scalar_one()=='legacy_unclassified'
        assert not c.execute(text('PRAGMA foreign_key_check')).all()
    db.downgrade(engine,'0017')
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM worker_jobs')).scalar_one()==1
    engine.dispose()


def test_ack_without_dispatch_pauses_without_unknown(world):
    from personal_agent_dal.storage.transport_models import SupervisorLaunchManifest
    p=prepared(world); result=start(world,p); now=utc_now()
    claim_job(world,worker_id='w',lease_ttl_seconds=1,now=now)
    with session_factory(world)() as s,s.begin():
        s.add(SupervisorLaunchManifest(attempt_id=result['attempt_id'],body='{}',sha256='a'*64,recorded_at=now))
    reclaim_expired(world,max_attempts=5,now=now+timedelta(seconds=2))
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt,result['attempt_id']).state=='prepared'
        assert s.get(WorkerJob,result['job_id']).last_error=='preparation_lease_expired'
        assert s.get(ExecutionGate,'f').mode=='paused'
        assert s.get(SupervisorLaunchManifest,result['attempt_id']) is not None


def test_known_result_does_not_become_unknown(world):
    p=prepared(world); result=start(world,p); now=utc_now()
    claim_job(world,worker_id='w',lease_ttl_seconds=1,now=now)
    with session_factory(world)() as s,s.begin():
        a=s.get(ProviderAttempt,result['attempt_id']); a.state='result_recorded'
        a.dispatch_started_at=now; a.result_digest='a'*64; a.result_recorded_at=now
    reclaim_expired(world,max_attempts=5,now=now+timedelta(seconds=2))
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt,result['attempt_id']).state=='result_recorded'
        assert s.get(WorkerJob,result['job_id']).last_error=='result_available_not_accepted'


def test_unclassified_context_is_not_execution_permission(world):
    from personal_agent_dal.worker.queue import enqueue_job_in_session
    from personal_agent_dal.machine.resume_dispatch import prelaunch_context
    with session_factory(world)() as s,s.begin():
        job=enqueue_job_in_session(s,feature_id='f',repository_id='repo',base_sha='0'*40,
            branch_name='task',toolchain_ref='test',now=utc_now())
    claim_job(world,worker_id='w',lease_ttl_seconds=60)
    with pytest.raises(ValueError,match='UNCLASSIFIED'):
        prelaunch_context(world,job_id=job,worker_id='w',job_lease_epoch=1)


def test_preparation_replay_preserves_exact_confirmation(world):
    p=prepared(world)
    assert prepared(world)==p
    start(world,p)
    assert prepared(world)==p


def test_new_provider_cannot_use_legacy_result_endpoint(world):
    from personal_agent_dal.worker.queue import finish_job
    p=prepared(world); result=start(world,p)
    claim_job(world,worker_id='w',lease_ttl_seconds=60)
    with pytest.raises(ValueError,match='PROVIDER_EXECUTION_RESULT_REQUIRED'):
        finish_job(world,job_id=result['job_id'],worker_id='w',lease_epoch=1,state='succeeded',result_sha256='a'*64)


def test_kill_switch_blocks_new_start_but_allows_receipt_replay(world):
    p=prepared(world); body=request(p)
    with pytest.raises(ValueError,match='KILL_SWITCH'):
        start_execution(world,feature_id='f',actor='real-token-operator',body=body,kill_switch=lambda:True)
    result=start_execution(world,feature_id='f',actor='real-token-operator',body=body)
    assert start_execution(world,feature_id='f',actor='real-token-operator',body=body,kill_switch=lambda:True)==result


def test_initial_context_issues_one_bounded_lease_and_blocks_legacy_manifest(world):
    from personal_agent_dal.storage.transport_models import WorkerEnrollment
    from personal_agent_dal.storage.machine_models import ExecutionPolicyLeaseIssuance, Lease
    from personal_agent_dal.machine.resume_dispatch import prelaunch_context
    from personal_agent_dal.machine.action_lifecycle import claim_dispatch
    p=prepared(world); result=start(world,p)
    with session_factory(world)() as s,s.begin():
        s.add(WorkerEnrollment(worker_id='w',machine_id='synthetic',capabilities='["coding"]',created_at=utc_now()))
    claim_job(world,worker_id='w',lease_ttl_seconds=60)
    context=prelaunch_context(world,job_id=result['job_id'],worker_id='w',job_lease_epoch=1)
    assert context==prelaunch_context(world,job_id=result['job_id'],worker_id='w',job_lease_epoch=1)
    assert context['execution_role']=='coder'
    assert context['execution_input']['task_description']=='Synthetic task'
    with session_factory(world)() as s:
        lease=s.get(Lease,context['lease_id']);job=s.get(WorkerJob,result['job_id'])
        assert lease.expires_at<=job.lease_expires_at
        assert s.scalar(select(func.count()).select_from(ExecutionPolicyLeaseIssuance))==1
    assert claim_dispatch(world,attempt_id=result['attempt_id'],expected_version=1,owner_id='w',
        job_id=result['job_id'],lease_id=context['lease_id']).code=='MANIFEST_ACKNOWLEDGEMENT_REQUIRED'


def test_same_session_result_and_finish_rollback_together(tmp_path):
    from tests.dal.test_p0_02_action_lifecycle import _engine
    from personal_agent_dal.machine.action_lifecycle import create_provider_action, claim_dispatch, _record_result_in_session
    engine=_engine(tmp_path)
    action=create_provider_action(engine,feature_id='feature-1',action_key='task',input_binding_sha256='a'*64,execution_snapshot_sha256='b'*64)
    assert claim_dispatch(engine,attempt_id=action.attempt_id,expected_version=1,owner_id='worker-a',job_id='j',lease_id='l').code=='DISPATCH_GRANTED'
    with session_factory(engine)() as s:
        assert _record_result_in_session(s,attempt_id=action.attempt_id,expected_version=2,owner_id='worker-a',fence=1,digest='c'*64).code=='RESULT_RECORDED'
        assert _finish_job_in_session(s,job_id='j',worker_id='worker-a',lease_epoch=3,state='succeeded',result_sha256='c'*64)
        s.rollback()
    with session_factory(engine)() as s:
        assert s.get(ProviderAttempt,action.attempt_id).state=='dispatching'
        assert s.get(ProviderAttempt,action.attempt_id).result_digest is None
        assert s.get(WorkerJob,'j').state=='running'
        assert s.scalar(select(func.count()).select_from(WorkerResultReceipt))==0
    engine.dispose()
