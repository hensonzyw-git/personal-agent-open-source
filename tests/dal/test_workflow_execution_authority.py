import pytest
from sqlalchemy import select
from personal_agent.api.dal_client import sign_decision
from personal_agent_dal.timeline.driver import WorkflowDriver
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.storage.timeline_models import DevelopmentExecution,DevelopmentDriverStep,DevelopmentWorkflow
from tests.dal.test_timeline_requests import world
from tests.dal.test_timeline_driver import configured


def result():return dict(kind='clarification',text='Synthetic',ready=True,questions=[],acceptance=['A'])


def test_plain_internal_dictionary_cannot_dispatch_or_accept(world):
    r,signed,wf,launch=configured(world)
    driver=WorkflowDriver(r,roles=signed.roles)
    with pytest.raises(ValueError,match='RUNTIME_ADMISSION_REQUIRED'):
        driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=result())
    assert r.detail(wf)['phase']=='clarify'


@pytest.mark.parametrize('attack',['signature','payload','binding','worker','domain','expiry','revocation','boot'])
def test_signed_result_is_bound_to_exact_authority(world,attack):
    r,driver,wf,launch=configured(world)
    proof=driver.proof(launch['step_id'],'dal.workflow-result/1.0',result())
    body=result()
    if attack=='signature':proof=proof[:-8]+'tampered'
    if attack=='payload':body['text']='different'
    if attack in ('binding','worker','domain','expiry'):
        import base64,json
        claims=json.loads(base64.urlsafe_b64decode(proof.split('.')[1]+'=='))
        if attack=='binding':claims['binding_digest']='f'*64
        if attack=='worker':claims['iss']='another-worker'
        if attack=='domain':claims['domain']='dal.workflow-prelaunch/1.0'
        if attack=='expiry':claims['exp']=claims['iat']-1
        proof=sign_decision(claims,key=driver.test_key,kid='test')
    if attack=='revocation':driver.test_registry['synthetic']['admission']['revoked']=True
    if attack=='boot':driver.test_registry['synthetic']['admission']['boot_id']='new-boot'
    with pytest.raises(ValueError):
        WorkflowDriver.accept(driver,launch['step_id'],attempt_id=launch['attempt_id'],result=body,receipt=proof)
    with r.sessions() as s:
        assert s.get(DevelopmentWorkflow,wf).phase=='clarify'
        assert s.scalar(select(DevelopmentExecution)).receipt_digest is None


def test_receipt_and_transition_rollback_together_on_invalid_output(world):
    r,driver,wf,launch=configured(world)
    body=dict(result(),unexpected=True)
    with pytest.raises(ValueError):driver.accept(launch['step_id'],attempt_id=launch['attempt_id'],result=body)
    with r.sessions() as s:
        assert s.scalar(select(DevelopmentExecution)).receipt_digest is None
        assert s.get(DevelopmentDriverStep,launch['step_id']).status=='dispatch_started'


def test_pregrant_clarification_attempts_share_one_workflow_budget(world):
    from datetime import timedelta
    from personal_agent_dal.timeline.recovery import RecoveryService
    r,driver,wf,item=configured(world)
    later=r.now()+timedelta(seconds=590);r.now=lambda:later
    driver.accept(item['step_id'],attempt_id=item['attempt_id'],result=dict(kind='clarification',text='Synthetic clarification',ready=False,questions=['Missing detail'],acceptance=[]))
    with r.sessions() as s:version=s.get(DevelopmentWorkflow,wf).version
    RecoveryService(r).apply(command_id='clarify-again',source_message_ref='source-again',subject='device:synthetic',
        workflow_id=wf,expected_version=version,action='clarification',text='Synthetic additional detail')
    step=driver.tick(wf)
    with pytest.raises(ValueError,match='EXECUTION_BUDGET_EXHAUSTED'):
        driver.dispatch(step['step_id'],admission={'test':'signed-by-helper'})
