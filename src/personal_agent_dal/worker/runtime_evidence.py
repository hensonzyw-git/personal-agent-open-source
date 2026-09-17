"""Bounded, redacted local evidence; model assertions never become test passes."""
import hashlib
import json
import os
from pathlib import Path
import stat
from personal_agent_dal.machine.execution_results import _SECRET


_FILE_LIMIT = 131072
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK


def _open_directory(path):
    """Anchor every component at a held dirfd; never follow directory symlinks."""
    path = Path(path)
    fd = os.open('/' if path.is_absolute() else '.', _DIR_FLAGS)
    try:
        for part in path.parts:
            if part in ('/', '.'): continue
            if part == '..': raise OSError('parent traversal refused')
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_regular(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _FILE_LIMIT:
            return None
        raw = bytearray()
        while len(raw) <= _FILE_LIMIT:
            chunk = os.read(fd, _FILE_LIMIT + 1 - len(raw))
            if not chunk: break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (len(raw) > _FILE_LIMIT or after.st_nlink != 1 or
                after.st_size != len(raw) or
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            return None
        return bytes(raw)
    finally:
        os.close(fd)


def collect_evidence(plan, reservation, process):
    artifacts=[]; git=[]; tests=[]
    result=dict(tests=tests,git_evidence=git,artifacts=artifacts)
    def record(raw, kind, label, target):
        clean=_SECRET.sub('[REDACTED]',raw.decode('utf-8',errors='replace')).encode()
        target.append(dict(kind=kind,artifact_id=label,sha256=hashlib.sha256(clean).hexdigest()))
    record(process['raw'],'report','local-cli-stream-redacted',artifacts)
    # Even directory enumeration is forbidden while task group absence is unknown.
    if not process.get('stop', {}).get('process_exited'):
        return result
    total=0;visited=0
    def reports(fd, prefix=''):
        nonlocal total, visited
        visited += 1
        if visited > 128 or len(artifacts) >= 60: return
        # Bound enumeration as well as content; names are task-produced.
        try:
            with os.scandir(fd) as entries:
                names=[]
                for entry in entries:
                    names.append(entry.name)
                    if len(names) >= 128: break
        except OSError:
            return
        for name in sorted(names):
            if total > 1048576 or len(artifacts) >= 60: return
            relative=prefix+name
            try:
                child=os.open(name,_DIR_FLAGS,dir_fd=fd)
            except OSError:
                child=None
            if child is not None:
                try:
                    if visited < 128: reports(child,relative+'/')
                finally: os.close(child)
                continue
            try: raw=_read_regular(fd,name)
            except OSError: continue
            if raw is None: continue
            total+=len(raw)
            if total>1048576:return
            kind='patch' if Path(name).suffix.lower() in ('.patch','.diff') else 'test_report' if name.endswith(('.junit.xml','.test.json')) else 'report'
            label='local-report-'+hashlib.sha256(relative.encode()).hexdigest()[:24]
            record(raw,kind,label,artifacts)
            if kind=='test_report':tests.append('Unverified task-produced test report: '+label+' sha256='+artifacts[-1]['sha256'])
    if plan.task_directories:
        try: fd=_open_directory(plan.task_directories['reports'])
        except OSError: fd=None
        if fd is not None:
            try: reports(fd)
            finally: os.close(fd)
    # Writable metadata is task-produced input, never independent Git success.
    root=Path(reservation['git'])/'repository'
    for name in ('HEAD','logs/HEAD','index','COMMIT_EDITMSG'):
        fd=None
        try:
            fd=_open_directory(root/Path(name).parent)
            raw=_read_regular(fd,Path(name).name)
            if raw is not None:
                record(raw,'report','unverified-task-git-'+name.replace('/','-'),git)
        except OSError: pass
        finally:
            if fd is not None: os.close(fd)
    return result


def bounded_git_patch(plan, reservation, git_pin):
    """Read actual diff with pinned Git, no hooks/textconv/helper or model call."""
    import selectors
    import subprocess
    import time
    from personal_agent_dal.worker.supervisor import verify_executable
    executable=verify_executable(git_pin)
    argv=[str(executable),'--no-pager','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
          '-C',reservation['workspace'],'diff','--no-ext-diff','--no-textconv','HEAD','--']
    env=git_environment(reservation)
    child=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    stream=selectors.DefaultSelector();stream.register(child.stdout,selectors.EVENT_READ)
    raw=bytearray();end=time.monotonic()+3
    try:
        while stream.get_map():
            if time.monotonic()>=end:return None
            for key,_ in stream.select(.05):
                chunk=os.read(key.fd,65536)
                if not chunk:stream.unregister(key.fileobj);continue
                raw.extend(chunk)
                if len(raw)>131072:return None
        if child.wait(timeout=max(.01,end-time.monotonic()))!=0:return None
        clean=_SECRET.sub('[REDACTED]',bytes(raw).decode('utf-8',errors='replace')).encode()
        sha=hashlib.sha256(clean).hexdigest()
        name='observed-git-'+sha+'.patch'
        path=Path(plan.task_directories['reports'])/name
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb',closefd=False) as f:f.write(clean)
        finally:os.close(fd)
        return dict(kind='patch',artifact_id=name,sha256=sha)
    except (OSError,subprocess.TimeoutExpired):return None
    finally:
        stream.close();child.stdout.close()
        if child.poll() is None:
            from personal_agent_dal.worker.supervisor import SupervisorRefusal
            try:
                child.kill()
                child.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                if child.poll() is None:
                    raise SupervisorRefusal('GIT_EVIDENCE_STOP_UNPROVEN') from None


def git_environment(reservation):
    """No runtime, credential-helper, proxy or caller environment inheritance."""
    return {'PATH':'/usr/bin:/bin', 'HOME':reservation.get('temp', '/dev/null'),
        'TMPDIR':reservation.get('temp', '/tmp'), 'GIT_CONFIG_NOSYSTEM':'1',
        'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_TERMINAL_PROMPT':'0',
        'GIT_CONFIG_COUNT':'4',
        'GIT_CONFIG_KEY_0':'core.hooksPath', 'GIT_CONFIG_VALUE_0':'/dev/null',
        'GIT_CONFIG_KEY_1':'core.fsmonitor', 'GIT_CONFIG_VALUE_1':'false',
        'GIT_CONFIG_KEY_2':'protocol.allow', 'GIT_CONFIG_VALUE_2':'never',
        'GIT_CONFIG_KEY_3':'credential.helper', 'GIT_CONFIG_VALUE_3':''}
