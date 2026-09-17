"""Driver adversaries are enumerated before the implementation."""
import pytest
from sqlalchemy import select
from tests.dal.test_timeline_requests import world, submit
from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step, DevelopmentWorkflow as Workflow
from tests.dal.workflow_signer import SignedDriver as WorkflowDriver


@pytest.mark.parametrize('result', [None, {}, {'kind':'prd','text':''}, {'kind':'prd','text':'x','extra':True},
    {'kind':'prd','text':'api_key=synthetic-secret'}, {'kind':'research','text':'unexpected phase'},
    [{'kind':'prd','text':'one'},{'kind':'prd','text':'two'}]])
def test_unbound_or_malformed_result_never_advances(world, result):
    driver=WorkflowDriver(world[2], roles=None)
    with pytest.raises(ValueError):driver.accept('missing', attempt_id='attempt', result=result)


def test_role_unavailable_is_durable_blocker_not_fake_progress(world):
    requests=world[2];wf=submit(requests)['request_id']
    driver=WorkflowDriver(requests, roles=None)
    assert driver.tick(wf)['status']=='blocked'
    with requests.sessions() as s:
        row=s.get(Workflow,wf)
        assert row.status=='blocked' and row.phase=='clarify'
        assert not list(s.scalars(select(Step)))
    assert driver.tick(wf)['status']=='blocked'


def configured(world):
    from tests.dal.test_timeline_roles import config,registry
    from personal_agent_dal.timeline.roles import RoleService
    r=world[2];body=config();roles=RoleService(r,registry(body))
    rev=roles.register(body);roles.bind(scope='system',scope_id='default',revision_id=rev,expected_version=0)
    driver=WorkflowDriver(r,roles=roles);wf=submit(r)['request_id']
    prepared=driver.tick(wf)
    with r.sessions() as s:
        step=s.get(Step,prepared['step_id'])
        admission=dict(step_id=step.step_id,input_digest=step.input_digest,snapshot_id=step.snapshot_id,gate_epoch=step.gate_epoch,worker_id='synthetic',receipt_digest='a'*64)
    launched=driver.dispatch(prepared['step_id'],admission=admission)
    return r,driver,wf,launched


def test_lost_result_cannot_dispatch_again_and_reconciles_same_attempt(world):
    r,driver,wf,launch=configured(world)
    driver.reconcile_required(launch['step_id'])
    assert driver.tick(wf)['status']=='result_unknown'
    with pytest.raises(ValueError):driver.dispatch(launch['step_id'],admission={})
    result=dict(kind='clarification',text='Synthetic requirement',ready=True,questions=[],acceptance=['A'])
    receipt=driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=result)
    assert driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=result)==receipt
    with r.sessions() as s:assert s.get(Workflow,wf).phase=='project_routing'


@pytest.mark.parametrize('attack',['empty','extra','secret','wrong_phase','condition','wrong_attempt'])
def test_real_claim_rejects_bad_result_without_transition(world,attack):
    r,driver,wf,launch=configured(world)
    result=dict(kind='clarification',text='Synthetic requirement',ready=True,questions=[],acceptance=['A'])
    attempt=launch['attempt_id']
    if attack=='empty':result['text']=''
    if attack=='extra':result['extra']='injected'
    if attack=='secret':result['text']='api_key=synthetic-secret'
    if attack=='wrong_phase':result['kind']='prd'
    if attack=='condition':result['questions']=['Still ambiguous']
    if attack=='wrong_attempt':attempt='foreign'
    with pytest.raises(ValueError):driver.accept(launch['step_id'],attempt_id=attempt,result=result)
    with r.sessions() as s:
        assert s.get(Workflow,wf).phase=='clarify'
        assert s.get(Step,launch['step_id']).status=='dispatch_started'


