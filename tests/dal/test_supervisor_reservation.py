"""Synthetic filesystem control-flow checks, never macOS acceptance."""
import os
import json
from pathlib import Path

import pytest
from personal_agent_dal.worker.supervisor import Supervisor, SupervisorRefusal


def supervisor(tmp_path, **kw):
    root = tmp_path / 'supervisor'
    return Supervisor(root, boot_id=kw.get('boot_id', 'boot'), epoch=kw.get('epoch', 1))


def reserve(s, attempt='attempt'):
    return s.reserve(attempt_id=attempt, workspace_id=attempt, generation=1,
                     authority={'job_id': 'job', 'lease_id': 'lease'}, read_roots=())


APPROVED_LITERAL_RULES = (
    '(allow file-read* (literal "/"))',
    '(allow file-read* file-write* (literal "/dev/null"))',
)


@pytest.mark.parametrize('rule', APPROVED_LITERAL_RULES)
def test_profile_has_exact_approved_literal_exception(tmp_path, rule):
    s = supervisor(tmp_path)
    r = reserve(s)
    assert s.sandbox_profile(r['reservation_id']).splitlines().count(rule) == 1


def test_profile_preserves_directory_and_protected_boundaries(tmp_path):
    s = supervisor(tmp_path)
    runtime = tmp_path/'runtime'; runtime.mkdir()
    r = s.reserve(attempt_id='old', workspace_id='old', generation=1,
                  authority={}, read_roots=(runtime,))
    replacement = reserve(s, 'replacement')
    profile = s.sandbox_profile(r['reservation_id'])
    # Removing only the approved exceptions must leave the entire old policy.
    ordinary = [line for line in profile.splitlines() if line not in APPROVED_LITERAL_RULES]
    writable = [r['workspace'], r['temp'], r['git']]
    assert ordinary == [
        '(version 1)', '(deny default)', '(allow process*)', '(allow sysctl-read)',
        '(allow file-read-metadata)',
        *['(allow file-read* (subpath '+json.dumps(p)+'))' for p in [str(runtime), *writable]],
        *['(allow file-write* (subpath '+json.dumps(p)+'))' for p in writable],
    ]
    assert r['read_roots'] == [str(runtime)]
    assert s.validate(r['reservation_id'])['read_roots'] == [str(runtime)]
    assert '(subpath "/")' not in profile
    assert '(subpath "/dev")' not in profile
    assert '(allow file-write* (literal "/"))' not in profile
    for path in [*(replacement[k] for k in ('workspace', 'git', 'temp', 'parent')),
                 str(s.root), str(s.root/'synthetic-signing-canary')]:
        assert json.dumps(path) not in profile


def test_root_read_root_is_refused(tmp_path):
    s = supervisor(tmp_path)
    with pytest.raises(SupervisorRefusal, match='ROOT_OVERLAP'):
        s.reserve(attempt_id='root', workspace_id='root', generation=1,
                  authority={}, read_roots=('/',))


def test_machine_acceptance_guard_never_spawns(monkeypatch):
    from personal_agent_dal.worker.supervisor import require_machine_acceptance
    calls = []
    monkeypatch.setattr('subprocess.Popen', lambda *a, **kw: calls.append(1))
    with pytest.raises(SupervisorRefusal, match='MINI_ACCEPTANCE_REQUIRED'):
        require_machine_acceptance()
    assert calls == []


def test_missing_machine_proof_never_executes(tmp_path):
    s = supervisor(tmp_path)
    r = reserve(s)
    calls = []
    with pytest.raises(SupervisorRefusal, match='MINI_ACCEPTANCE_REQUIRED'):
        s.launch(r['reservation_id'], executor=lambda _: calls.append(1))
    assert calls == []


def test_reopen_reservation_and_competing_owner(tmp_path):
    s = supervisor(tmp_path)
    r = reserve(s)
    assert supervisor(tmp_path).validate(r['reservation_id']) == r
    with pytest.raises(SupervisorRefusal, match='RESERVATION_CONFLICT'):
        s.reserve(attempt_id='other', workspace_id='attempt', generation=1,
                  authority={'job_id':'other'}, read_roots=())


@pytest.mark.parametrize('change', ['boot', 'epoch', 'directory', 'symlink', 'hardlink', 'parent'])
def test_drift_refused(tmp_path, change):
    s = supervisor(tmp_path)
    r = reserve(s)
    workspace = Path(r['workspace'])
    if change == 'boot': s = supervisor(tmp_path, boot_id='new')
    elif change == 'epoch': s = supervisor(tmp_path, epoch=2)
    elif change == 'directory':
        workspace.rename(workspace.with_name('old'))
        workspace.mkdir()
    elif change == 'symlink': (workspace/'bad').symlink_to(tmp_path)
    elif change == 'hardlink':
        (workspace/'file').write_text('synthetic')
        os.link(workspace/'file', workspace/'alias')
    else: os.chmod(workspace.parent, 0o777)
    with pytest.raises(SupervisorRefusal): s.validate(r['reservation_id'])


