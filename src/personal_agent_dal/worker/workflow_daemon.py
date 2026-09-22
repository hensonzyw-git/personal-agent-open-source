"""Single-owner workflow scheduling. The inventory owns execution recovery."""
import fcntl
import json
import os
import signal
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


@contextmanager
def scheduler_lock(path):
    path=Path(path)
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def run_loop(poll,stop,*,write_status,idle_seconds=15,error_seconds=60):
    while not stop.is_set():
        try:
            result=poll()
            status=result.get('status','unknown')
            write_status({'status':status,'observed_at':datetime.now(timezone.utc).isoformat()})
            delay=1 if status=='completed' else idle_seconds if status=='idle' else error_seconds
        except Exception as error:
            # Never copy model output, paths, credentials or raw HTTP errors.
            write_status({'status':'error','error_type':type(error).__name__,
                'observed_at':datetime.now(timezone.utc).isoformat()})
            delay=error_seconds
        stop.wait(delay)


def serve(config,workflow_path):
    from personal_agent_dal.worker.cli import _open_transport
    from personal_agent_dal.worker.remote import RemoteHttpAdapter
    from personal_agent_dal.worker.workflow import WorkflowWorker,load_config
    workflow_path=Path(workflow_path)
    stop=threading.Event()
    old={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
    def stopping(*_):stop.set()
    def poll():
        with _open_transport(config) as transport:
            if not isinstance(transport,RemoteHttpAdapter):raise ValueError('REMOTE_WORKFLOW_TRANSPORT_REQUIRED')
            return WorkflowWorker(transport,load_config(workflow_path),stop_event=stop).poll()
    def status(value):
        target=workflow_path.parent/'scheduler-status.json'
        temporary=target.with_suffix('.tmp')
        fd=os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as file:json.dump(value,file)
        os.replace(temporary,target)
    try:
        with scheduler_lock(workflow_path.parent/'operator-poll.lock'):
            for sig in old:signal.signal(sig,stopping)
            run_loop(poll,stop,write_status=status)
            status({'status':'stopped','observed_at':datetime.now(timezone.utc).isoformat()})
    finally:
        for sig,handler in old.items():signal.signal(sig,handler)
    return 0
