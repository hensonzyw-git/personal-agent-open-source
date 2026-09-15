"""Independent connections, real signed contracts; no CLI/provider execution."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import event, select, func
from personal_agent.auth.device_keys import encode_device_public_key
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import ProviderAttempt, WorkflowAction, ExecutionGate, ExecutionResultEnvelope, Lease
from personal_agent_dal.storage.worker_models import WorkerJob, WorkerResultReceipt
from personal_agent_dal.storage.transport_models import WorkerEnrollment
from personal_agent_dal.machine.isolation_evidence import register_supervisor
from personal_agent_dal.machine.execution_manifest import sign_execution_manifest
from personal_agent_dal.machine.execution_results import canonical_result, submit_execution_result, execution_status
from personal_agent_dal.machine.execution_start import prepare_execution
from personal_agent_dal.machine.workflow_selection import digest
from personal_agent_dal.service.app import create_app
from personal_agent_dal.service.tokens import issue_token
from personal_agent_dal.worker.remote import RemoteHttpAdapter, CachedToken
from tests.dal.test_trusted_execution_unit1 import world, prepared, start, preparation

@pytest.fixture
def launched(world,tmp_path):
    p=prepared(world); r=start(world,p)
    key=ec.generate_private_key(ec.SECP256R1())
    with session_factory(world)() as s,s.begin():
        s.add(WorkerEnrollment(worker_id='w',machine_id='synthetic',capabilities='[]',created_at=utc_now()))
    register_supervisor(world,kid='k',worker_id='w',machine_id='synthetic',public_key=encode_device_public_key(key.public_key()),boot_id='boot',supervisor_epoch=1)
    switch=tmp_path/'kill'
    app=create_app(world,service_key=b'synthetic',enrollment_secret=b'synthetic-enroll',lease_ttl_seconds=720,
        kill_switch_path=switch,resume_config={'issuer':'pa','audience':'dal','keys':{},'profiles':[]})
    t=RemoteHttpAdapter(SimpleNamespace(worker_id='w',endpoint='https://testserver',retry_attempts=1),client=TestClient(app))
    expiry=int(utc_now().timestamp())+600
    t._token=CachedToken('w',issue_token(worker_id='w',capabilities=[],key=b'synthetic',expires_at_epoch=expiry),expiry)
    l=t.claim(); c=t.prelaunch_context(l)
    payload=dict(schema='dal.launch-manifest/1.1',kid='k',worker_id='w',machine_id='synthetic',registration_epoch=1,
        boot_id='boot',supervisor_epoch=1,attempt_id=r['attempt_id'],workspace_id='workspace',workspace_generation=1,
        isolation_policy_sha256='a'*64,issued_at=int(utc_now().timestamp()),expires_at=c['execution_spec']['policy_expires_at'],
        inventory_sha256='b'*64,reservation_id='reservation',job_id=l.job_id,job_lease_epoch=l.lease_epoch,
        lease_id=c['lease_id'],policy_lease_epoch=c['policy_lease_epoch'],execution_spec=c['execution_spec'],
        execution_spec_sha256=c['execution_spec_sha256'],launcher_plan_sha256='c'*64,source_reservation_sha256='b'*64,
        isolation_id=None,isolation_binding_sha256=None)
    assertion,sha=sign_execution_manifest(payload,key=key)
    assert t.acknowledge_prelaunch(l,assertion)=={'manifest_sha256':sha}
    assert t.dispatch_prelaunch(l,sha)=={'code':'DISPATCH_GRANTED'}
    body={k:c[k] for k in ('feature_id','action_id','attempt_id','job_id','worker_id','job_lease_epoch','lease_id','policy_lease_epoch','snapshot_sha256','execution_role')}
    body.update(schema='dal.execution-result/1.0',request_id='result-1',attempt_version=2,fence=1,
        execution_spec_sha256=c['execution_spec_sha256'],manifest_sha256=sha,outcome='succeeded',reason=None,
        started_at=int(utc_now().timestamp()),ended_at=int(utc_now().timestamp()),stop=dict(requested=False,process_exited=True,forced=False),
        report='Synthetic complete report',cli_exit_code=0,tool_events=[],tests=[],git_evidence=[],artifacts=[],
        usage=dict(input_tokens=None,output_tokens=None,provider_requests=None),unverified=[],truncated=False,redacted=False)
    request=dict(schema='dal.worker-execution-transport/1.0',result=body,result_sha256=digest(body))
    return t,l,request,switch


def test_atomic_report_replay_and_next_role(world,launched):
    t,l,r,switch=launched
    result=t.submit_execution_result(l,r)
    assert result['code']=='REPORT_ACCEPTED' and result['accepted']
    switch.touch()
    replay=t.submit_execution_result(l,r)
    assert replay['code']=='REPORT_REPLAY' and replay['receipt_id']==result['receipt_id']
    status=t.execution_status(l)
    assert status['classification']=='report_complete' and status['stop_required']
    with session_factory(world)() as s:
        a=s.get(ProviderAttempt,r['result']['attempt_id'])
        assert a.result_consumed_at is None and a.consumption_receipt_id is None
        assert s.get(WorkflowAction,a.action_id).active_attempt_id is None
        assert s.get(ExecutionGate,'f').mode=='paused'
    p=preparation(request_id='select-next',action_key='review-task',execution_role='reviewer',expected_gate_version=3)
    assert prepare_execution(world,feature_id='f',actor='operator',body=p)['action_id']!=r['result']['action_id']


@pytest.mark.parametrize('failure_prefix',['INSERT INTO worker_result_receipts','UPDATE execution_gates'])
def test_receipt_insert_failure_rolls_back_all_authority(world,launched,failure_prefix):
    t,l,r,_=launched
    def fail(conn,cursor,statement,*args):
        if statement.startswith(failure_prefix): raise RuntimeError('injected')
    event.listen(world,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError,match='injected'): t.submit_execution_result(l,r)
    finally: event.remove(world,'before_cursor_execute',fail)
    with session_factory(world)() as s:
        a=s.get(ProviderAttempt,r['result']['attempt_id'])
        assert a.state=='dispatching' and a.result_digest is None and a.report_receipt_id is None
        assert s.get(WorkflowAction,a.action_id).active_attempt_id==a.attempt_id
        assert s.get(ExecutionGate,'f').mode=='open'
        assert s.scalar(select(func.count()).select_from(ExecutionResultEnvelope))==1
        assert s.scalar(select(func.count()).select_from(WorkerResultReceipt))==0
    assert t.submit_execution_result(l,r)['accepted']


def test_concurrent_independent_results_one_receipt(world,launched):
    _,l,r,_=launched
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:submit_execution_result(world,job_id=l.job_id,worker_id='w',request=r),range(2)))
    assert sorted(x['code'] for x in results)==['REPORT_ACCEPTED','REPORT_REPLAY']
    assert len({x['receipt_id'] for x in results})==1

@pytest.mark.parametrize('kind',['lease','policy','cancel','kill','unknown'])
def test_evidence_not_authority(world,launched,kind):
    t,l,r,switch=launched
    with session_factory(world)() as s,s.begin():
        if kind=='lease': s.get(WorkerJob,l.job_id).lease_expires_at=utc_now()-timedelta(seconds=1)
        if kind=='policy': s.get(Lease,r['result']['lease_id']).revoked_at=utc_now()
        if kind=='cancel':
            s.get(ExecutionGate,'f').mode='cancelled';s.get(ExecutionGate,'f').approval_epoch+=1
    if kind=='kill': switch.touch()
    if kind=='unknown':
        r['result'].update(outcome='unknown',cli_exit_code=None);r['result']['stop']['process_exited']=False
        r['result_sha256']=digest(r['result'])
    outcome=t.submit_execution_result(l,r)
    assert not outcome['accepted']
    status=t.execution_status(l)
    assert status['classification']==('execution_effects_unknown' if kind=='unknown' else 'result_available_not_accepted')
    assert status['stop_required']
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt,r['result']['attempt_id']).result_digest is None
        assert s.scalar(select(func.count()).select_from(WorkerResultReceipt))==0
        if kind=='cancel': assert s.get(ExecutionGate,'f').mode=='cancelled'


def test_redaction_digest_and_conflict(world,launched):
    t,l,r,_=launched
    r['result']['report']='api_key=synthetic-secret-value'
    clean,sha=canonical_result(r['result'])
    assert 'synthetic-secret-value' not in json.dumps(clean)
    with pytest.raises(Exception,match='RESULT_DIGEST_MISMATCH'): t.submit_execution_result(l,r)
    r['result_sha256']=sha
    assert t.submit_execution_result(l,r)['accepted']
    r['result']=dict(clean,report='Different report');r['result_sha256']=digest(r['result'])
    assert t.submit_execution_result(l,r)['code']=='RESULT_CONFLICT'
    with session_factory(world)() as s:
        bodies=list(s.scalars(select(ExecutionResultEnvelope.body)))
        assert all('synthetic-secret-value' not in b for b in bodies)
        assert all(digest(json.loads(b)) in (sha,r['result_sha256']) for b in bodies)


def test_historical_status_not_foreign_authority(world,launched):
    t,l,r,_=launched
    t.submit_execution_result(l,r)
    with session_factory(world)() as s,s.begin():
        s.add(WorkerEnrollment(worker_id='other',machine_id='other',capabilities='[]',created_at=utc_now()))
    with pytest.raises(ValueError,match='WORKER_MISMATCH'): execution_status(world,job_id=l.job_id,worker_id='other')
    assert t.execution_status(l)['report_receipt_id']


def test_replacement_inherits_input_and_issues_generic_lease(world):
    from personal_agent_dal.machine.action_lifecycle import pause_execution
    from personal_agent_dal.machine.workflow_selection import SelectionRequest, select_workflow
    from personal_agent_dal.machine.resume_authority import propose, resume, ResumeRequest
    from personal_agent_dal.machine.resume_dispatch import consume_intent, prelaunch_context
    from personal_agent_dal.storage.machine_models import DispatchIntent, ExecutionPolicyLeaseIssuance, ExecutionJobBinding
    from personal_agent_dal.storage.models import Feature
    from personal_agent_dal.worker.queue import claim_job
    from tests.dal.test_p0_02_resume_authority import approve
    p=prepared(world); first=start(world,p)
    with session_factory(world)() as s,s.begin():
        s.get(Feature,'f').artifact_sha256='a'*64
        s.add(WorkerEnrollment(worker_id='w',machine_id='synthetic',capabilities='[]',created_at=utc_now()))
    assert pause_execution(world,feature_id='f',expected_gate_version=2).code=='PAUSED'
    selection=select_workflow(world,feature_id='f',actor='operator',body=SelectionRequest(request_id='replace-select',profile_revision_id='B-1',expected_feature_version=1,expected_gate_version=3))
    proposal=propose(world,feature_id='f',selection_id=selection['selection_id'],request_id='proposal')
    key=ec.generate_private_key(ec.SECP256R1());now=int(utc_now().timestamp())
    approval=approve(world,key,dict(iss='pa-resume',aud='dal-resume',jti='jti',iat=now,exp=now+600,
        decision_id='decision',device_id='phone',subject_id='device:phone',key_thumbprint='a'*43,
        decision='approve_once',proposal_id=proposal['proposal_id'],binding_sha256=proposal['binding_sha256']))
    replacement=resume(world,feature_id='f',body=ResumeRequest(request_id='resume',approval_id=approval['approval_id']))
    with session_factory(world)() as s:
        intent=s.scalar(select(DispatchIntent.intent_id))
    job=consume_intent(world,intent_id=intent)
    # The superseded original queue row is never executable; select replacement directly after parking it.
    with session_factory(world)() as s,s.begin():
        s.get(WorkerJob,first['job_id']).state='cancelled'
    assert claim_job(world,worker_id='w',lease_ttl_seconds=720)==job
    c=prelaunch_context(world,job_id=job,worker_id='w',job_lease_epoch=1)
    assert c['execution_input']==preparation().execution_input.model_dump(by_alias=True)
    assert c['execution_spec']['expected_dispatch_fence']==2
    assert c['execution_spec']['prepared_attempt_version']==1
    with session_factory(world)() as s:
        assert s.get(ExecutionJobBinding,replacement['new_attempt_id']).origin=='replacement'
        assert s.get(ExecutionPolicyLeaseIssuance,(replacement['new_attempt_id'],1)).lease_id==c['lease_id']
        assert s.get(WorkerJob,job).intake_key is None


def test_report_forbids_feature_consumption(world,launched):
    from dataclasses import replace
    from personal_agent_dal.machine.action_lifecycle import consume_result
    from tests.dal.test_p0_02_review_regressions import provider_command
    t,l,r,_=launched;t.submit_execution_result(l,r)
    with session_factory(world)() as s: version=s.get(ProviderAttempt,r['result']['attempt_id']).version
    result=consume_result(world,attempt_id=r['result']['attempt_id'],expected_version=version,command=provider_command())
    assert result.code=='REPORT_ONLY_NOT_CONSUMABLE'

@pytest.mark.parametrize('field,value',[('schema','dal.execution-result/2.0'),('fence',True),('extra','no'),('attempt_version',3)])
def test_malformed_or_wrong_fence_is_not_persisted(world,launched,field,value):
    t,l,r,_=launched;r['result'][field]=value;r['result_sha256']=digest(r['result'])
    with pytest.raises((ValueError,RuntimeError)): t.submit_execution_result(l,r)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(ExecutionResultEnvelope))==0

@pytest.mark.parametrize('mode',['legacy_unclassified','provider_v1'])
def test_missing_binding_never_routes_to_legacy(world,mode):
    from personal_agent_dal.machine.resume_dispatch import prelaunch_context
    from personal_agent_dal.storage.machine_models import ExecutionJobBinding
    from personal_agent_dal.worker.queue import claim_job
    p=prepared(world);r=start(world,p)
    claim_job(world,worker_id='w',lease_ttl_seconds=720)
    with session_factory(world)() as s,s.begin():
        s.get(WorkerJob,r['job_id']).execution_mode=mode
        s.delete(s.get(ExecutionJobBinding,r['attempt_id']))
    with pytest.raises(ValueError,match='EXECUTION_MODE_UNCLASSIFIED|EXECUTION_BINDING_REQUIRED'):
        prelaunch_context(world,job_id=r['job_id'],worker_id='w',job_lease_epoch=1)


def test_legacy_result_route_cannot_replay_provider_report(world,launched):
    import hashlib
    from personal_agent_core.manifest import canonical_json
    t,l,r,_=launched;t.submit_execution_result(l,r)
    payload=canonical_json(dict(schema_version='dal.worker-transport/1.0',request_id='legacy',job_id=l.job_id,
        worker_id='w',lease_epoch=l.lease_epoch,state='succeeded',result_sha256=r['result_sha256'],last_error=None)).encode()
    response=t._client.post('/jobs/'+l.job_id+'/result',content=payload,headers={
        'content-type':'application/json','authorization':'Bearer '+t._token.token,
        'x-transport-body-digest':hashlib.sha256(payload).hexdigest()})
    assert response.status_code==409
    assert response.json()['code']=='PROVIDER_EXECUTION_RESULT_REQUIRED'


def test_local_adapter_matches_remote_replay(world,launched,tmp_path):
    from personal_agent_dal.worker.transport import LocalSQLiteAdapter
    import inspect
    t,l,r,_=launched
    # Constructor defaults are exercised by the existing adapter suite.
    local=LocalSQLiteAdapter(world,worker_id='w',lease_ttl_seconds=720,max_attempts=3,checkpoint_root=tmp_path)
    assert local.execution_status(l)==t.execution_status(l)
    assert local.submit_execution_result(l,r)['accepted']
    assert t.submit_execution_result(l,r)['replay']
    for name in ('execution_status','submit_execution_result'):
        assert inspect.signature(getattr(LocalSQLiteAdapter,name))==inspect.signature(getattr(RemoteHttpAdapter,name))


def test_cancel_between_arrival_and_acceptance_is_seen_from_fresh_session(world,launched,monkeypatch):
    from personal_agent_dal.machine import execution_results as module
    t,l,r,_=launched
    original=module._transaction
    calls=0
    def interleave(engine,work):
        nonlocal calls
        result=original(engine,work);calls+=1
        if calls==1:
            with session_factory(world)() as s,s.begin():
                g=s.get(ExecutionGate,'f');g.mode='cancelled';g.approval_epoch+=1
        return result
    monkeypatch.setattr(module,'_transaction',interleave)
    result=t.submit_execution_result(l,r)
    assert result['code']=='RESULT_AVAILABLE_NOT_ACCEPTED'
    with session_factory(world)() as s:
        assert s.get(ExecutionGate,'f').mode=='cancelled'
        assert s.get(ProviderAttempt,r['result']['attempt_id']).result_digest is None


def test_late_old_output_does_not_pause_new_attempt(world,launched):
    t,l,r,_=launched
    # Model a separately committed replacement, keeping the original signed dispatch evidence.
    with session_factory(world)() as s,s.begin():
        old=s.get(ProviderAttempt,r['result']['attempt_id'])
        old.state='superseded';old.version+=1
        a=s.get(WorkflowAction,old.action_id)
        new=ProviderAttempt(attempt_id='replacement',action_id=a.action_id,attempt_no=2,state='prepared',version=1,
            fence=2,approval_epoch=old.approval_epoch+1,feature_version=old.feature_version,
            capability_epoch=old.capability_epoch,created_at=utc_now(),updated_at=utc_now())
        s.add(new);s.flush();a.active_attempt_id=new.attempt_id;a.version+=1
        g=s.get(ExecutionGate,'f');g.version+=1;g.approval_epoch+=1
    result=t.submit_execution_result(l,r)
    assert result['code']=='RESULT_AVAILABLE_NOT_ACCEPTED' and not result['accepted']
    with session_factory(world)() as s:
        assert s.get(ExecutionGate,'f').mode=='open'
        assert s.get(WorkflowAction,r['result']['action_id']).active_attempt_id=='replacement'
        assert s.get(ProviderAttempt,'replacement').state=='prepared'


def test_known_result_does_not_become_unknown_on_recovery(world,launched):
    from personal_agent_dal.machine.action_recovery import recover_attempt
    t,l,r,_=launched
    with session_factory(world)() as s,s.begin():
        s.get(WorkerJob,l.job_id).lease_expires_at=utc_now()-timedelta(seconds=1)
    assert t.submit_execution_result(l,r)['code']=='RESULT_AVAILABLE_NOT_ACCEPTED'
    with session_factory(world)() as s: version=s.get(ProviderAttempt,r['result']['attempt_id']).version
    result=recover_attempt(world,attempt_id=r['result']['attempt_id'],expected_version=version,
        command_id='recover',requested_by='operator')
    assert result.code=='RECOVERY_NOT_NEEDED'
