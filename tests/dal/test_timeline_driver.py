"""Driver adversaries are enumerated before the implementation."""
import pytest
from sqlalchemy import select
from tests.dal.test_timeline_requests import world, submit
from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step, DevelopmentWorkflow as Workflow
from personal_agent_dal.timeline.driver import WorkflowDriver


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
