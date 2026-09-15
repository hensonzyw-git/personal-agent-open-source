"""Renewed authority must fail closed before any Worker dispatch."""
import pytest
from pydantic import ValidationError

from personal_agent_dal.machine.resume_authority import ResumeRequest
from personal_agent_dal.machine.workflow_selection import SelectionRequest


@pytest.mark.parametrize('field', ['selection_id', 'expected_gate_version', 'approved', 'device_id', 'subject_id'])
def test_resume_is_closed(field):
    with pytest.raises(ValidationError):
        ResumeRequest.model_validate({'request_id': 'r', 'approval_id': 'a', field: True})


@pytest.mark.parametrize('value', ['', None, True, 1, 'a b', '../x'])
def test_resume_refuses_malformed_identity(value):
    with pytest.raises(ValidationError):
        ResumeRequest(request_id=value, approval_id='a')


@pytest.mark.parametrize('value', [True, '1', 0, -1])
def test_selection_versions_are_strict(value):
    with pytest.raises(ValidationError):
        SelectionRequest(request_id='r', profile_revision_id='A-1', expected_feature_version=value,
                         expected_gate_version=1)

from datetime import timedelta
from sqlalchemy import event, select, func
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import pause_execution, claim_dispatch, cancel_execution
from personal_agent_dal.machine.resume_authority import propose, import_decision, resume, revoke_decision
from personal_agent_dal.machine.workflow_selection import register_profile, select_workflow
from personal_agent_dal.storage.machine_models import Approval, ProviderAttempt, ExecutionGate, ResumeReceipt, DispatchIntent
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.models import Feature
from personal_agent.api.dal_client import sign_decision
from tests.dal.test_p0_02_review_regressions import world, create, claim
from cryptography.hazmat.primitives.asymmetric import ec


def setup_resume(engine, *, same_snapshot=False):
    a = create(engine)
    with session_factory(engine)() as s, s.begin():
        s.get(Feature, 'f').artifact_sha256 = 'a'*64
    pause_execution(engine, feature_id='f', expected_gate_version=1)
    roles = {role: dict(runtime='codex_cli', provider='openai', model=model, reasoning=reasoning,
        placement='home_mac', permission=permission, billing='subscription')
        for role, model, reasoning, permission in [('planner','gpt-6-astra','medium','read_only'),
            ('coder','gpt-5.6-sol','high','workspace_write'), ('reviewer','gpt-6-astra','medium','read_only')]}
    register_profile(engine, revision_id='B-1', profile='B', revision=1, roles=roles)
    selection = select_workflow(engine, feature_id='f', actor='operator', body=SelectionRequest(
        request_id='select', profile_revision_id='B-1', expected_feature_version=1, expected_gate_version=2))
    if same_snapshot:
        from personal_agent_dal.storage.machine_models import WorkflowAction
        with session_factory(engine)() as s, s.begin():
            s.get(WorkflowAction,a.action_id).execution_snapshot_sha256=selection['snapshot_sha256']
    proposal = propose(engine, feature_id='f', selection_id=selection['selection_id'], request_id='proposal')
    key = ec.generate_private_key(ec.SECP256R1())
    now = int(utc_now().timestamp())
    claims = dict(iss='pa-resume', aud='dal-resume', jti='jti', iat=now, exp=now+600,
        decision_id='decision', device_id='phone', subject_id='device:phone', key_thumbprint='a'*43,
        decision='approve_once', proposal_id=proposal['proposal_id'], binding_sha256=proposal['binding_sha256'])
    return a, proposal, key, claims


def approve(engine, key, claims):
    return import_decision(engine, assertion=sign_decision(claims, key=key, kid='resume'),
        keys={'resume': key.public_key()}, issuer='pa-resume', audience='dal-resume')