@pytest.mark.parametrize('phase,result',[
    ('researching',dict(kind='research',text='Synthetic',sources='not-a-list',unknowns=[],workspace_receipt_digest='a'*64)),
    ('researching',dict(kind='research',text='Synthetic',sources=[],unknowns=[],workspace_receipt_digest='invented')),
    ('design_authoring',dict(kind='design',text='Synthetic',prd_digest='a'*64,plan=None)),
    ('design_authoring',dict(kind='design',text='Synthetic',prd_digest='a'*64,plan={})),
    ('design_review',dict(kind='review',text='Synthetic',reviewed_artifact_id=[],reviewed_digest='x',verdict='PASS',findings=[])),
    ('delivery_revision_planning',dict(kind='revision_plan',text='Synthetic',stages=[],scope_changed='false')),
])
def test_nested_provider_shapes_fail_before_publication(phase,result):
    from personal_agent_dal.timeline.driver import validate_result
    with pytest.raises(ValueError):validate_result(phase,result)


def test_next_author_receives_complete_artifact_and_human_change_feedback(world):
    from tests.dal.test_timeline_decisions import routed
    from personal_agent_dal.timeline.decisions import DecisionService
    from personal_agent_dal.storage.timeline_models import DevelopmentArtifact as Artifact, DevelopmentDriverStep as Step
    from tests.dal.test_timeline_artifacts import source
    from personal_agent_dal.timeline.artifacts import ArtifactService
    from personal_agent_dal.timeline.roles import RoleService
    from tests.dal.test_timeline_roles import config,registry
    r,wf,command=routed(world)
    DecisionService(r).consume(**command)
    from tests.dal.stage_fixtures import workspace
    workspace(r,wf)
    sha=source(r,wf,'Synthetic PRD complete text')
    artifact=ArtifactService(r).record(step_id='step',expected_result_digest=sha)
    proposal=DecisionService(r).propose(wf,artifact,kind='prd')
    DecisionService(r).consume(command_id='changes',source_message_ref='changes-message',subject='device:synthetic',
        decision_id=proposal['decision_id'],binding_digest=proposal['binding_digest'],text='修改：保留完整意见并补充验收指标')
    driver=WorkflowDriver(r,roles=RoleService(r,registry(config())))
    prepared=driver.tick(wf)
    with r.sessions() as s:
        step=s.get(Step,prepared['step_id'])
        inputs=r._open(Step,step.step_id,'sealed_input',step.sealed_input)
        assert next(a for a in inputs['artifacts'] if a['artifact_id']==artifact)['body']['text']=='Synthetic PRD complete text'
        assert inputs['human_feedback'][-1]['feedback']=='保留完整意见并补充验收指标'
        assert '保留完整意见' not in str(step.sealed_input)


def test_reconfigured_reviewer_cannot_review_prior_author_same_model(world):
    from tests.dal.test_timeline_decisions import routed
    from personal_agent_dal.timeline.decisions import DecisionService
    from personal_agent_dal.timeline.artifacts import ArtifactService
    from personal_agent_dal.timeline.roles import RoleService
    from personal_agent_dal.storage.timeline_models import DevelopmentRoleSnapshot as Snapshot
    from tests.dal.test_timeline_roles import config,registry
    from tests.dal.test_timeline_stages import plan
    from personal_agent_dal.timeline.requests import digest
    r,wf,command=routed(world);DecisionService(r).consume(**command)
    from tests.dal.stage_fixtures import workspace
    workspace(r,wf)
    body=config();roles=RoleService(r,registry(body));author=roles.snapshot(workflow_id=wf)
    result=dict(kind='design',text='Synthetic design',prd_digest='a'*64,plan=plan())
    with r.sessions() as s,s.begin():
        row=s.get(Workflow,wf);row.phase='design_review';row.version+=1
        s.add(Step(step_id='design-source',workflow_id=wf,phase='design_authoring',input_digest='a'*64,
            sealed_input=r._seal(Step,'design-source','sealed_input',{}),snapshot_id=author['snapshot_id'],
            expected_version=row.version-1,gate_epoch=1,cycle=1,status='completed',attempt_id='design-attempt',
            result_digest=digest(result),sealed_result=r._seal(Step,'design-source','sealed_result',result)))
    ArtifactService(r).record(step_id='design-source',expected_result_digest=digest(result))
    changed=config('new-planner-model');changed['revision']=2
    changed['roles']['reviewer']['model']=body['roles']['planner']['model']
    replacement=RoleService(r,registry(changed));revision=replacement.register(changed)
    replacement.bind(scope='system',scope_id='default',revision_id=revision,expected_version=1)
    outcome=WorkflowDriver(r,roles=replacement).tick(wf)
    assert outcome['status']=='blocked' and outcome['reason']=='REVIEW_NOT_INDEPENDENT'