def test_shared_read_parent_and_historical_manifest_refused(tmp_path):
    s=supervisor(tmp_path)
    with pytest.raises(SupervisorRefusal, match='ROOT_OVERLAP'):
        s.reserve(attempt_id='a', workspace_id='a', generation=1,
                  authority={}, read_roots=(tmp_path,))
    with pytest.raises(SupervisorRefusal, match='HISTORICAL'):
        s.validate('never-recorded')


def test_legacy_coder_and_toolchain_spawn_gate_never_calls_popen(monkeypatch):
    from personal_agent_dal.worker.supervisor import spawn_unaccepted
    calls=[]
    monkeypatch.setattr('subprocess.Popen',lambda *a,**kw:calls.append(1))
    with pytest.raises(SupervisorRefusal,match='MACHINE_PROOF_REQUIRED'):
        spawn_unaccepted(['/explicit/pinned/cli'],env={'SYNTHETIC':'canary'})
    assert calls==[]


def test_controlled_crash_persists_unknown_reservation(tmp_path):
    s=supervisor(tmp_path);r=reserve(s);calls=[]
    def crash(_):raise RuntimeError('synthetic crash')
    with pytest.raises(RuntimeError):
        s.controlled_prelaunch(r['reservation_id'],commit_permission=lambda:'DISPATCH_GRANTED',executor=crash)
    with pytest.raises(SupervisorRefusal,match='ALREADY_DISPATCHED'):
        supervisor(tmp_path).controlled_prelaunch(r['reservation_id'],commit_permission=lambda:'DISPATCH_GRANTED',executor=lambda _:calls.append(1))
    assert calls==[]


def test_competing_process_reservations_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    s=supervisor(tmp_path)
    def competing(attempt):
        try:
            return s.reserve(attempt_id=attempt,workspace_id='shared',generation=1,authority={},read_roots=())
        except SupervisorRefusal:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(competing,['first','second']))
    assert sum(r is not None for r in results)==1


def test_independent_git_metadata_no_shared_objects(tmp_path):
    import hashlib
    import subprocess
    from personal_agent_dal.worker.supervisor import provision_repository
    # Explicit test executable pin. No provider and no user's repository read.
    git=Path('/home/example/private-path').resolve()
    if not git.exists():pytest.skip('explicit local Git fixture unavailable')
    source=tmp_path/'source';source.mkdir()
    environment={'PATH':str(git.parent)+':/usr/bin:/bin','HOME':str(tmp_path),
        'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
    def run(*args):
        return subprocess.run([str(git),'-C',str(source),*args],env=environment,
            check=True,capture_output=True,text=True).stdout.strip()
    run('init');(source/'synthetic.txt').write_text('synthetic fixture')
    run('add','synthetic.txt')
    run('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-m','synthetic')
    base=run('rev-parse','HEAD')
    s=supervisor(tmp_path);r=reserve(s)
    pin={'executable':str(git),'executable_sha256':hashlib.sha256(git.read_bytes()).hexdigest(),'version':run('--version')}
    marker=tmp_path/'provision-marker'
    hook=tmp_path/'monitor';hook.write_text('#!/bin/sh\n/usr/bin/touch "'+str(marker)+'"\n');hook.chmod(0o700)
    run('config','core.fsmonitor',str(hook))
    run('config','core.hooksPath',str(tmp_path))
    result=provision_repository(s,r['reservation_id'],source=source,base_sha=base,git_pin=pin)
    assert not marker.exists()
    assert (Path(result['workspace'])/'synthetic.txt').read_text()=='synthetic fixture'
    assert (Path(result['workspace'])/'.git').is_file()
    assert not (Path(result['git'])/'objects'/'info'/'alternates').exists()
    assert all(p.stat().st_nlink==1 for p in Path(result['git']).rglob('*') if p.is_file())
    assert s.validate(r['reservation_id'])==result


def test_synthetic_spawn_has_durable_inventory_before_process(tmp_path,monkeypatch):
    import hashlib
    import sqlite3
    from unittest.mock import Mock
    binary=tmp_path/'runtime';binary.mkdir();executable=binary/'python';executable.write_bytes(b'synthetic');executable.chmod(0o700)
    s=supervisor(tmp_path)
    r=s.reserve(attempt_id='probe',workspace_id='probe',generation=1,authority={},read_roots=(binary,))
    expected_policy = s.sandbox_profile(r['reservation_id'])
    monkeypatch.setenv('DAL_SYNTHETIC_PARENT_CANARY', 'synthetic-only')
    calls=[]
    def popen(*args,**kwargs):
        policy = args[0][2]
        assert args[0] == [str(executable), '-p', expected_policy, str(executable)]
        for rule in APPROVED_LITERAL_RULES:
            assert policy.splitlines().count(rule) == 1
        policy_digest = hashlib.sha256(policy.encode()).hexdigest()
        assert r['sandbox_policy_sha256'] == policy_digest
        with sqlite3.connect(s.root/'reservations.sqlite3') as independent:
            row=independent.execute('SELECT pid,body FROM launch_inventory').fetchone()
            assert row[0] is None
            assert json.loads(row[1])['policy_sha256'] == policy_digest
        assert kwargs['close_fds'] is True and 'DAL_SYNTHETIC_PARENT_CANARY' not in kwargs['env']
        assert kwargs['env'] == {'HOME':r['temp'], 'TMPDIR':r['temp'], 'PATH':'/usr/bin:/bin'}
        calls.append(1)
        process=Mock(pid=4242)
        process.communicate.return_value=('synthetic',None);process.returncode=0
        return process
    monkeypatch.setattr('subprocess.Popen',popen)
    assert s.synthetic_process(r['reservation_id'],sandbox_pin={'executable':str(executable),'executable_sha256':hashlib.sha256(b'synthetic').hexdigest(),'version':'synthetic'},
        argv=[str(executable)],executable_sha256=hashlib.sha256(b'synthetic').hexdigest())==(0,'synthetic')
    with sqlite3.connect(s.root/'reservations.sqlite3') as db:
        assert db.execute('SELECT pid,pgid FROM launch_inventory').fetchone()==(4242,4242)
    assert calls==[1]


def test_orphan_after_interrupted_reserve_is_explicit(tmp_path, monkeypatch):
    s=supervisor(tmp_path)
    original=Path.mkdir
    def interrupted(path, *args, **kwargs):
        if path.name=='work': raise OSError('synthetic interruption')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'mkdir', interrupted)
        with pytest.raises(OSError): reserve(s)
    with pytest.raises(SupervisorRefusal, match='WORKSPACE_ORPHANED'):
        reserve(supervisor(tmp_path))
    assert (s.root/'workspace-attempt').is_dir()
    with s._db() as db:
        assert db.execute('SELECT count(*) FROM reservations').fetchone()==(0,)
        assert db.execute('SELECT count(*) FROM launch_inventory').fetchone()==(0,)


