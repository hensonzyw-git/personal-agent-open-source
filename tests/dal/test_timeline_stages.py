import pytest
from personal_agent_dal.timeline.stages import stage_identity
from tests.dal.stage_fixtures import reviewed_design,evidence
from sqlalchemy import select
from tests.dal.test_timeline_requests import world,submit
from personal_agent_dal.timeline.stages import StageService
from personal_agent_dal.storage.timeline_models import DevelopmentDependencySatisfaction,DevelopmentStage


def plan():
    return dict(nodes=[dict(stage_id='one',revision=1,goal='Synthetic first',acceptance=['a']),dict(stage_id='two',revision=1,goal='Synthetic second',acceptance=['b'])],edges=[dict(upstream_id='one',upstream_revision=1,downstream_id='two',downstream_revision=1)],acceptance=['a','b'])


def test_dag_freezes_without_future_receipts_and_root_alone_ready(world):
    r=submit(world[2]);service=StageService(world[2])
    id=service.freeze(workflow_id=r['request_id'],revision=1,body=plan(),**reviewed_design(world[2],r['request_id'],plan()))
    with world[2].sessions() as s:
        assert list(s.scalars(select(DevelopmentDependencySatisfaction)))==[]
    assert service.ready(id)==[stage_identity(r['request_id'],'one')]
    assert service.claim(r['request_id'],stage_identity(r['request_id'],'one'),1,expected_version=2)['state']=='coding'
    with pytest.raises(ValueError):service.claim(r['request_id'],stage_identity(r['request_id'],'two'),1,expected_version=1)


@pytest.mark.parametrize('attack',['cycle','missing','duplicate','unassigned'])
def test_invalid_dag_is_never_persisted(world,attack):
    body=plan()
    if attack=='cycle':body['edges'].append(dict(upstream_id='two',upstream_revision=1,downstream_id='one',downstream_revision=1))
    elif attack=='missing':body['edges'][0]['upstream_id']='absent'
    elif attack=='duplicate':body['nodes'].append(body['nodes'][0])
    else:body['acceptance'].append('not-assigned')
    r=submit(world[2])
    with pytest.raises(ValueError):StageService(world[2]).freeze(workflow_id=r['request_id'],revision=1,design_digest='a'*64,review_digest='b'*64,body=body)


def test_candidate_change_invalidates_test_and_review_evidence(world):
    r=submit(world[2]);service=StageService(world[2]);id=service.freeze(workflow_id=r['request_id'],revision=1,body=plan(),**reviewed_design(world[2],r['request_id'],plan()));service.ready(id);service.claim(r['request_id'],stage_identity(r['request_id'],'one'),1,expected_version=2)
    candidate=dict(base_sha='a'*40,head_sha='b'*40,tree_sha='c'*40)
    service.candidate(stage_identity(r['request_id'],'one'),1,expected_version=3,**candidate)
    _,verification=evidence(world[2],r['request_id'],'verify',dict(candidate=candidate,passed=True),stage=(stage_identity(r['request_id'],'one'),1))
    service.verified(stage_identity(r['request_id'],'one'),1,expected_version=4,receipt_digest=verification,**candidate)
    with pytest.raises(ValueError,match='STALE_BINDING'):
        service.reviewed(stage_identity(r['request_id'],'one'),1,expected_version=5,receipt_digest='e'*64,passed=True,**dict(candidate,head_sha='f'*40))


def test_all_dependencies_need_commit_evidence(world):
    r=submit(world[2]);service=StageService(world[2]);id=service.freeze(workflow_id=r['request_id'],revision=1,body=plan(),**reviewed_design(world[2],r['request_id'],plan()));service.ready(id);service.claim(r['request_id'],stage_identity(r['request_id'],'one'),1,expected_version=2)
    candidate=dict(base_sha='a'*40,head_sha='b'*40,tree_sha='c'*40)
    service.candidate(stage_identity(r['request_id'],'one'),1,expected_version=3,**candidate)
    _,verification=evidence(world[2],r['request_id'],'verify',dict(candidate=candidate,passed=True),stage=(stage_identity(r['request_id'],'one'),1))
    service.verified(stage_identity(r['request_id'],'one'),1,expected_version=4,receipt_digest=verification,**candidate)
    _,review=evidence(world[2],r['request_id'],'code_review',dict(candidate=candidate,passed=True),stage=(stage_identity(r['request_id'],'one'),1))
    service.reviewed(stage_identity(r['request_id'],'one'),1,expected_version=5,receipt_digest=review,passed=True,**candidate)
    assert service.ready(id)==[]
    _,commit=evidence(world[2],r['request_id'],'stage_commit',dict(candidate=candidate,committed=True,commit_sha='e'*40),stage=(stage_identity(r['request_id'],'one'),1))
    service.committed(stage_identity(r['request_id'],'one'),1,expected_version=6,receipt_digest=commit,**candidate)
    assert service.ready(id)==[stage_identity(r['request_id'],'two')]
    with world[2].sessions() as s:assert len(list(s.scalars(select(DevelopmentDependencySatisfaction))))==1


@pytest.mark.parametrize('mutation',[
    lambda b:b.update(edges=None),
    lambda b:b['nodes'].__setitem__(0,None),
    lambda b:b['nodes'][0].update(revision=True),
    lambda b:b['nodes'][0].update(acceptance=[{}]),
    lambda b:b['nodes'].__setitem__(1,dict(b['nodes'][0],revision=2,acceptance=['b'])),
])
def test_malformed_stage_plan_is_closed_refusal(world,mutation):
    body=plan();mutation(body);r=submit(world[2])
    with pytest.raises(ValueError,match='PLAN_INVALID'):
        StageService(world[2]).freeze(workflow_id=r['request_id'],revision=1,design_digest='a'*64,review_digest='b'*64,body=body)


def test_logical_stage_names_are_namespaced_and_retry_stable(world):
    service=StageService(world[2])
    first=submit(world[2])
    second=submit(world[2],command_id='other-command',source_message_ref='other-message')
    plans=[]
    for request in (first,second):
        args=dict(workflow_id=request['request_id'],revision=1,body=plan(),**reviewed_design(world[2],request['request_id'],plan()))
        identity=service.freeze(**args)
        assert service.freeze(**args)==identity
        plans.append(identity)
    assert plans[0]!=plans[1]
    with world[2].sessions() as session:
        rows=list(session.scalars(select(DevelopmentStage)))
        assert len(rows)==4
        assert len({row.stage_id for row in rows})==4


def test_unproven_design_cannot_freeze(world):
    r=submit(world[2])
    with pytest.raises(ValueError,match='EXECUTION_FENCED|REVIEW_SOURCE_REQUIRED'):
        StageService(world[2]).freeze(workflow_id=r['request_id'],revision=1,design_digest='a'*64,review_digest='b'*64,body=plan())
