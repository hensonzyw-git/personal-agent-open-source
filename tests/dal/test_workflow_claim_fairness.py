"""Real claim/reserve/SQLite; synthetic Worker auth and signed admission keys."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step, DevelopmentWorkflow as Workflow
from personal_agent_dal.timeline.runner import mount_routes
from tests.dal.test_timeline_driver import configured
from tests.dal.test_timeline_requests import world, submit


def claims(world,count):
    r,driver,_,_=configured(world)
    for number in range(2,count+1):
        wf=submit(r,number)['request_id'];driver.tick(wf)
    endpoint=SimpleNamespace(requests=r,roles=driver.roles,execution_authority=driver.authority)
    app=FastAPI()
    mount_routes(app,endpoint,SimpleNamespace(kill_switch=False,auth=lambda:('synthetic',1)))
    with r.sessions() as s:ids=list(s.scalars(select(Step.step_id).order_by(Step.step_id)))
    return r,driver,TestClient(app),ids


def claim(client):
    response=client.post('/workflow/claim',json={})
    assert response.status_code==200,response.text
    return response.json()['binding']


@pytest.mark.parametrize('count',[2,103])
@pytest.mark.parametrize('blocked_first',[False,True])
def test_every_eligible_step_is_offered_before_replaying_first(world,count,blocked_first):
    r,_,client,ids=claims(world,count)
    if blocked_first:
        with r.sessions() as s,s.begin():
            step=s.get(Step,ids[0]);s.get(Workflow,step.workflow_id).status='blocked'
        ids=ids[1:]
    # Leave all steps nonterminal: an earlier reservation must not hide the
    # other workflows. Includes a page boundary and a refused candidate.
    with client:
        bindings=[claim(client) for _ in ids]
        assert [b['step_id'] if b else None for b in bindings]==ids
        assert claim(client) is None  # One bounded wrap poll.
        repeated=claim(client)
        assert repeated==bindings[0]  # Durable reservation, not a fresh attempt.


def test_new_lower_id_is_found_on_next_wrap(world,monkeypatch):
    r,driver,client,ids=claims(world,2)
    with client:
        assert claim(client)['step_id']==ids[0]
        wf=submit(r,3)['request_id']
        lower='0'*32
        with monkeypatch.context() as patch:
            patch.setattr('personal_agent_dal.timeline.driver.new_id',lambda:lower)
            assert driver.tick(wf)['step_id']==lower
        next_binding=claim(client)
        assert next_binding is not None and next_binding['step_id']==ids[1]
        assert claim(client) is None
        assert claim(client)['step_id']==lower


@pytest.mark.parametrize('healthy_candidate',[False,True])
def test_old_pregrant_budget_no_longer_blocks_a_candidate(world,monkeypatch,healthy_candidate):
    from datetime import timedelta
    from personal_agent_dal.storage.timeline_models import DevelopmentExecution as Execution
    from personal_agent_dal.timeline.recovery import RecoveryService
    r,driver,client,_=claims(world,2 if healthy_candidate else 1)
    with r.sessions() as s:
        running=s.scalar(select(Step).where(Step.status=='dispatch_started'))
        wf,step_id,attempt_id=running.workflow_id,running.step_id,running.attempt_id
        started=s.scalar(select(Execution).where(Execution.step_id==step_id)).started_at
    later=started+timedelta(seconds=590);r.now=lambda:later
    driver.accept(step_id,attempt_id=attempt_id,result=dict(kind='clarification',text='Synthetic clarification',
        ready=False,questions=['Missing detail'],acceptance=[]))
    with r.sessions() as s:
        version=s.get(Workflow,wf).version
        execution=s.scalar(select(Execution).where(Execution.step_id==step_id))
        assert execution.charged_seconds==590
    RecoveryService(r).apply(command_id='budget-more-detail',source_message_ref='budget-more-detail-source',
        subject='device:synthetic',workflow_id=wf,expected_version=version,action='clarification',text='Synthetic detail')
    with monkeypatch.context() as patch:
        patch.setattr('personal_agent_dal.timeline.driver.new_id',lambda:'0')
        assert driver.tick(wf)['step_id']=='0'
    # Historical elapsed time remains accounted but no longer blocks pregrant work.
    with client:binding=claim(client)
    with r.sessions() as s:
        current=s.get(Workflow,wf)
        assert current.status=='active' and current.blocker_reason is None
        assert binding is not None and binding['step_id']=='0'
        execution=s.scalar(select(Execution).where(Execution.step_id=='0'))
        assert execution.reserved_seconds==3600


def test_normal_tick_and_resume_cannot_create_fresh_step_beside_inflight(world):
    from personal_agent_dal.timeline.recovery import RecoveryService
    r,driver,client,ids=claims(world,1)
    with r.sessions() as s:
        step=s.get(Step,ids[0]);wf=step.workflow_id;version=s.get(Workflow,wf).version
    for _ in range(3):
        assert driver.tick(wf)==dict(workflow_id=wf,status='dispatch_started',step_id=ids[0])
    result=RecoveryService(r).process(command_id='resume-inflight',source_message_ref='resume-inflight-source',
        subject='device:synthetic',workflow_id=wf,expected_version=version,action='resume',text='继续开发')
    assert result['status']=='refused' and result['reason']=='RECONCILIATION_REQUIRED'
    with client:binding=claim(client)
    assert binding['step_id']==ids[0]
    with r.sessions() as s:
        assert list(s.scalars(select(Step.step_id).where(Step.workflow_id==wf)))==ids
        assert s.get(Workflow,wf).status=='active'