@pytest.mark.parametrize('offset,code', [(-1,'RESERVATION_FUTURE'), (900,'RESERVATION_EXPIRED'), (901,'RESERVATION_EXPIRED')])
def test_manifest_fixed_window_refuses_before_signing(tmp_path, monkeypatch, offset, code):
    from personal_agent_dal.worker.supervisor import signed_manifest
    s=supervisor(tmp_path); r=reserve(s); calls=[]
    monkeypatch.setattr('personal_agent.api.dal_client.sign_decision', lambda *a,**kw:calls.append(1))
    with pytest.raises(SupervisorRefusal, match=code):
        signed_manifest(s,r,identity={},key=None,now=r['created_at_epoch']+offset)
    assert calls==[]


@pytest.mark.parametrize('change', ['group', 'world', 'not_executable', 'parent_link', 'file_link'])
def test_worker_uses_shared_executable_checks(tmp_path, monkeypatch, change):
    import hashlib
    from types import SimpleNamespace
    from personal_agent_dal.worker import prelaunch
    from tests.dal.test_runtime_mapping import snapshot, pins
    from personal_agent_dal.machine.workflow_selection import digest
    folder=tmp_path/'binary'; folder.mkdir(); executable=folder/'cli'
    executable.write_bytes(b'synthetic'); executable.chmod(0o700)
    if change in ('group','world','not_executable'): executable.chmod({'group':0o720,'world':0o702,'not_executable':0o600}[change])
    elif change=='parent_link':
        link=tmp_path/'alias'; link.symlink_to(folder, target_is_directory=True); executable=link/'cli'
    else:
        link=folder/'alias'; link.symlink_to(executable); executable=link
    snap=snapshot(); registered=pins(snap)
    for pin in registered: pin.update(executable=str(executable), executable_sha256=hashlib.sha256(b'synthetic').hexdigest())
    body=dict(root=str(tmp_path/'supervisor'),boot_id='boot',supervisor_epoch=1,runtime_pins=registered,
        identity=dict(kid='synthetic', worker_id='w', machine_id='synthetic',
                      registration_epoch=1, boot_id='boot', supervisor_epoch=1),
        read_roots=[], signing_key_path=str(tmp_path/'synthetic-key'), git_pin={})
    monkeypatch.setattr(prelaunch,'load_supervisor_config',lambda _:body)
    monkeypatch.setattr('personal_agent_dal.worker.supervisor.current_boot_id',lambda:'boot')
    with pytest.raises(SupervisorRefusal, match='EXECUTABLE_NOT_PROTECTED|SYMLINK_PATH'):
        prelaunch.worker_prelaunch(None,SimpleNamespace(supervisor_config_path='synthetic',worker_id='w'),None,
                                  dict(worker_id='w',snapshot=snap,snapshot_sha256=digest(snap)))
