"""Failure-first contracts for real SQLite authorization, no model grants."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, text
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow, DevelopmentGate, DevelopmentRequest, DevelopmentProjectAuthorization
from tests.dal.test_timeline_requests import world, submit


def setup_case(world):
    from personal_agent_dal.timeline.phone_authorization import ProjectAuthorizationService, ProjectTemplate
    r=world[2];request=submit(r);rid=request['request_id']
    with r.sessions() as s,s.begin():
        wf=s.scalar(select(DevelopmentWorkflow).where(DevelopmentWorkflow.request_id==rid))
        wf.phase='project_routing';wf.status='blocked';wf.blocker_reason='PROJECT_AUTHORIZATION_REQUIRED'
        if s.get(DevelopmentGate,wf.workflow_id) is None:s.add(DevelopmentGate(workflow_id=wf.workflow_id,mode='open',epoch=1,version=1))
    service=ProjectAuthorizationService(r)
    template=ProjectTemplate(project_id='synthetic-project',revision=1,display_name='Synthetic isolated project',kind='existing',
        root='/synthetic/project',remote_repository='synthetic/repo',allowed_actions=['read','write','push','pr'],
        registration_policies=['local_tracker'],max_budget_seconds=3600,max_validity_seconds=86400,
        allow_subjects=['device:synthetic','device:second'],worker_id='synthetic-worker',worker_configuration_digest='a'*64,
        directory_identity_digest='b'*64,base_sha='c'*40,base_branch='main',budget_policy_ref='existing-budget')
    service.register_template(template,actor='operator',observed_at=r.now(),expires_at=r.now()+timedelta(hours=1),evidence_digest='d'*64)
    state=service.read(rid,subject='device:synthetic')
    payload=dict(request_id=rid,operation='create',project_id=template.project_id,template_revision=1,
        template_digest=state['candidates'][0]['template_digest'],requested_actions=['read'],budget_seconds=1000,
        grant_expires_at=(r.now()+timedelta(hours=1)).isoformat(),expected=state['expected'],expected_grant=None)
    return r,service,rid,payload


def preview(service,payload,command_id='preview',subject='device:synthetic'):
    return service.preview(command_id=command_id,subject=subject,payload=payload)


def approve(service,proposal,command_id='approve',**overrides):
    payload=dict(proposal_id=proposal['proposal_id'],binding_digest=proposal['binding_digest'],
        source_action_ref='action',context_id='context',context_jti='nonce',key_thumbprint='synthetic-thumb',
        confirmed_at=service.r.now().isoformat(),confirmation_expires_at=(service.r.now()+timedelta(minutes=10)).isoformat())
    payload.update(overrides)
    return service.approve(command_id=command_id,subject='device:synthetic',payload=payload)


def test_preview_cannot_grant_and_approve_replays_after_expiry(world):
    r,service,rid,payload=setup_case(world)
    result=preview(service,payload)
    assert result['status']=='accepted'
    with r.sessions() as s:assert s.scalar(select(DevelopmentProjectAuthorization)) is None
    proposal=result['proposal'];assert proposal['scope']['root']=='/synthetic/project'
    when=r.now();r.now=lambda:when
    first=approve(service,proposal)
    assert first['status']=='accepted' and first['authorization_applied'] is True
    r.now=lambda:when+timedelta(days=2)
    # Repeat the identical original command, not a freshly timestamped approval.
    again=approve(service,proposal,confirmed_at=when.isoformat(),confirmation_expires_at=(when+timedelta(minutes=10)).isoformat())
    assert again==first
    with r.sessions() as s:assert len(list(s.scalars(select(DevelopmentProjectAuthorization))))==1


@pytest.mark.parametrize('change',[
    {'root':'/other'}, {'requested_actions':['read','remote_issue']}, {'budget_seconds':True},
    {'requested_actions':['read','pr']},{'budget_seconds':4000},{'requested_actions':['read','read']},
])
def test_malicious_or_inconsistent_scope_never_creates_grant(world,change):
    r,service,_,payload=setup_case(world);payload.update(change)
    result=preview(service,payload)
    assert result['status']=='refused'
    with r.sessions() as s:assert s.scalar(select(DevelopmentProjectAuthorization)) is None


def test_old_preview_and_foreign_subject_are_refused(world):
    r,service,rid,payload=setup_case(world)
    first=preview(service,payload)['proposal']
    payload['expected']=service.read(rid,subject='device:synthetic')['expected']
    preview(service,payload,command_id='new-preview')
    assert approve(service,first)['status']=='refused'
    assert service.read(rid,subject='device:unknown')['candidates']==[]
    assert preview(service,payload,command_id='foreign',subject='device:unknown')['status']=='refused'


def test_preview_concurrency_has_one_current_generation(world):
    _,service,_,payload=setup_case(world)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda i:preview(service,payload,command_id='p'+str(i)),range(2)))
    assert sorted(x['status'] for x in results)==['accepted','refused']


def test_expired_pending_reopens_same_workflow(world):
    from personal_agent_dal.storage.timeline_models import DevelopmentAuthorizationRequest
    r,service,rid,payload=setup_case(world)
    preview(service,payload)
    with r.sessions() as s,s.begin():
        pending=s.scalar(select(DevelopmentAuthorizationRequest));pending.status='expired';pending.expires_at=r.now()-timedelta(hours=1)
    payload['expected']=service.read(rid,subject='device:synthetic')['expected']
    result=approve(service,preview(service,payload,'reopen')['proposal'])
    assert result['status']=='accepted'
    with r.sessions() as s:
        assert s.scalar(select(DevelopmentAuthorizationRequest)).status=='granted'
        assert len(list(s.scalars(select(DevelopmentWorkflow))))==1


def test_changed_body_same_command_does_not_overwrite_original(world):
    _,service,_,payload=setup_case(world)
    original=preview(service,payload)
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        preview(service,dict(payload,budget_seconds=999))
    assert preview(service,payload)==original


def test_first_approval_after_confirmation_expiry_refused(world):
    r,service,_,payload=setup_case(world)
    proposal=preview(service,payload)['proposal']
    result=approve(service,proposal,confirmed_at=(r.now()-timedelta(hours=1)).isoformat(),confirmation_expires_at=(r.now()-timedelta(minutes=1)).isoformat())
    assert result['status']=='refused'
    with r.sessions() as s:assert s.scalar(select(DevelopmentProjectAuthorization)) is None


def test_template_revocation_invalidates_proposal(world):
    _,service,_,payload=setup_case(world)
    proposal=preview(service,payload)['proposal']
    service.disable_template('synthetic-project',expected_revision=1,actor='operator')
    assert approve(service,proposal)['status']=='refused'


def test_amend_rebinds_pending_document_and_preserves_pause(world):
    from tests.dal.test_timeline_artifacts import source
    from personal_agent_dal.timeline.artifacts import ArtifactService
    from personal_agent_dal.timeline.decisions import DecisionService
    from personal_agent_dal.storage.timeline_models import DevelopmentDecisionRequest as Decision
    r,service,rid,payload=setup_case(world)
    first=approve(service,preview(service,payload)['proposal'])
    sha=source(r,rid,'Synthetic approved scope')
    artifact=ArtifactService(r).record(step_id='step',expected_result_digest=sha)
    with r.sessions() as s,s.begin():
        wf=s.get(DevelopmentWorkflow,rid);wf.status='active';wf.blocker_reason=None
    old=DecisionService(r).propose(rid,artifact,kind='prd')
    with r.sessions() as s,s.begin():
        wf=s.get(DevelopmentWorkflow,rid);wf.status='paused'
        gate=s.get(DevelopmentGate,rid);gate.mode='paused';gate.version+=1;gate.epoch+=1
    state=service.read(rid,subject='device:synthetic')
    payload.update(operation='amend',requested_actions=['read','write'],expected=state['expected'],
        expected_grant=dict(id=first['grant_id'],version=first['grant_version'],digest=first['grant_digest']))
    second=approve(service,preview(service,payload,'amend-preview')['proposal'],'amend-approve')
    assert second['status']=='accepted' and not second['workflow_resumed']
    assert second['grant_id']==first['grant_id'] and second['grant_version']==2
    with r.sessions() as s:
        assert s.get(Decision,old['decision_id']).status=='superseded'
        replacement=s.scalar(select(Decision).where(Decision.status=='pending'))
        binding=r._open(Decision,replacement.decision_id,'sealed_binding',replacement.sealed_binding)
        assert binding['artifact_id']==artifact
        assert binding['workflow_version']==s.get(DevelopmentWorkflow,rid).version
        assert s.get(DevelopmentWorkflow,rid).status=='paused'
        assert s.get(DevelopmentGate,rid).mode=='paused'


@pytest.mark.parametrize('mutation',['disabled','budget','root','actions','revoked'])
def test_current_execution_policy_rechecks_authority(world,mutation):
    from personal_agent_dal.timeline.phone_authorization import execution_policy
    r,service,_,payload=setup_case(world)
    receipt=approve(service,preview(service,payload)['proposal'])
    with r.sessions() as s:
        grant=s.get(DevelopmentProjectAuthorization,receipt['grant_id'])
        assert execution_policy(r,s,grant)['base_sha']=='c'*40
    if mutation=='disabled':service.disable_template('synthetic-project',actor='operator',expected_revision=1)
    else:
        with r.sessions() as s,s.begin():
            grant=s.get(DevelopmentProjectAuthorization,receipt['grant_id'])
            if mutation=='revoked':grant.revoked=1
            else:
                value=r._open(DevelopmentProjectAuthorization,grant.grant_id,'sealed_grant',grant.sealed_grant)
                if mutation=='budget':value['budget_seconds']=4000
                if mutation=='root':value['root']='/other'
                if mutation=='actions':value['actions']=['read','remote_issue']
                grant.digest=digest(value)
                grant.sealed_grant=r._seal(DevelopmentProjectAuthorization,grant.grant_id,'sealed_grant',value)
    with r.sessions() as s:
        with pytest.raises(ValueError,match='PROJECT_AUTHORIZATION_REQUIRED'):
            execution_policy(r,s,s.get(DevelopmentProjectAuthorization,receipt['grant_id']))


def test_migration_refuses_to_discard_proposal_history(world):
    from personal_agent_dal.storage import db
    _,service,_,payload=setup_case(world)
    preview(service,payload)
    with pytest.raises(RuntimeError,match='Authorization history exists'):db.downgrade(world[0],'0022')


def test_expired_template_does_not_hide_grant_needed_for_renewal(world):
    r,service,rid,payload=setup_case(world)
    first=approve(service,preview(service,payload)['proposal'])
    now=r.now();r.now=lambda:now+timedelta(hours=2)
    state=service.read(rid,subject='device:synthetic')
    assert state['candidates']==[]
    assert [g['grant_id'] for g in state['grants']]==[first['grant_id']]


def test_policy_fence_keeps_signed_result_observable_without_advancing(world,monkeypatch):
    from tests.dal.workflow_signer import SignedDriver
    from tests.dal.test_timeline_roles import config,registry
    from personal_agent_dal.timeline.roles import RoleService
    from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step,DevelopmentExecution as Execution
    r=world[2];body=config();roles=RoleService(r,registry(body))
    revision=roles.register(body);roles.bind(scope='system',scope_id='default',revision_id=revision,expected_version=0)
    driver=SignedDriver(r,roles=roles);rid=submit(r)['request_id'];prepared=driver.tick(rid)
    with r.sessions() as s,s.begin():
        step=s.get(Step,prepared['step_id'])
        inputs=r._open(Step,step.step_id,'sealed_input',step.sealed_input)
        inputs['project_policy']={'worker_id':'synthetic'}
        step.input_digest=digest(inputs);step.sealed_input=r._seal(Step,step.step_id,'sealed_input',inputs)
        admission=dict(step_id=step.step_id,input_digest=step.input_digest,snapshot_id=step.snapshot_id,
            gate_epoch=step.gate_epoch,worker_id='synthetic',receipt_digest='a'*64)
    launch=driver.dispatch(prepared['step_id'],admission=admission)
    def policy_revoked(*args):raise ValueError('PROJECT_AUTHORIZATION_REQUIRED')
    monkeypatch.setattr(driver,'_current',policy_revoked)
    result=dict(kind='clarification',text='Synthetic requirement',ready=True,questions=[],acceptance=['A'])
    receipt=driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=result)
    assert receipt['status']=='retired'
    assert driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=result)==receipt
    with r.sessions() as s:
        assert s.get(Execution,launch['attempt_id']).receipt_digest is not None
        assert s.get(DevelopmentWorkflow,rid).phase=='clarify'
        assert s.get(DevelopmentWorkflow,rid).status=='blocked'


@pytest.mark.parametrize('operation,old_remote,phase,accepted', [
    ('amend', None, 'project_routing', True),
    ('renew', None, 'project_routing', False),
    ('amend', 'synthetic/repo', 'project_routing', False),
    ('amend', None, 'research', False),
])
def test_remote_first_attachment_requires_phone_amend_before_routing(world, operation, old_remote, phase, accepted):
    from personal_agent_dal.storage.timeline_models import DevelopmentProjectTemplate as Template
    from personal_agent_dal.timeline.authorization_contracts import ProjectTemplate
    r, service, rid, payload = setup_case(world)
    with r.sessions() as s:
        row = s.scalar(select(Template))
        value = r._open(Template, row.template_id, 'sealed_template', row.sealed_template)
    value.update(revision=2, remote_repository=old_remote, allowed_actions=['read', 'write'])
    service.register_template(ProjectTemplate(**value), actor='operator', observed_at=r.now(), expires_at=r.now()+timedelta(hours=1), evidence_digest='e'*64)
    state = service.read(rid, subject='device:synthetic')
    payload.update(template_revision=2, template_digest=state['candidates'][0]['template_digest'], expected=state['expected'])
    original = approve(service, preview(service, payload)['proposal'])
    value.update(revision=3, remote_repository='synthetic/new-repo', max_budget_seconds=None, max_validity_seconds=None)
    service.register_template(ProjectTemplate(**value), actor='operator', observed_at=r.now(), expires_at=None, evidence_digest='f'*64)
    with r.sessions() as s, s.begin():
        s.get(DevelopmentWorkflow, rid).phase = phase
    state = service.read(rid, subject='device:synthetic')
    payload.update(operation=operation, template_revision=3, template_digest=state['candidates'][0]['template_digest'],
        expected=state['expected'], budget_seconds=None, grant_expires_at=None,
        expected_grant=dict(id=original['grant_id'], version=1, digest=original['grant_digest']))
    result = preview(service, payload, 'attach-preview')
    assert (result['status']=='accepted') is accepted
    with r.sessions() as s:
        grant=s.get(DevelopmentProjectAuthorization, original['grant_id'])
        assert r._open(DevelopmentProjectAuthorization, grant.grant_id, 'sealed_grant', grant.sealed_grant)['remote_repository']==old_remote
    if accepted:
        result=approve(service, result['proposal'], 'attach-approve')
        assert result['authorization_applied'] and result['grant_id']==original['grant_id']
