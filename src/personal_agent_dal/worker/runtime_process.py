"""One owned process group, bounded output/deadline and identity-aware stop.

This bounds ordinary CLI children, not arbitrary hostile detached descendants.
"""
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from personal_agent_dal.worker.role_adapter import LaunchPlan
from personal_agent_dal.worker.supervisor import SupervisorRefusal, require_machine_acceptance


def process_identity(pid):
    if sys.platform=='linux':
        raw=Path('/proc')/str(pid)/'stat'
        try:return raw.read_text().rsplit(')',1)[1].split()[19]
        except FileNotFoundError:return None
    if sys.platform != 'darwin':
        raise SupervisorRefusal('PROCESS_IDENTITY_PLATFORM_UNSUPPORTED')
    # Darwin public libproc ABI, sys/proc_info.h PROC_PIDTBSDINFO.
    # Microsecond start identity avoids ps's one-second text precision.
    import ctypes
    class BSDInfo(ctypes.Structure):
        _fields_=[('prefix',ctypes.c_uint32*12),('comm',ctypes.c_char*16),
                  ('name',ctypes.c_char*32),('suffix',ctypes.c_uint32*6),
                  ('seconds',ctypes.c_uint64),('microseconds',ctypes.c_uint64)]
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    info=BSDInfo()
    size=lib.proc_pidinfo(pid,3,0,ctypes.byref(info),ctypes.sizeof(info))
    if size!=ctypes.sizeof(info):return None
    return str(info.seconds)+':'+str(info.microseconds)



def os_boot_id():
    if sys.platform == 'linux':
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    from personal_agent_dal.worker.supervisor import current_boot_id
    return current_boot_id()


def stop_registered(observation, *, boot_id):
    """Never infer ownership from a PID alone. Caller holds the lifetime lock."""
    stop = dict(requested=False, forced=False, process_exited=False)
    pid = observation.get('pid')
    if observation.get('boot_id') != boot_id:
        return dict(stop, process_exited=bool(observation.get('boot_id')), reason='BOOT_CHANGED_NO_SIGNAL')
    if type(pid) is not int or pid <= 1 or observation.get('pgid') != pid or not observation.get('process_start'):
        return dict(stop, reason='PROCESS_UNREGISTERED_NO_SIGNAL')
    identity = observation['process_start']
    def own():
        try:
            return process_identity(pid) == identity and os.getpgid(pid) == pid
        except ProcessLookupError:
            return False
    def group_absent():
        # A reused PID is not evidence that the registered process is absent.
        current = process_identity(pid)
        if current is not None and current != identity:
            return False
        try: os.killpg(pid, 0)
        except ProcessLookupError: return process_identity(pid) is None
        except PermissionError: return False
        return False
    def wait_absent():
        end = time.monotonic() + 2
        while True:
            # Reap only a direct child; ECHILD is normal after supervisor restart.
            try: os.waitpid(pid, os.WNOHANG)
            except ChildProcessError: pass
            if group_absent(): return True
            remaining = end - time.monotonic()
            if remaining <= 0: return False
            time.sleep(min(.02, remaining))
    if not own():
        # A vanished leader does not prove its ordinary descendants exited.
        # A surviving or reused group is unowned and cannot be signalled.
        return dict(stop, process_exited=wait_absent(), reason='IDENTITY_ABSENT_NO_SIGNAL')
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not own():
            return dict(stop, process_exited=wait_absent(), reason='OWNER_LOST_BOUNDED_STOP')
        try: os.killpg(pid, sig)
        except ProcessLookupError:
            return dict(stop, process_exited=wait_absent(), reason='OWNER_LOST_BOUNDED_STOP')
        stop['requested'] = True
        stop['forced'] |= sig == signal.SIGKILL
        if wait_absent():
            return dict(stop, process_exited=True, reason='OWNER_LOST_BOUNDED_STOP')
    stop['process_exited'] = group_absent()
    return dict(stop, reason='OWNER_LOST_BOUNDED_STOP')


def fixture_plan(reservation, mode='success', *, wall_seconds=2, output_bytes=4194304):
    if mode not in ('success','timeout','output','events','unicode_report','escaped_report','malformed','truncated','provider_error','stderr_secret','descendant'):raise SupervisorRefusal('FIXTURE_UNKNOWN')
    if not 0<wall_seconds<=600 or not 0<output_bytes<=4194304:raise SupervisorRefusal('FIXTURE_BUDGET_INVALID')
    fixture=Path(__file__).with_name('runtime_fixture.py').resolve()
    return LaunchPlan((str(Path(sys.executable).resolve()),'-I','-B',str(fixture),mode),reservation['temp'],
        {'PATH':'/usr/bin:/bin','HOME':reservation['temp'],'TMPDIR':reservation['temp']},
        'synthetic_fixture',hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),'fixed-fixture/1',wall_seconds,output_bytes,
        fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest())



