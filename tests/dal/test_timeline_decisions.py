import pytest
from personal_agent_dal.timeline.decisions import parse_decision,parse_project_choice


@pytest.mark.parametrize('text',['通过','批准。','同意这个 PRD'])
def test_whole_message_positive(text):assert parse_decision(text)['decision']=='approve'


@pytest.mark.parametrize('text',['不通过，但他说通过','如果测试通过就批准','“通过”','通过并发布','先通过？',''])
def test_fragments_never_authorize(text):assert parse_decision(text) is None


def test_change_feedback_is_not_truncated():
    text='改进说明'*100
    assert parse_decision('修改：'+text)==dict(decision='request_changes',feedback=text)


def test_project_selection_is_unique_and_not_model_composed():
    candidates=[dict(candidate_key='a',display_name='项目甲'),dict(candidate_key='b',display_name='项目乙')]
    assert parse_project_choice('选第二个项目',candidates)=='b'
    assert parse_project_choice('选择 项目甲。',candidates)=='a'
    for text in ['不要选第二个项目','选第三个项目','选第二个项目并授权','他说“选第二个项目”']:
        assert parse_project_choice(text,candidates) is None
    assert parse_project_choice('选择 项目甲',[candidates[0],dict(candidate_key='c',display_name='项目甲')]) is None


from tests.dal.test_timeline_requests import world,submit
from tests.dal.test_timeline_artifacts import source
from personal_agent_dal.timeline.artifacts import ArtifactService
from personal_agent_dal.timeline.decisions import DecisionService


def test_prd_decision_is_bound_and_single_use(world):
    r=world[2];wf=submit(r)['request_id'];sha=source(r,wf,'Synthetic PRD')
    artifact=ArtifactService(r).record(step_id='step',expected_result_digest=sha)
    service=DecisionService(r);proposal=service.propose(wf,artifact,kind='prd')
    body=dict(command_id='approval',source_message_ref='reply',subject='device:synthetic',decision_id=proposal['decision_id'],binding_digest=proposal['binding_digest'],text='通过')
    with pytest.raises(ValueError):service.consume(**dict(body,text='如果没问题就通过'))
    accepted=service.consume(**body)
    assert accepted['phase']=='design_authoring'
    assert service.consume(**body)==accepted
    with pytest.raises(ValueError):service.consume(**dict(body,command_id='another',source_message_ref='another-message'))


def test_stale_command_has_durable_refusal_instead_of_endless_unknown(world):
    service=DecisionService(world[2])
    body=dict(command_id='missing-target',source_message_ref='message',subject='device:synthetic',
        decision_id='missing',binding_digest='a'*64,text='通过',expected_kind='prd')
    receipt=service.process(**body)
    assert receipt==dict(command_id='missing-target',decision_id='missing',status='refused',reason='STALE_BINDING')
    assert service.process(**body)==receipt
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):service.process(**dict(body,text='拒绝'))


def routed(world, *, grant_request=None, candidate_kind='existing'):
    from datetime import timedelta
    from tests.dal.test_timeline_driver import configured
    from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step
    from personal_agent_dal.timeline.operator import Authorization,register_authorization
    r,driver,wf,launch=configured(world)
    driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=dict(
        kind='clarification',text='Synthetic requirement',ready=True,questions=[],acceptance=['A']))
    register_authorization(r,Authorization(grant_id='grant',request_id=grant_request or wf,
        project_id='project',subject='device:synthetic',approval_evidence_ref='approval',
        root='/synthetic/project',kind='existing',display_name='Synthetic project',actions=['read','write'],
        budget_seconds=600,expires_at=r.now()+timedelta(hours=1),registration_policy='local_tracker'),actor='synthetic-operator')
    prepared=driver.tick(wf)
    with r.sessions() as s:
        step=s.get(Step,prepared['step_id'])
        admission=dict(step_id=step.step_id,input_digest=step.input_digest,snapshot_id=step.snapshot_id,
            gate_epoch=step.gate_epoch,worker_id='synthetic',receipt_digest='a'*64)
    launch=driver.dispatch(prepared['step_id'],admission=admission)
    driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=dict(kind='project_route',
        text='Synthetic route',candidates=[dict(candidate_key='one',project_id='project',grant_id='grant',
        display_name='Synthetic project',kind=candidate_kind)]))
    from sqlalchemy import select
    from personal_agent_dal.storage.timeline_models import DevelopmentDecisionRequest as Decision
    with r.sessions() as s:
        decision=s.scalar(select(Decision).where(Decision.workflow_id==wf))
        return r,wf,dict(command_id='project-selection',source_message_ref='project-reply',
            subject='device:synthetic',decision_id=decision.decision_id,binding_digest=decision.binding_digest,text='选第一个项目')


@pytest.mark.parametrize('attack',['wrong_request','wrong_kind'])
def test_project_choice_cannot_reuse_another_request_or_kind_grant(world,attack):
    r,wf,command=routed(world,grant_request='other-request' if attack=='wrong_request' else None,
        candidate_kind='local_new' if attack=='wrong_kind' else 'existing')
    with pytest.raises(ValueError,match='INPUT_NOT_AUTHORIZED'):
        DecisionService(r).consume(**command)


def test_authorized_project_choice_advances_only_to_registration(world):
    r,wf,command=routed(world)
    assert DecisionService(r).consume(**command)['phase']=='project_registration'
