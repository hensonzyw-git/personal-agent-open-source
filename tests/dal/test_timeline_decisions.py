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