def test_resume_replay_and_old_leases_blocked(world):
    a, p, key, claims = setup_resume(world)
    approval = approve(world, key, claims)
    body = ResumeRequest(request_id='resume', approval_id=approval['approval_id'])
    receipt = resume(world, feature_id='f', body=body)
    world.dispose()
    assert resume(world, feature_id='f', body=body) == receipt
    assert claim_dispatch(world, attempt_id=receipt['new_attempt_id'], expected_version=1,
        owner_id='w', job_id='j', lease_id='l').code == 'REPLACEMENT_EPISODE_REQUIRED'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt, a.attempt_id).state == 'superseded'
        assert s.get(Approval, approval['approval_id']).consumed_by_command_id == 'resume'
        assert s.scalar(select(func.count()).select_from(ResumeReceipt)) == 1
        assert s.scalar(select(func.count()).select_from(DispatchIntent)) == 1
    assert cancel_execution(world, feature_id='f', expected_gate_version=3).code == 'CANCELLED'
    assert resume(world, feature_id='f', body=body) == receipt


@pytest.mark.parametrize('table', ['resume_receipts', 'dispatch_intents'])
def test_resume_rollback(world, table):
    a, p, key, claims = setup_resume(world)
    approval = approve(world, key, claims)
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO '+table):
            raise RuntimeError('storage-failed')
    event.listen(world, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='storage-failed'):
            resume(world, feature_id='f', body=ResumeRequest(request_id='r', approval_id=approval['approval_id']))
    finally:
        event.remove(world, 'before_cursor_execute', fail)
    with session_factory(world)() as s:
        assert s.get(ExecutionGate, 'f').mode == 'paused'
        assert s.get(Approval, approval['approval_id']).consumed_at is None
        assert s.get(ProviderAttempt, a.attempt_id).state == 'prepared'
        assert s.scalar(select(func.count()).select_from(DispatchIntent)) == 0


@pytest.mark.parametrize('change', ['cancel', 'artifact', 'version', 'revocation'])
def test_stale_approval_does_not_consume(world, change):
    a, p, key, claims = setup_resume(world)
    approval = approve(world, key, claims)
    if change == 'cancel':
        cancel_execution(world, feature_id='f', expected_gate_version=2)
    elif change == 'revocation':
        revoke_decision(world, decision_id='decision')
    else:
        with session_factory(world)() as s, s.begin():
            f = s.get(Feature, 'f')
            if change == 'artifact': f.artifact_sha256 = 'b'*64
            else: f.version += 1
    with pytest.raises(ValueError):
        resume(world, feature_id='f', body=ResumeRequest(request_id='r', approval_id=approval['approval_id']))
    with session_factory(world)() as s:
        assert s.get(Approval, approval['approval_id']).consumed_at is None
        assert s.scalar(select(func.count()).select_from(DispatchIntent)) == 0


@pytest.mark.parametrize('field,value', [('iss','wrong'), ('aud','wrong'), ('iat',True), ('exp',True),
    ('subject_id','device:forged'), ('decision','reject')])
def test_invalid_claims_never_approve(world, field, value):
    a,p,key,claims = setup_resume(world)
    claims[field] = value
    with pytest.raises(ValueError):
        approve(world,key,claims)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 0


def test_tombstone_before_import(world):
    a,p,key,claims = setup_resume(world)
    revoke_decision(world, decision_id='decision')
    with pytest.raises(ValueError): approve(world,key,claims)

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import jwt


def test_two_commands_one_approval(world):
    a,p,key,claims=setup_resume(world)
    approval=approve(world,key,claims)
    barrier=Barrier(2)
    def run(request):
        barrier.wait(timeout=5)
        try:return resume(world,feature_id='f',body=ResumeRequest(request_id=request,approval_id=approval['approval_id']))['receipt_id']
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,['one','two']))
    assert sum(r is not None for r in results)==1
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(DispatchIntent))==1


@pytest.mark.parametrize('mutation',['expiry','future','lifetime','kid','jku','tamper','algorithm','duplicate'])
def test_assertion_attack_shapes(world,mutation):
    a,p,key,claims=setup_resume(world)
    headers={'kid':'resume','typ':'JWT'}
    if mutation=='expiry':claims['exp']=int(utc_now().timestamp())
    if mutation=='future':claims['iat']+=60
    if mutation=='lifetime':claims['exp']=claims['iat']+901
    if mutation=='kid':headers['kid']='other'
    if mutation=='jku':headers['jku']='https://attacker.invalid/key'
    if mutation=='duplicate':
        approve(world,key,claims)
        claims['device_id']='other';claims['subject_id']='device:other'
    token=jwt.encode(claims,key if mutation!='algorithm' else b'synthetic-key-00000000000000000000',algorithm='ES256' if mutation!='algorithm' else 'HS256',headers=headers)
    if mutation=='tamper':token=token[:-8]+'AAAAAAAA'
    with pytest.raises(ValueError):
        import_decision(world,assertion=token,keys={'resume':key.public_key()},issuer='pa-resume',audience='dal-resume')


