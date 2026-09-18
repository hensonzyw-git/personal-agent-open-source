"""Real claim/reserve/SQLite fairness; only the Worker auth identity is synthetic."""
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
