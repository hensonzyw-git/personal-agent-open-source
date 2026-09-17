import pytest
from sqlalchemy import select
from tests.dal.test_timeline_requests import world,submit
from personal_agent_dal.timeline.stages import StageService
from personal_agent_dal.storage.timeline_models import DevelopmentDependencySatisfaction,DevelopmentStage


def plan():
    return dict(nodes=[dict(stage_id='one',revision=1,goal='Synthetic first',acceptance=['a']),dict(stage_id='two',revision=1,goal='Synthetic second',acceptance=['b'])],edges=[dict(upstream_id='one',upstream_revision=1,downstream_id='two',downstream_revision=1)],acceptance=['a','b'])


def test_dag_freezes_without_future_receipts_and_root_alone_ready(world):
    r=submit(world[2]);service=StageService(world[2])
    id=service.freeze(workflow_id=r['request_id'],revision=1,design_digest='a'*64,review_digest='b'*64,body=plan())
    with world[2].sessions() as s:
        assert list(s.scalars(select(DevelopmentDependencySatisfaction)))==[]
    assert service.ready(id)==['one']
    assert service.claim(r['request_id'],'one',1,expected_version=2)['state']=='coding'
    with pytest.raises(ValueError):service.claim(r['request_id'],'two',1,expected_version=1)


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
    r=submit(world[2]);service=StageService(world[2]);id=service.freeze(workflow_id=r['request_id'],revision=1,design_digest='a'*64,review_digest='b'*64,body=plan());service.ready(id);service.claim(r['request_id'],'one',1,expected_version=2)
    candidate=dict(base_sha='a'*40,head_sha='b'*40,tree_sha='c'*40)
    service.candidate('one',1,expected_version=3,**candidate)
    service.verified('one',1,expected_version=4,receipt_digest='d'*64,**candidate)
    with pytest.raises(ValueError,match='STALE_BINDING'):
        service.reviewed('one',1,expected_version=5,receipt_digest='e'*64,passed=True,**dict(candidate,head_sha='f'*40))


def test_all_dependencies_need_commit_evidence(world):
    r=submit(world[2]);service=StageService(world[2]);id=service.freeze(workflow_id=r['request_id'],revision=1,design_digest='a'*64,review_digest='b'*64,body=plan());service.ready(id);service.claim(r['request_id'],'one',1,expected_version=2)
    candidate=dict(base_sha='a'*40,head_sha='b'*40,tree_sha='c'*40)
    service.candidate('one',1,expected_version=3,**candidate)
    service.verified('one',1,expected_version=4,receipt_digest='d'*64,**candidate)
    service.reviewed('one',1,expected_version=5,receipt_digest='e'*64,passed=True,**candidate)
    assert service.ready(id)==[]
    service.committed('one',1,expected_version=6,receipt_digest='f'*64,**candidate)
    assert service.ready(id)==['two']
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