def test_unknown_without_manifest_is_blocked(world):
    a=create(world)
    claim(world,a)
    pause_execution(world,feature_id='f',expected_gate_version=1)
    from personal_agent_dal.machine.isolation_evidence import issue_challenge
    with pytest.raises(ValueError,match='MANIFEST_MISSING'):issue_challenge(world,attempt_id=a.attempt_id)


def test_two_approvals_cannot_replace_same_attempt(world):
    a,p,key,claims=setup_resume(world)
    first=approve(world,key,claims)
    second=approve(world,key,{**claims,'decision_id':'second','jti':'second'})
    receipt=resume(world,feature_id='f',body=ResumeRequest(request_id='one',approval_id=first['approval_id']))
    with pytest.raises(ValueError):resume(world,feature_id='f',body=ResumeRequest(request_id='two',approval_id=second['approval_id']))
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(DispatchIntent))==1
        assert s.get(Approval,second['approval_id']).consumed_at is None


def test_authority_blocks_destructive_downgrade(world):
    from personal_agent_dal.storage import db
    setup_resume(world)
    with pytest.raises(RuntimeError,match='authority exists'):db.downgrade(world,'0014')


def test_late_old_result_is_observation_only(world):
    from personal_agent_dal.machine.action_lifecycle import record_result
    from personal_agent_dal.storage.machine_models import ProviderResultObservation
    a,p,key,claims=setup_resume(world)
    approved=approve(world,key,claims)
    receipt=resume(world,feature_id='f',body=ResumeRequest(request_id='r',approval_id=approved['approval_id']))
    outcome=record_result(world,attempt_id=a.attempt_id,expected_version=1,owner_id='w',fence=1,digest='a'*64)
    assert outcome.code!='RESULT_RECORDED'
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt,receipt['new_attempt_id']).result_digest is None
        assert s.scalar(select(func.count()).select_from(ProviderResultObservation))==1


def test_profile_a_and_b_explicit_selection_never_dispatches(world):
    from personal_agent_dal.storage.machine_models import WorkflowProfileRevision,ExecutionSnapshot
    a,p,key,claims=setup_resume(world)
    with session_factory(world)() as s:
        import json
        roles=json.loads(s.get(WorkflowProfileRevision,'B-1').body)['roles']
    roles['coder'].update(runtime='claude_code',provider='synthetic',model='synthetic-domestic-coder',reasoning='high',billing='api')
    register_profile(world,revision_id='A-synthetic-1',profile='A',revision=1,roles=roles)
    selected=select_workflow(world,feature_id='f',actor='operator',body=SelectionRequest(request_id='choose-a',
        profile_revision_id='A-synthetic-1',expected_feature_version=1,expected_gate_version=2))
    assert selected['snapshot_sha256']!=p['binding']['snapshot_sha256']
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(DispatchIntent))==0
        assert s.scalar(select(func.count()).select_from(ExecutionSnapshot))==2
    with pytest.raises(ValueError):approve(world,key,claims)


@pytest.mark.parametrize('target,field,value',[
    ('gate','version',9),('action','version',9),('attempt','version',9),
    ('action','input_binding_sha256','b'*64),('action','execution_snapshot_sha256','c'*64),('action','active_attempt_id',None),
    ('snapshot','body','{}'),('selection','feature_id','other'),('selection','gate_version',9),
])
def test_every_current_binding_change_blocks_consumption(world,target,field,value):
    from personal_agent_dal.storage.machine_models import WorkflowAction, ExecutionSnapshot, WorkflowSelection
    a,p,key,claims=setup_resume(world)
    approved=approve(world,key,claims)
    with session_factory(world)() as s,s.begin():
        if target == 'selection' and field == 'feature_id':
            from tests.dal.factories import feature_row
            s.add(feature_row(feature_id='other', version=1, state='coding', now=utc_now()))
            s.flush()
        row={'gate':lambda:s.get(ExecutionGate,'f'), 'action':lambda:s.get(WorkflowAction,a.action_id),
             'attempt':lambda:s.get(ProviderAttempt,a.attempt_id),
             'snapshot':lambda:s.get(ExecutionSnapshot,p['binding']['snapshot_sha256']),
             'selection':lambda:s.get(WorkflowSelection,p['binding']['selection_id'])}[target]()
        setattr(row,field,value)
    with pytest.raises(ValueError):resume(world,feature_id='f',body=ResumeRequest(request_id='r',approval_id=approved['approval_id']))
    with session_factory(world)() as s:
        assert s.get(Approval,approved['approval_id']).consumed_at is None
        assert s.scalar(select(func.count()).select_from(DispatchIntent))==0


