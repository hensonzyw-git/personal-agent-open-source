"""Owned deterministic subprocesses, after separate native executor admission."""
import os
import selectors
import signal
import subprocess
import time
from personal_agent_dal.worker.runtime_process import process_identity,stop_registered
from personal_agent_dal.worker.runtime_admission import private_json,code_identity
from personal_agent_dal.worker.supervisor import SupervisorRefusal
from personal_agent_dal.timeline.requests import digest


def validate_executor(config,supervisor):
    evidence=private_json(config['executor_admission_file'])
    expected=dict(schema='dal.workflow-executor-admission/1.0',provenance='operator-attested-external-native-executor',
        code_sha256=code_identity(),boot_id=supervisor.boot_id,supervisor_epoch=supervisor.epoch,
        config_digest=digest({k:config[k] for k in ('git_pin','projects','sandbox_pin')}))
    if set(evidence)!=set(expected)|{'issued_at','expires_at','revoked','observations'} or any(evidence.get(k)!=v for k,v in expected.items()):
        raise SupervisorRefusal('WORKFLOW_EXECUTOR_ADMISSION_REQUIRED')
    if (evidence['revoked'] is not False or type(evidence['issued_at']) is not int or type(evidence['expires_at']) is not int
        or not evidence['issued_at']<=int(time.time())<evidence['expires_at']
        or evidence['observations']!=['isolated-workspace','pinned-git','outside-write-denied','network-denied','bounded-stop','commit-readback']):
        raise SupervisorRefusal('WORKFLOW_EXECUTOR_ADMISSION_REQUIRED')


def run_owned(argv,*,cwd,environment,timeout,inventory,attempt,heartbeat,revalidate,data=b''):
    revalidate()
    if not heartbeat():raise SupervisorRefusal('AUTHORITY_LOST')
    inventory.observe(attempt,{'executor_launch_started':True})
    process=subprocess.Popen(argv,cwd=cwd,env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,close_fds=True,start_new_session=True)
    try:
        identity=dict(pid=process.pid,pgid=process.pid,process_start=process_identity(process.pid),boot_id=inventory.supervisor.boot_id)
        inventory.observe(attempt,{'executor_process':identity})
    except BaseException:
        # We still own the unreaped child: its PID cannot have been reused.
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5)
        for stream in (process.stdin,process.stdout,process.stderr):stream.close()
        raise
    selector=selectors.DefaultSelector();output=bytearray();errors=bytearray();pending=memoryview(data)
    for stream in (process.stdin,process.stdout,process.stderr):os.set_blocking(stream.fileno(),False)
    if pending:selector.register(process.stdin,selectors.EVENT_WRITE)
    else:process.stdin.close()
    selector.register(process.stdout,selectors.EVENT_READ);selector.register(process.stderr,selectors.EVENT_READ)
    until=time.monotonic()+timeout;next_beat=0
    try:
        while selector.get_map() or process.poll() is None:
            now=time.monotonic()
            if now>=until:raise SupervisorRefusal('EXECUTOR_TIMEOUT')
            if now>=next_beat:
                revalidate()
                if not heartbeat():raise SupervisorRefusal('AUTHORITY_LOST')
                next_beat=now+1
            for key,mask in selector.select(min(.1,until-now)):
                if mask&selectors.EVENT_WRITE:
                    try:n=os.write(key.fd,pending[:65536]);pending=pending[n:]
                    except BrokenPipeError:pending=memoryview(b'')
                    if not pending:selector.unregister(key.fileobj);key.fileobj.close()
                    continue
                chunk=os.read(key.fd,65536)
                if not chunk:selector.unregister(key.fileobj);continue
                if len(output)+len(errors)+len(chunk)>4194304:raise SupervisorRefusal('EXECUTOR_OUTPUT_LIMIT')
                (output if key.fileobj is process.stdout else errors).extend(chunk)
        code=process.wait(timeout=1)
        return bytes(output),code
    finally:
        stopped=stop_registered(identity,boot_id=inventory.supervisor.boot_id)
        selector.close()
        for stream in (process.stdin,process.stdout,process.stderr):
            if not stream.closed:stream.close()
        if process.poll() is None:
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:pass
        inventory.observe(attempt,{'executor_stop':stopped})
        if not stopped['process_exited']:raise SupervisorRefusal('EXECUTOR_STOP_UNPROVEN')
