"""Actual bounded child processes with a synthetic CLI, no model invocation."""
import os
import pytest
from pathlib import Path
from personal_agent_core.crypto import KeyRing,generate_key
from personal_agent_dal.worker.supervisor import Supervisor
from personal_agent_dal.worker.workflow_inventory import WorkflowInventory
from personal_agent_dal.worker.runtime_process import fixture_plan,run_process,os_boot_id,active_process_observation
from personal_agent_dal.worker.workflow_process import run_owned


@pytest.mark.parametrize('mode',['success','events'])
def test_packet_git_runs_in_starting_then_model_owns_recovery(tmp_path,monkeypatch,mode):
    supervisor=Supervisor(tmp_path.resolve()/'sup',boot_id=os_boot_id(),epoch=1)
    reservation=supervisor.reserve(attempt_id='review',workspace_id='review',generation=1,authority={'workflow_id':'w'},read_roots=[])
    inventory=WorkflowInventory(supervisor,KeyRing([generate_key('test')],service='dal-worker'))
    inventory.adopt({'attempt_id':'review','owner':{'kind':'workflow','workflow_id':'w'}},reservation)
    inventory.transition('review','prepared','dispatch_requested');inventory.transition('review','dispatch_requested','granted')
    seen={}
    import personal_agent_dal.worker.runtime_process as processes
    original_counter=processes.CLIEventCounter
    def counter(runtime,limit):
        seen['event_limit']=limit
        return original_counter(runtime,limit)
    monkeypatch.setattr(processes,'CLIEventCounter',counter)
    def prepare():
        assert inventory.get('review')['state']=='starting'
        output,code=run_owned(['/usr/bin/git','--version'],cwd=reservation['workspace'],environment={'PATH':'/usr/bin:/bin'},
            timeout=10,inventory=inventory,attempt='review',heartbeat=lambda:True,revalidate=lambda:None)
        assert code==0 and b'git version' in output
        seen['git']=active_process_observation(inventory.get('review')['observation'])['pid']
        return b''
    result=run_process(inventory,'review',fixture_plan(reservation,mode),heartbeat=lambda:True,deadline=9999999999,prepare_prompt=prepare)
    assert result['stop']['process_exited']
    if mode=='success':assert result['reason'] is None and result['exit_code']==0
    else:assert result['reason']=='CLI_EVENT_STEP_LIMIT'
    observation=inventory.get('review')['observation']
    assert seen['event_limit']==24
    assert observation['active_process_owner']=='model'
    assert active_process_observation(observation)['pid']==observation['pid']!=seen['git']


def test_process_selection_preserves_unknown_and_legacy():
    old={'pid':11,'executor_process':{'pid':22}}
    assert active_process_observation(old)['pid']==22
    assert active_process_observation(dict(old,active_process_owner='model'))['pid']==11
    assert active_process_observation(dict(old,active_process_owner='executor',executor_process={}))=={}
    assert active_process_observation(dict(old,active_process_owner='tampered'))=={}
