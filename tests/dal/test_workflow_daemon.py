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


def test_manual_poll_honors_daemon_lock_before_opening_transport(tmp_path,monkeypatch):
    from personal_agent_dal.worker import cli
    monkeypatch.setattr(cli,'load_worker_config',lambda _: object())
    calls=[]
    def transport(_):
        calls.append(True)
        raise ValueError('synthetic stop before network')
    monkeypatch.setattr(cli,'_open_transport',transport)
    argv=['--config',str(tmp_path/'worker.json'),'workflow-poll-once','--workflow-config',str(tmp_path/'workflow.json')]
    with scheduler_lock(tmp_path/'operator-poll.lock'):
        assert cli.main(argv)==1
        assert calls==[]
    assert cli.main(argv)==1
    assert calls==[True]
