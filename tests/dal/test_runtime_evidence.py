"""Synthetic filesystem and controlled helper faults; no provider calls."""
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from personal_agent_dal.worker import runtime_evidence as evidence
from personal_agent_dal.worker.supervisor import SupervisorRefusal
from tests.dal.test_trusted_runtime import runtime, world


@pytest.fixture
def files(tmp_path):
    root=tmp_path.resolve()
    repo=root/'git'/'repository';repo.mkdir(parents=True)
    reports=root/'reports';reports.mkdir()
    return SimpleNamespace(task_directories={'reports':str(reports)}), {'git':str(repo.parent)}, repo


def collect(files):
    plan,reservation,_=files
    return evidence.collect_evidence(plan,reservation,dict(raw=b'api_key=synthetic-secret-value',stop={'process_exited':True}))


@pytest.mark.parametrize('shape',['symlink','fifo','hardlink','oversized','directory','intermediate'])
def test_metadata_rejects_unsafe_files(files,shape):
    _,_,repo=files
    source=repo.parent/'synthetic';source.write_bytes(b'synthetic')
    target=repo/'HEAD'
    if shape=='symlink':target.symlink_to(source)
    elif shape=='fifo':os.mkfifo(target)
    elif shape=='hardlink':os.link(source,target)
    elif shape=='oversized':target.write_bytes(b'x'*131073)
    elif shape=='directory':target.mkdir()
    else:
        outside=repo.parent/'other';outside.mkdir();(outside/'HEAD').write_bytes(b'synthetic')
        (repo/'logs').symlink_to(outside,target_is_directory=True)
    assert collect(files)['git_evidence']==[]


def test_metadata_growth_during_same_fd_read_rejected(files,monkeypatch):
    _,_,repo=files
    target=repo/'HEAD';target.write_bytes(b'initial')
    original=os.read
    def growing(fd,count):
        with target.open('ab') as stream:stream.write(b'x'*131073)
        return original(fd,count)
    monkeypatch.setattr(os,'read',growing)
    assert collect(files)['git_evidence']==[]


def test_redaction_before_digest_and_explicit_metadata_label(files):
    _,_,repo=files
    (repo/'HEAD').write_bytes(b'api_key=synthetic-secret-value')
    result=collect(files)
    assert result['git_evidence']==[dict(kind='report',artifact_id='unverified-task-git-HEAD',
        sha256=hashlib.sha256(b'[REDACTED]').hexdigest())]


def test_unproven_stop_reads_no_files_or_directories(files,monkeypatch):
    def forbidden(*args,**kwargs):pytest.fail('task filesystem accessed')
    monkeypatch.setattr(os,'open',forbidden)
    monkeypatch.setattr(os,'scandir',forbidden)
    result=evidence.collect_evidence(files[0],files[1],dict(raw=b'api_key=synthetic-secret-value',stop={'process_exited':False}))
    assert result['git_evidence']==[] and len(result['artifacts'])==1
    assert result['artifacts'][0]['sha256']==hashlib.sha256(b'[REDACTED]').hexdigest()


def test_report_root_and_intermediate_symlinks_rejected(files):
    plan,_,repo=files
    outside=repo.parent/'other';outside.mkdir();(outside/'report').write_text('synthetic')
    reports=Path(plan.task_directories['reports'])
    (reports/'alias').symlink_to(outside,target_is_directory=True)
    assert len(collect(files)['artifacts'])==1
    plan.task_directories['reports']=str(reports/'alias')
    assert len(collect(files)['artifacts'])==1