def _abort_owned_child(process):
    """Only for a direct child whose handle has not yet been reaped."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def run_process(inventory, attempt, plan, *, heartbeat, deadline, prompt=b''):
    # Synthetic classification is not a caller-controlled executable capability.
    if plan.runtime=='synthetic_fixture':
        row=inventory.get(attempt);reservation=inventory.supervisor.validate(row['reservation_id'])
        expected=fixture_plan(reservation,plan.argv[-1],wall_seconds=plan.wall_seconds,output_bytes=plan.output_bytes)
        if plan!=expected:raise SupervisorRefusal('FIXTURE_PLAN_MISMATCH')
    else:
        from personal_agent_dal.worker.runtime_admission import revalidate_plan
        revalidate_plan(plan,inventory,attempt)
    if not heartbeat():raise SupervisorRefusal('AUTHORITY_LOST_BEFORE_START')
    inventory.transition(attempt,'granted','starting',observation={'plan':asdict(plan),'boot_id':inventory.supervisor.boot_id,
        'owner_pid':os.getpid(),'owner_start':process_identity(os.getpid())})
    # Crash from here through PID registration is unknown, never a retry.
    if plan.runtime!='synthetic_fixture':revalidate_plan(plan,inventory,attempt)
    process=subprocess.Popen(plan.argv,cwd=plan.cwd,env=plan.environment,stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,close_fds=True,start_new_session=True)
    try:
        identity=process_identity(process.pid)
        if identity is None:raise SupervisorRefusal('PROCESS_IDENTITY_UNPROVEN')
    except BaseException:
        _abort_owned_child(process)
        raise
    observation={'pid':process.pid,'pgid':process.pid,'process_start':identity,'started_at':int(time.time())}
    try:inventory.transition(attempt,'starting','running',observation=observation)
    except BaseException:
        _abort_owned_child(process)
        raise
    output=bytearray();errors=bytearray();event_count=0;reason=None;forced=False;requested=False;truncated=False
    sel=selectors.DefaultSelector()
    try:
        for stream in (process.stdout,process.stderr):os.set_blocking(stream.fileno(),False);sel.register(stream,selectors.EVENT_READ)
        os.set_blocking(process.stdin.fileno(),False)
        pending=memoryview(prompt)
        if pending:sel.register(process.stdin,selectors.EVENT_WRITE)
        else:process.stdin.close()
    except BaseException:
        _abort_owned_child(process)
        sel.close()
        raise
    end=min(time.monotonic()+plan.wall_seconds,time.monotonic()+max(0,deadline-time.time()-10))
    next_beat=time.monotonic()
    stop_finished=False
    try:
        while sel.get_map() or process.poll() is None:
            now=time.monotonic()
            if now>=end:reason='CLI_TIMEOUT'
            if now>=next_beat:
                try:alive=heartbeat()
                except Exception:alive=False
                if not alive:reason='AUTHORITY_LOST'
                next_beat=now+1
            if reason:break
            for key,mask in sel.select(min(.1,max(0,end-now))):
                if mask & selectors.EVENT_WRITE:
                    try:n=os.write(key.fd,pending[:65536]);pending=pending[n:]
                    except BrokenPipeError:pending=memoryview(b'')
                    if not pending:sel.unregister(key.fileobj);key.fileobj.close()
                    continue
                chunk=os.read(key.fd,65536)
                if not chunk:sel.unregister(key.fileobj);continue
                if len(output)+len(errors)+len(chunk)>plan.output_bytes:reason='CLI_OUTPUT_LIMIT';truncated=True;break
                (output if key.fileobj is process.stdout else errors).extend(chunk)
                if key.fileobj is process.stdout:
                    event_count += chunk.count(b'\n')
                    if event_count > plan.max_steps: reason='CLI_EVENT_STEP_LIMIT';break
        # An unreaped direct child anchors ownership while ordinary children
        # are stopped. Once reaped, never signal a surviving unowned group.
        process.poll()
        stop = stop_registered(dict(observation,boot_id=inventory.supervisor.boot_id),
                               boot_id=inventory.supervisor.boot_id)
        if process.returncode is None:
            try: process.wait(timeout=1)
            except subprocess.TimeoutExpired: pass
        stop_finished=True
        requested,forced = stop['requested'],stop['forced']
        if not stop['process_exited']: reason = 'PROCESS_GROUP_STOP_UNPROVEN'
        return dict(raw=bytes(output),stderr=bytes(errors),event_count=event_count,exit_code=process.returncode,reason=reason,
            truncated=truncated,stop={'requested':requested,'forced':forced,'process_exited':stop['process_exited']},
            started_at=observation['started_at'],ended_at=int(time.time()))
    finally:
        if not stop_finished:
            stop_registered(dict(observation,boot_id=inventory.supervisor.boot_id),
                            boot_id=inventory.supervisor.boot_id)
        sel.close()
        for stream in (process.stdin,process.stdout,process.stderr):
            if not stream.closed:stream.close()
