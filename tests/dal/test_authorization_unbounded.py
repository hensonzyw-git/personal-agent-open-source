"""Personal project authorization preserves explicit limits and legacy grants."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from tests.dal.test_timeline_requests import world
from tests.dal.test_phone_authorization import setup_case, preview, approve
from personal_agent_dal.timeline.authorization_contracts import ProjectTemplate, Preview
from personal_agent_dal.storage.timeline_models import DevelopmentProjectTemplate as Template, DevelopmentProjectAuthorization as Grant
from personal_agent_dal.timeline.authorization_limits import extends_limit, within_limit


def unlimited_case(world):
    r,service,rid,payload=setup_case(world)
    with r.sessions() as s:
        row=s.scalar(select(Template))
        value=r._open(Template,row.template_id,'sealed_template',row.sealed_template)
    value.update(revision=2,max_budget_seconds=None,max_validity_seconds=None)
    service.register_template(ProjectTemplate.model_validate(value),actor='operator',observed_at=r.now(),
        expires_at=r.now()+timedelta(hours=23),evidence_digest='e'*64)
    state=service.read(rid,subject='device:synthetic')
    payload.update(template_revision=2,template_digest=state['candidates'][0]['template_digest'],
        budget_seconds=None,grant_expires_at=None,expected=state['expected'])
    return r,service,rid,payload


def remoteless_case(world):
    """Unbounded grant whose template carries no remote repository yet."""
    r,service,rid,payload=setup_case(world)
    with r.sessions() as s:
        row=s.scalar(select(Template))
        value=r._open(Template,row.template_id,'sealed_template',row.sealed_template)
    # push/pr would force a remote repository in the template contract.
    value.update(revision=2,remote_repository=None,allowed_actions=['read','write'],
        max_budget_seconds=None,max_validity_seconds=None)
    service.register_template(ProjectTemplate.model_validate(value),actor='operator',observed_at=r.now(),
        expires_at=r.now()+timedelta(hours=23),evidence_digest='e'*64)
    state=service.read(rid,subject='device:synthetic')
    payload.update(template_revision=2,template_digest=state['candidates'][0]['template_digest'],
        budget_seconds=None,grant_expires_at=None,expected=state['expected'])
    return r,service,rid,payload


def test_unbounded_grant_has_null_scope_not_fake_display_date(world):
    r,service,rid,payload=unlimited_case(world)
    proposal=preview(service,payload)['proposal']
    assert proposal['scope']['expires_at'] is None
    assert approve(service,proposal)['status']=='accepted'
    state=service.read(rid,subject='device:synthetic')
    assert state['grants'][0]['expires_at'] is None
    assert state['grants'][0]['scope']['budget_seconds'] is None
    with r.sessions() as s:
        grant=s.scalar(select(Grant));assert grant.expires_at.year==9999
        body=r._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)
        assert body['expires_at'] is None


@pytest.mark.parametrize('field',['budget_seconds','grant_expires_at'])
def test_finite_template_rejects_unbounded(world,field):
    _,service,_,payload=setup_case(world);payload[field]=None
    assert preview(service,payload)['reason']=='BUDGET_LIMIT_EXCEEDED'


@pytest.mark.parametrize('field',['budget_seconds','grant_expires_at'])
def test_omission_is_not_infinite_authority(world,field):
    _,_,_,payload=unlimited_case(world);payload.pop(field)
    with pytest.raises(ValueError):Preview.model_validate(payload)


@pytest.mark.parametrize('value',[True,0,-1,'infinite'])
def test_invalid_budget_rejected(world,value):
    _,_,_,payload=unlimited_case(world);payload['budget_seconds']=value
    with pytest.raises(ValueError):Preview.model_validate(payload)


def test_unbounded_cannot_silently_shrink():
    assert extends_limit(None,600) and extends_limit(None,None)
    assert not extends_limit(600,None)
    assert not within_limit(None,600)


def test_long_lived_template_and_grant_still_enforce_revocation(world):
    from personal_agent_dal.timeline.phone_authorization import execution_policy
    r,service,rid,payload=unlimited_case(world)
    with r.sessions() as s:
        row=s.scalar(select(Template).where(Template.active==1))
        value=r._open(Template,row.template_id,'sealed_template',row.sealed_template)
    service.register_template(ProjectTemplate.model_validate(value),actor='operator',observed_at=r.now(),expires_at=None,evidence_digest='f'*64)
    granted=approve(service,preview(service,payload)['proposal'])
    now=r.now();r.now=lambda:now+timedelta(days=40)
    with r.sessions() as s,s.begin():
        grant=s.get(Grant,granted['grant_id'])
        assert execution_policy(r,s,grant)['project_id']=='synthetic-project'
        grant.revoked=1
        with pytest.raises(ValueError,match='PROJECT_AUTHORIZATION_REQUIRED'):execution_policy(r,s,grant)


def test_active_quiescent_phone_upgrade_keeps_same_grant(world):
    from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow as Workflow
    r,service,rid,payload=unlimited_case(world)
    finite=dict(payload,budget_seconds=1000,grant_expires_at=(r.now()+timedelta(hours=1)).isoformat())
    old=approve(service,preview(service,finite)['proposal'])
    with r.sessions() as s,s.begin():
        wf=s.get(Workflow,rid);wf.status='active';wf.blocker_reason=None
    state=service.read(rid,subject='device:synthetic')
    payload.update(operation='renew',expected=state['expected'],
        expected_grant=dict(id=old['grant_id'],version=old['grant_version'],digest=old['grant_digest']))
    updated=approve(service,preview(service,payload,'upgrade-preview')['proposal'],'upgrade-approve')
    assert updated['status']=='accepted' and updated['grant_id']==old['grant_id']
    assert updated['grant_version']==2


@pytest.mark.parametrize('paused',[False,True])
def test_phone_project_choice_is_reused_but_pause_preserved(world,paused):
    from personal_agent_dal.timeline.projects import catalog,bind_phone_choice
    from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow as Workflow,DevelopmentGate as Gate,DevelopmentArtifact as Artifact,DevelopmentProjectBinding as Binding
    r,service,rid,payload=unlimited_case(world)
    approve(service,preview(service,payload)['proposal'])
    with r.sessions() as s,s.begin():
        wf=s.get(Workflow,rid);wf.status='paused' if paused else 'active'
        gate=s.get(Gate,rid);gate.mode='paused' if paused else 'open'
        s.add(Artifact(artifact_id='route',workflow_id=rid,kind='project_route',revision=1,
            body_sha256='a'*64,sealed_body=r._seal(Artifact,'route','sealed_body',{}),source_step_id='step',source_receipt_digest='b'*64));s.flush()
        assert bind_phone_choice(r,s,wf,'route',catalog(r,s,rid)) is not paused
        s.flush()
        assert (s.get(Binding,rid) is None)==paused
        assert wf.phase==('project_routing' if paused else 'project_registration')


def granted_invalidation_event(r,rid):
    from personal_agent_dal.storage.timeline_models import DevelopmentEvent as Event,DevelopmentRequest as Request
    with r.sessions() as s:
        request=s.scalar(select(Request).where(Request.request_id==rid))
        events=list(s.scalars(select(Event).where(Event.kind=='workflow.authorization_granted').order_by(Event.seq)))
        assert events
        return r._open(Event,events[-1].event_id,'sealed_body',events[-1].sealed_body)


@pytest.mark.parametrize('paused',[False,True])
def test_phone_upgrade_retires_old_project_selection_and_reroutes(world,paused):
    import hashlib
    from personal_agent_dal.timeline.projects import catalog
    from personal_agent_dal.timeline.decisions import DecisionService
    from personal_agent_dal.timeline.requests import digest
    from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow as Workflow,DevelopmentGate as Gate,DevelopmentArtifact as Artifact,DevelopmentDecisionRequest as Decision,DevelopmentProjectBinding as Binding
    r,service,rid,payload=unlimited_case(world)
    old=approve(service,preview(service,payload)['proposal'])
    with r.sessions() as s,s.begin():
        wf=s.get(Workflow,rid);wf.status='active';wf.blocker_reason=None
        candidates=catalog(r,s,rid);body=dict(kind='project_route',text='Synthetic route',candidates=candidates)
        s.add(Artifact(artifact_id='route',workflow_id=rid,kind='project_route',revision=1,
            body_sha256=hashlib.sha256(body['text'].encode()).hexdigest(),sealed_body=r._seal(Artifact,'route','sealed_body',body),
            source_step_id='step',source_receipt_digest=digest(body)))
    decision=DecisionService(r).propose(rid,'route',kind='project_selection',candidates=candidates)
    if paused:
        with r.sessions() as s,s.begin():
            s.get(Workflow,rid).status='paused';s.get(Gate,rid).mode='paused'
    state=service.read(rid,subject='device:synthetic')
    payload.update(operation='renew',expected=state['expected'],
        expected_grant=dict(id=old['grant_id'],version=old['grant_version'],digest=old['grant_digest']))
    proposal=preview(service,payload,'reroute-preview')['proposal']
    with r.sessions() as s:assert s.get(Workflow,rid).phase=='project_selection'
    updated=approve(service,proposal,'reroute-approve')
    assert updated['status']=='accepted'
    assert granted_invalidation_event(r,rid)['invalidated_decision_ids']==[decision['decision_id']]
    with r.sessions() as s:
        assert s.get(Workflow,rid).phase=='project_routing'
        assert s.get(Workflow,rid).status==('paused' if paused else 'active')
        assert s.get(Gate,rid).mode==('paused' if paused else 'open')
        assert s.get(Decision,decision['decision_id']).status=='superseded'
        assert s.scalar(select(Decision).where(Decision.status=='pending')) is None
        assert s.get(Binding,rid) is None


@pytest.mark.parametrize('paused',[False,True])
def test_phone_remote_attachment_amend_retires_selection_and_reroutes(world,paused):
    """First remote attachment from project_selection without a Binding, end to end."""
    import hashlib
    from personal_agent_dal.timeline.projects import catalog
    from personal_agent_dal.timeline.decisions import DecisionService
    from personal_agent_dal.timeline.requests import digest
    from personal_agent_dal.storage.timeline_models import (DevelopmentWorkflow as Workflow,DevelopmentGate as Gate,
        DevelopmentArtifact as Artifact,DevelopmentDecisionRequest as Decision,DevelopmentProjectBinding as Binding,
        DevelopmentProjectAuthorization as Grant)
    r,service,rid,payload=remoteless_case(world)
    old=approve(service,preview(service,payload)['proposal'])
    with r.sessions() as s,s.begin():
        wf=s.get(Workflow,rid);wf.status='active';wf.blocker_reason=None
        candidates=catalog(r,s,rid);body=dict(kind='project_route',text='Synthetic route',candidates=candidates)
        s.add(Artifact(artifact_id='route',workflow_id=rid,kind='project_route',revision=1,
            body_sha256=hashlib.sha256(body['text'].encode()).hexdigest(),sealed_body=r._seal(Artifact,'route','sealed_body',body),
            source_step_id='step',source_receipt_digest=digest(body)))
    decision=DecisionService(r).propose(rid,'route',kind='project_selection',candidates=candidates)
    if paused:
        with r.sessions() as s,s.begin():
            s.get(Workflow,rid).status='paused';s.get(Gate,rid).mode='paused'
    with r.sessions() as s:
        row=s.scalar(select(Template).where(Template.active==1))
        value=r._open(Template,row.template_id,'sealed_template',row.sealed_template)
    value.update(revision=3,remote_repository='synthetic/new-repo')
    service.register_template(ProjectTemplate.model_validate(value),actor='operator',observed_at=r.now(),
        expires_at=None,evidence_digest='f'*64)
    state=service.read(rid,subject='device:synthetic')
    payload.update(operation='amend',requested_actions=['read','write'],template_revision=3,
        template_digest=state['candidates'][0]['template_digest'],expected=state['expected'],
        expected_grant=dict(id=old['grant_id'],version=old['grant_version'],digest=old['grant_digest']))
    proposal=preview(service,payload,'attach-preview')['proposal']
    with r.sessions() as s:assert s.get(Workflow,rid).phase=='project_selection'
    updated=approve(service,proposal,'attach-approve')
    assert updated['status']=='accepted' and updated['grant_id']==old['grant_id']
    assert granted_invalidation_event(r,rid)['invalidated_decision_ids']==[decision['decision_id']]
    with r.sessions() as s:
        assert s.get(Workflow,rid).phase=='project_routing'
        assert s.get(Workflow,rid).status==('paused' if paused else 'active')
        assert s.get(Gate,rid).mode==('paused' if paused else 'open')
        assert s.get(Decision,decision['decision_id']).status=='superseded'
        assert s.scalar(select(Decision).where(Decision.status=='pending')) is None
        assert s.get(Binding,rid) is None
        grant=s.get(Grant,old['grant_id'])
        assert r._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)['remote_repository']=='synthetic/new-repo'


@pytest.mark.parametrize('operation,phase',[('amend','project_selection'),('renew','project_selection'),
    ('amend','project_registration')])
def test_bound_workflow_refuses_remote_attachment(world,operation,phase):
    """A live Binding blocks the reroute guard, so first attachment must be refused."""
    import hashlib
    from personal_agent_dal.timeline.projects import catalog,bind_phone_choice
    from personal_agent_dal.timeline.decisions import DecisionService
    from personal_agent_dal.timeline.requests import digest
    from personal_agent_dal.storage.timeline_models import (DevelopmentWorkflow as Workflow,
        DevelopmentArtifact as Artifact,DevelopmentDecisionRequest as Decision,DevelopmentProjectBinding as Binding)
    r,service,rid,payload=remoteless_case(world)
    old=approve(service,preview(service,payload)['proposal'])
    with r.sessions() as s,s.begin():
        wf=s.get(Workflow,rid);wf.status='active';wf.blocker_reason=None
        candidates=catalog(r,s,rid);body=dict(kind='project_route',text='Synthetic route',candidates=candidates)
        s.add(Artifact(artifact_id='route',workflow_id=rid,kind='project_route',revision=1,
            body_sha256=hashlib.sha256(body['text'].encode()).hexdigest(),sealed_body=r._seal(Artifact,'route','sealed_body',body),
            source_step_id='step',source_receipt_digest=digest(body)))
    decision=DecisionService(r).propose(rid,'route',kind='project_selection',candidates=candidates)
    with r.sessions() as s,s.begin():
        wf=s.get(Workflow,rid)
        assert bind_phone_choice(r,s,wf,'route',catalog(r,s,rid)) is True
        wf.phase=phase
    with r.sessions() as s:
        row=s.scalar(select(Template).where(Template.active==1))
        value=r._open(Template,row.template_id,'sealed_template',row.sealed_template)
    value.update(revision=3,remote_repository='synthetic/new-repo')
    service.register_template(ProjectTemplate.model_validate(value),actor='operator',observed_at=r.now(),
        expires_at=None,evidence_digest='f'*64)
    state=service.read(rid,subject='device:synthetic')
    payload.update(operation=operation,requested_actions=['read','write'],template_revision=3,
        template_digest=state['candidates'][0]['template_digest'],expected=state['expected'],
        expected_grant=dict(id=old['grant_id'],version=old['grant_version'],digest=old['grant_digest']))
    result=preview(service,payload,'bound-attach-preview')
    assert result['status']=='refused' and result['reason']=='INVALID_ARGUMENT'
    with r.sessions() as s:
        wf=s.get(Workflow,rid)
        assert wf.phase==phase and s.get(Binding,rid) is not None
        assert s.get(Decision,decision['decision_id']).status=='pending'
