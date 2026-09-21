"""Renewal preserves real persisted ledger rows, including irreversible charges."""
from datetime import timedelta
from sqlalchemy import select
from tests.dal.test_timeline_requests import world
from tests.dal.test_phone_authorization import setup_case,preview,approve
from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step,DevelopmentExecution as Execution,DevelopmentWorkflow as Workflow


def test_renewal_preserves_spent_and_releases_only_unstarted_reservation(world):
    r,service,rid,payload=setup_case(world)
    original=approve(service,preview(service,payload)['proposal'])
    with r.sessions() as s,s.begin():
        wf=s.get(Workflow,rid);wf.blocker_reason='EXECUTION_BUDGET_EXHAUSTED'
        for index,status,charge,started in [(1,'completed',300,r.now()-timedelta(minutes=5)),(2,'prepared',None,None)]:
            key='budget-step-'+str(index)
            step=Step(step_id=key,workflow_id=rid,phase='coding',cycle=index,status=status,input_digest='a'*64,
                sealed_input=r._seal(Step,key,'sealed_input',{}),expected_version=wf.version,gate_epoch=1)
            s.add(step);s.flush()
            s.add(Execution(execution_id=key,step_id=key,worker_id='synthetic-worker',boot_id='synthetic-boot',
                supervisor_epoch=1,lease_id=key,lease_until=r.now()+timedelta(minutes=5),admission_digest='b'*64,
                binding_digest='c'*64,sealed_binding=r._seal(Execution,key,'sealed_binding',{}),
                grant_id=original['grant_id'],reserved_seconds=500,charged_seconds=charge,started_at=started))
    payload.update(operation='renew',budget_seconds=1500,expected=service.read(rid,subject='device:synthetic')['expected'],
        expected_grant=dict(id=original['grant_id'],version=1,digest=original['grant_digest']))
    renewed=approve(service,preview(service,payload,'renew-preview')['proposal'],'renew-approve')
    assert renewed['status']=='accepted' and renewed['grant_id']==original['grant_id']
    with r.sessions() as s:
        rows=list(s.scalars(select(Execution).order_by(Execution.execution_id)))
        assert [row.charged_seconds for row in rows]==[300,0]
        assert all(row.grant_id==original['grant_id'] for row in rows)
        assert s.get(Step,'budget-step-1').status=='completed'
        assert s.get(Step,'budget-step-2').status=='retired'


def test_new_allowed_device_can_renew_without_rewriting_original_grant_subject(world):
    from personal_agent_dal.storage.timeline_models import DevelopmentProjectTemplate as Template,DevelopmentProjectAuthorization as Grant
    from personal_agent_dal.timeline.authorization_contracts import ProjectTemplate
    from personal_agent_dal.timeline.phone_authorization import execution_policy
    r,service,rid,payload=setup_case(world)
    original=approve(service,preview(service,payload)['proposal'])
    with r.sessions() as s:
        row=s.scalar(select(Template));value=r._open(Template,row.template_id,'sealed_template',row.sealed_template)
    value.update(revision=2,allow_subjects=['device:second'])
    service.register_template(ProjectTemplate(**value),actor='operator',observed_at=r.now(),expires_at=r.now()+timedelta(hours=1),evidence_digest='f'*64)
    state=service.read(rid,subject='device:second')
    payload.update(operation='renew',template_revision=2,template_digest=state['candidates'][0]['template_digest'],
        expected=state['expected'],expected_grant=dict(id=original['grant_id'],version=1,digest=original['grant_digest']))
    proposal=preview(service,payload,'second-preview',subject='device:second')['proposal']
    result=service.approve(command_id='second-approve',subject='device:second',payload=dict(
        proposal_id=proposal['proposal_id'],binding_digest=proposal['binding_digest'],source_action_ref='second-action',
        context_id='second-context',context_jti='second-jti',key_thumbprint='second-thumb',
        confirmed_at=r.now().isoformat(),confirmation_expires_at=(r.now()+timedelta(minutes=10)).isoformat()))
    assert result['status']=='accepted'
    with r.sessions() as s:
        grant=s.get(Grant,original['grant_id'])
        assert grant.subject=='device:synthetic'
        assert execution_policy(r,s,grant)['template_digest']==state['candidates'][0]['template_digest']
