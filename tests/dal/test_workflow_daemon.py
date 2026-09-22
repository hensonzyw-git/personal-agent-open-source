"""Scheduler does not turn waiting or exceptions into a busy retry loop."""
import subprocess
import sys
import pytest
from personal_agent_dal.worker.workflow_daemon import scheduler_lock,run_loop


def test_lock_excludes_another_process_and_releases(tmp_path):
    lock=tmp_path/'owner.lock'
    code='from personal_agent_dal.worker.workflow_daemon import scheduler_lock; import sys\nwith scheduler_lock(sys.argv[1]): pass'
    with scheduler_lock(lock):
        assert subprocess.run([sys.executable,'-c',code,str(lock)],capture_output=True).returncode!=0
    assert subprocess.run([sys.executable,'-c',code,str(lock)],capture_output=True).returncode==0


def test_scheduler_waits_and_redacts_errors():
    class Stop:
        delays=[]
        def is_set(self):return len(self.delays)==3
        def wait(self,delay):self.delays.append(delay)
    stop=Stop();statuses=[];calls=[]
    def poll():
        calls.append(1)
        if len(calls)==1:return {'status':'completed'}
        if len(calls)==2:return {'status':'idle'}
        raise ValueError('private credential must not escape')
    run_loop(poll,stop,write_status=statuses.append)
    assert stop.delays==[1,15,60]
    assert statuses[-1]['error_type']=='ValueError'
    assert 'private' not in str(statuses)