@pytest.mark.parametrize('boundary',['dispatch','accept'])
def test_project_revocation_fences_prepared_and_inflight_steps(world,boundary):
    from tests.dal.test_timeline_decisions import routed
    from personal_agent_dal.timeline.decisions import DecisionService
    from personal_agent_dal.timeline.roles import RoleService
    from personal_agent_dal.storage.timeline_models import DevelopmentProjectAuthorization as Grant
    from tests.dal.test_timeline_roles import config,registry
    r,wf,command=routed(world);DecisionService(r).consume(**command)
    from tests.dal.stage_fixtures import workspace
    workspace(r,wf)
    with r.sessions() as s,s.begin():
        row=s.get(Workflow,wf);row.phase='prd_authoring';row.version+=1
    driver=WorkflowDriver(r,roles=RoleService(r,registry(config())))
    prepared=driver.tick(wf)
    with r.sessions() as s:
        step=s.get(Step,prepared['step_id'])
        admission=dict(step_id=step.step_id,input_digest=step.input_digest,snapshot_id=step.snapshot_id,
            gate_epoch=step.gate_epoch,worker_id='synthetic',receipt_digest='a'*64)
    launch=driver.dispatch(prepared['step_id'],admission=admission) if boundary=='accept' else None
    with r.sessions() as s,s.begin():s.get(Grant,'grant').revoked=1
    with pytest.raises(ValueError,match='PROJECT_AUTHORIZATION_REQUIRED'):
        if boundary=='dispatch':driver.dispatch(prepared['step_id'],admission=admission)
        else:driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=dict(kind='prd',text='Synthetic'))
    with r.sessions() as s:
        assert s.get(Workflow,wf).phase=='prd_authoring'
        assert s.get(Step,prepared['step_id']).status==('dispatch_started' if launch else 'prepared')


def test_missing_project_authority_is_durable_and_recovers_after_explicit_grant(world):
    from personal_agent_dal.storage.timeline_models import DevelopmentAuthorizationRequest
    from personal_agent_dal.timeline.operator import Authorization,register_authorization
    from datetime import timedelta
    r,driver,wf,item=configured(world)
    driver.accept(item['step_id'],attempt_id=item['attempt_id'],result=dict(kind='clarification',text='Synthetic',ready=True,questions=[],acceptance=['a']))
    assert driver.tick(wf)['reason']=='PROJECT_AUTHORIZATION_REQUIRED'
    with r.sessions() as s:
        assert s.get(DevelopmentAuthorizationRequest,wf).status=='pending'
    grant=Authorization(grant_id='approved',request_id=wf,project_id='local',subject='device:synthetic',approval_evidence_ref='explicit-user-approval',
        root='/synthetic',kind='local_new',display_name='Local',actions=['read','write','create','local_init'],budget_seconds=600,
        expires_at=r.now()+timedelta(hours=1),registration_policy='local_tracker')
    register_authorization(r,grant,actor='operator')
    result=driver.tick(wf)
    assert result['status']=='prepared'
    with r.sessions() as s:assert s.get(DevelopmentAuthorizationRequest,wf).status=='granted'