@pytest.mark.parametrize('unkillable',[False,True])
def test_helper_bounded_cleanup_classified(files,monkeypatch,unkillable):
    class Child:
        stdout=io.BytesIO()
        waits=[]
        done=False
        def poll(self):return 0 if self.done else None
        def kill(self):pass
        def wait(self,timeout):
            self.waits.append(timeout)
            if unkillable or len(self.waits)==1:raise subprocess.TimeoutExpired('synthetic',timeout)
            self.done=True
            return -9
    class Selector:
        def register(self,*args):pass
        def get_map(self):return {}
        def close(self):pass
    child=Child()
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:child)
    monkeypatch.setattr('selectors.DefaultSelector',Selector)
    monkeypatch.setattr('personal_agent_dal.worker.supervisor.verify_executable',lambda pin:Path('/synthetic/git'))
    plan,reservation,_=files;plan.environment={};reservation['workspace']='/synthetic'
    if unkillable:
        with pytest.raises(SupervisorRefusal,match='^GIT_EVIDENCE_STOP_UNPROVEN$'):
            evidence.bounded_git_patch(plan,reservation,{})
    else:assert evidence.bounded_git_patch(plan,reservation,{}) is None
    assert len(child.waits)==2 and child.waits[-1]==1


@pytest.mark.parametrize('boot_evidence',['same','changed','config_only','unavailable','missing','timeout'])
def test_evidence_helper_unknown_persists_and_prevents_relaunch(runtime,monkeypatch,boot_evidence):
    from personal_agent_dal.worker.trusted_runtime import (
        prepare_runtime,execute_runtime,reconcile_runtime,prepare_runtime_isolation)
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    from personal_agent_dal.worker.runtime_process import stop_registered
    from personal_agent_dal.worker.supervisor import Supervisor
    t,l,c,s,k=runtime
    row=prepare_runtime(t,l,c,**k)
    # Use the actual provisioning schema without creating admission/provider state.
    with s._db() as db:
        db.execute("INSERT INTO runtime_provisioning(reservation_id,body) VALUES (?,?)",
                   (row['reservation_id'],'{"binding":{"git_pin":{}}}'))
    def refusal(*a,**k):raise SupervisorRefusal('GIT_EVIDENCE_STOP_UNPROVEN')
    monkeypatch.setattr(evidence,'bounded_git_patch',refusal)
    with pytest.raises(SupervisorRefusal,match='GIT_EVIDENCE_STOP_UNPROVEN'):
        execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    inv=RuntimeInventory(s)
    failed=inv.get(c['attempt_id'])
    assert failed['state']=='unknown'
    marker=failed['observation']['evidence_helper_stop_unproven']
    assert marker==dict(boot_id='boot',reason='GIT_EVIDENCE_STOP_UNPROVEN')
    # Real fixed main CLI is gone: its successful stop is not a helper witness.
    assert stop_registered(failed['observation'],boot_id='boot')['process_exited']
    other='-another'  # Sort before the UUID so public cursor reconciliation selects only the old attempt.
    another=s.reserve(attempt_id=other,workspace_id='another',generation=1,authority={},read_roots=[])
    inv.adopt(dict(c,attempt_id=other),another)
    inv.transition(other,'prepared','dispatch_requested')
    inv.transition(other,'dispatch_requested','granted')
    monkeypatch.setattr('personal_agent_dal.worker.runtime_process.os_boot_id',lambda:'boot')
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('unknown replay launched'))
    with pytest.raises(SupervisorRefusal,match='EXECUTION_EFFECTS_UNKNOWN'):
        execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    reopened=Supervisor(s.root,boot_id='boot',epoch=s.epoch)
    for _ in range(2):
        # Fresh inventory and recovered successful server status must not clear it.
        reconcile_runtime(reopened,t,after=other)
        stopped=RuntimeInventory(reopened).get(c['attempt_id'])
        assert stopped['observation']['evidence_helper_stop_unproven']==marker
        assert not stopped['observation']['reconciliation_stop']['process_exited']
        with pytest.raises(SupervisorRefusal,match='UNRESOLVED_PROCESS_OWNERSHIP'):
            inv.transition(other,'granted','starting')
        with pytest.raises(SupervisorRefusal,match='ISOLATION_STOP_UNPROVEN'):
            prepare_runtime_isolation(reopened,{'binding':{'attempt_id':c['attempt_id']}},key=k['key'],read_roots=[])
    reopened=Supervisor(s.root,boot_id='new-boot' if boot_evidence in ('changed','config_only') else 'boot',epoch=s.epoch)
    def observed_boot():
        if boot_evidence=='unavailable':raise OSError('boot query failed')
        if boot_evidence=='timeout':raise subprocess.TimeoutExpired('synthetic boot query',5)
        return {'changed':'new-boot','missing':''}.get(boot_evidence,'boot')
    monkeypatch.setattr('personal_agent_dal.worker.runtime_process.os_boot_id',observed_boot)
    reconcile_runtime(reopened,after=other)
    stop=inv.get(c['attempt_id'])['observation']['reconciliation_stop']
    assert stop['process_exited'] is (boot_evidence=='changed')
    if boot_evidence=='changed':
        assert stop['reason']=='BOOT_CHANGED_NO_SIGNAL'
        assert stop['previous_boot_id']=='boot' and stop['boot_id']=='new-boot'
        inv.transition(other,'granted','starting')
    else:
        with pytest.raises(SupervisorRefusal,match='UNRESOLVED_PROCESS_OWNERSHIP'):
            inv.transition(other,'granted','starting')


