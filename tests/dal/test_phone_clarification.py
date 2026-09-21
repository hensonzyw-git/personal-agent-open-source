"""Phone supplemental input must update one bound intake, never create another."""
import pytest
from tests.dal.test_timeline_requests import world, submit
from personal_agent_dal.timeline.recovery import RecoveryService
from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow


def test_active_intake_accepts_supplement_once_and_preserves_original(world):
    r=world[2]; rid=submit(r)['request_id']
    command=dict(command_id='supplement',source_message_ref='phone-message',subject='device:synthetic',
        workflow_id=rid,expected_version=1,action='clarification',text='Only acquisition and storage')
    receipt=RecoveryService(r).process(**command)
    assert receipt['status']=='accepted'
    assert RecoveryService(r).process(**command)==receipt
    detail=r.detail(rid)
    assert detail['request_version']==2
    assert detail['text']=='Synthetic request 1\n\n用户补充：\nOnly acquisition and storage'
    assert r.list_tasks(subject='device:synthetic')['total']==1
    stale=RecoveryService(r).process(**dict(command,command_id='stale',text='changed'))
    assert stale['status']=='refused' and stale['reason']=='STALE_BINDING'


@pytest.mark.parametrize('phase,status',[('project_routing','active'),('prd_waiting','active'),('clarify','paused')])
def test_supplement_does_not_cross_gates(world,phase,status):
    r=world[2];rid=submit(r)['request_id']
    with r.sessions() as s,s.begin():
        wf=s.get(DevelopmentWorkflow,rid);wf.phase=phase;wf.status=status
    result=RecoveryService(r).process(command_id='supplement',source_message_ref='phone',subject='device:synthetic',
        workflow_id=rid,expected_version=1,action='clarification',text='Only storage')
    assert result['status']=='refused'
    assert r.detail(rid)['request_version']==1


def test_unknown_execution_refuses_input_change(world):
    from tests.dal.test_timeline_driver import configured
    from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep
    r,driver,rid,item=configured(world)
    with r.sessions() as s,s.begin():
        step=s.get(DevelopmentDriverStep,item['step_id']);step.status='dispatch_started'
    detail=r.detail(rid)
    result=RecoveryService(r).process(command_id='unsafe-change',source_message_ref='phone',subject='device:synthetic',
        workflow_id=rid,expected_version=detail['version'],action='clarification',text='Only storage')
    assert result['status']=='refused' and result['reason']=='RECONCILIATION_REQUIRED'
    assert r.detail(rid)['text']==detail['text']


def test_prepared_step_is_retired_when_input_changes(world):
    from tests.dal.test_timeline_driver import configured
    from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep
    r,driver,_,_=configured(world)
    rid=submit(r,2)['request_id']
    item=driver.tick(rid)
    detail=r.detail(rid)
    result=RecoveryService(r).process(command_id='new-input',source_message_ref='phone',subject='device:synthetic',
        workflow_id=rid,expected_version=detail['version'],action='clarification',text='Only storage')
    assert result['status']=='accepted'
    with r.sessions() as s:assert s.get(DevelopmentDriverStep,item['step_id']).status=='retired'