def test_unchanged_snapshot_retains_action(world):
    from personal_agent_dal.storage.machine_models import WorkflowAction,ReplacementBudget
    a,p,key,claims=setup_resume(world,same_snapshot=True)
    approved=approve(world,key,claims)
    r=resume(world,feature_id='f',body=ResumeRequest(request_id='r',approval_id=approved['approval_id']))
    with session_factory(world)() as s:
        assert s.get(ProviderAttempt,r['new_attempt_id']).action_id==a.action_id
        assert s.get(ReplacementBudget,a.attempt_id).new_action_id==a.action_id


def test_cancel_resume_race_never_leaves_cancelled_authority_open(world):
    a,p,key,claims=setup_resume(world)
    approved=approve(world,key,claims)
    barrier=Barrier(2)
    def go(kind):
        barrier.wait(timeout=5)
        if kind=='cancel':return cancel_execution(world,feature_id='f',expected_gate_version=2).code
        try:return resume(world,feature_id='f',body=ResumeRequest(request_id='r',approval_id=approved['approval_id']))['code']
        except ValueError:return 'REFUSED'
    with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(go,['cancel','resume']))
    with session_factory(world)() as s:
        gate=s.get(ExecutionGate,'f')
        if 'CANCELLED' in codes:
            assert gate.mode=='cancelled'
            assert s.get(Approval,approved['approval_id']).consumed_at is None
        else:
            assert 'RESUMED' in codes and gate.mode=='open'
            assert codes[0]=='EXECUTION_GATE_STALE'


def test_proposal_requests_each_have_durable_identity(world):
    a,p,key,claims=setup_resume(world)
    second=propose(world,feature_id='f',selection_id=p['binding']['selection_id'],request_id='second-proposal')
    assert second['proposal_id']!=p['proposal_id']
    assert second['binding_sha256']==p['binding_sha256']


def test_expired_proposal_can_be_renewed_without_reselection(world):
    from personal_agent_dal.storage.machine_models import ResumeProposal
    a,p,key,claims=setup_resume(world)
    with session_factory(world)() as s,s.begin():
        s.get(ResumeProposal,p['proposal_id']).expires_at=utc_now()-timedelta(seconds=1)
    second=propose(world,feature_id='f',selection_id=p['binding']['selection_id'],request_id='renewed-proposal')
    assert second['proposal_id']!=p['proposal_id']


def test_operator_can_resolve_delivered_decision_then_resume(world):
    from fastapi.testclient import TestClient
    from personal_agent_dal.service.app import create_app
    from personal_agent_dal.service.operator_tokens import issue_operator_token
    a,p,key,claims=setup_resume(world)
    approved=approve(world,key,claims)
    service_key=b'synthetic-operator-key'
    client=TestClient(create_app(world,service_key=service_key,enrollment_secret=b'synthetic-enrollment',
        resume_config={'issuer':'pa-resume','audience':'dal-resume','keys':{'resume':key.public_key()},'profiles':[]}))
    token=issue_operator_token(key=service_key,operator_id='operator',capabilities=['read','control'],
        expires_at_epoch=int(utc_now().timestamp())+600)
    headers={'Authorization':'Bearer '+token}
    found=client.get('/operator/human-decisions/decision',headers=headers)
    assert found.status_code==200
    assert found.json()['approval_id']==approved['approval_id']
    r=client.post('/operator/features/f/resume',headers=headers,json={'request_id':'operator-resume','approval_id':found.json()['approval_id']})
    assert r.status_code==200 and r.json()['code']=='RESUMED'
    assert client.get('/operator/human-decisions/decision').status_code==401
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(DispatchIntent))==1