def test_output_limit_and_unproven_stop_keep_truncation_and_unknown(runtime,monkeypatch):
    from personal_agent_dal.worker import runtime_process
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime,execute_runtime
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    original=runtime_process.stop_registered
    def unproven(*a,**k):
        stopped=original(*a,**k)  # actually stop the ordinary fixture
        return dict(stopped,process_exited=False)
    monkeypatch.setattr(runtime_process,'stop_registered',unproven)
    t,l,c,s,k=runtime;k['fixture']={'mode':'output','output_bytes':64}
    prepare_runtime(t,l,c,**k)
    response=execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    result=RuntimeInventory(s).get(c['attempt_id'])['result']['result']
    assert result['truncated'] is True
    assert result['reason']=='PROCESS_GROUP_STOP_UNPROVEN' and result['outcome']=='unknown'
    assert not response['accepted']
    assert len(result['artifacts'])==1 and not result['git_evidence']
    assert any('Git metadata are unverified' in item for item in result['unverified'])


@pytest.mark.parametrize('mode',['patch','precreated','nonzero'])
def test_patch_is_local_redacted_digest_or_conservative_absence(files,monkeypatch,mode):
    raw=b'+api_key=synthetic-secret-value'
    source=Path(files[1]['git'])/'synthetic-output';source.write_bytes(raw)
    class Child:
        stdout=source.open('rb')
        def poll(self):return 1 if mode=='nonzero' else 0
        def wait(self,timeout):return self.poll()
    class Selector:
        active=True
        def register(self,*args):pass
        def get_map(self):return self.active
        def select(self,timeout):return [(SimpleNamespace(fd=child.stdout.fileno(),fileobj=child.stdout),1)]
        def unregister(self,*args):self.active=False
        def close(self):pass
    child=Child()
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:child)
    monkeypatch.setattr('selectors.DefaultSelector',Selector)
    monkeypatch.setattr('personal_agent_dal.worker.supervisor.verify_executable',lambda pin:Path('/synthetic/git'))
    plan,reservation,_=files;plan.environment={};reservation['workspace']='/synthetic'
    sha=hashlib.sha256(b'+[REDACTED]').hexdigest()
    path=Path(plan.task_directories['reports'])/('observed-git-'+sha+'.patch')
    if mode=='precreated':path.write_bytes(b'precreated')
    result=evidence.bounded_git_patch(plan,reservation,{})
    if mode=='patch':
        assert result==dict(kind='patch',artifact_id=path.name,sha256=sha)
        assert path.read_bytes()==b'+[REDACTED]'
    else:
        assert result is None
        if mode=='precreated':assert path.read_bytes()==b'precreated'
