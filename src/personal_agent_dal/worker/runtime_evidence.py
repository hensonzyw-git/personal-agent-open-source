"""Bounded, redacted local evidence; model assertions never become test passes."""
import hashlib
import json
import os
from pathlib import Path
import stat
from personal_agent_dal.machine.execution_results import _SECRET


def collect_evidence(plan, reservation, process):
    artifacts=[]; git=[]; tests=[]
    def record(raw, kind, label, target):
        clean=_SECRET.sub('[REDACTED]',raw.decode('utf-8',errors='replace')).encode()
        target.append(dict(kind=kind,artifact_id=label,sha256=hashlib.sha256(clean).hexdigest()))
    # Only digest sanitized evidence. Files remain local; no remote availability claim.
    record(process['raw'],'report','local-cli-stream-redacted',artifacts)
    roots=[(Path(plan.task_directories['reports']),'report')] if plan.task_directories else []
    total=0;visited=0
    for root,kind in roots:
        for directory,dirs,files in os.walk(root,followlinks=False):
            visited+=1
            if visited>128 or len(artifacts)>=60:break
            dirs[:]=sorted(d for d in dirs if not (Path(directory)/d).is_symlink())[:64]
            for name in sorted(files):
                if len(artifacts)>=60: break
                path=Path(directory)/name
                try:
                    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    try:
                        st=os.fstat(fd)
                        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>131072:continue
                        raw=os.read(fd,131073)
                    finally:os.close(fd)
                    total+=len(raw)
                    if total>1048576:return dict(tests=tests,git_evidence=git,artifacts=artifacts)
                    if len(raw)>131072:continue
                    suffix=path.suffix.lower()
                    actual_kind='patch' if suffix in ('.patch','.diff') else 'test_report' if name.endswith(('.junit.xml','.test.json')) else kind
                    label='local-report-'+hashlib.sha256(str(path.relative_to(root)).encode()).hexdigest()[:24]
                    record(raw,actual_kind,label,artifacts)
                    if actual_kind=='test_report':tests.append('Unverified task-produced test report: '+label+' sha256='+artifacts[-1]['sha256'])
                except OSError:continue
    # Direct metadata observations do not execute repository hooks or interpret
    # model prose as Git success. They are bounded source evidence, not a diff.
    root=Path(reservation['git'])/'repository'
    for name in ('HEAD','logs/HEAD','index','COMMIT_EDITMSG'):
        path=root/name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size>131072:continue
            record(path.read_bytes(),'report','local-git-'+name.replace('/','-'),git)
        except OSError:continue
    return dict(tests=tests,git_evidence=git,artifacts=artifacts)


def bounded_git_patch(plan, reservation, git_pin):
    """Read actual diff with pinned Git, no hooks/textconv/helper or model call."""
    import selectors
    import subprocess
    import time
    from personal_agent_dal.worker.supervisor import verify_executable
    executable=verify_executable(git_pin)
    argv=[str(executable),'--no-pager','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
          '-C',reservation['workspace'],'diff','--no-ext-diff','--no-textconv','HEAD','--']
    env={k:v for k,v in plan.environment.items() if k not in ('CODEX_HOME','CLAUDE_CONFIG_DIR')}
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
        if child.poll() is None:child.kill();child.wait(timeout=1)
