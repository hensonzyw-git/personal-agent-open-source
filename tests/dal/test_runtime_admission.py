"""Offline attestation-schema tests. These records are not real smoke receipts.

No provider process runs; production Popen is intercepted only after all actual
admission, signing, acknowledgement and dispatch code has executed.
"""
from dataclasses import asdict, replace
import copy
import hashlib
import json
import os
from pathlib import Path
import time
import pytest
from tests.dal.test_trusted_runtime import world, runtime
from personal_agent_dal.worker.supervisor import SupervisorRefusal, _digest
from personal_agent_dal.worker.role_adapter import build_plan
from personal_agent_dal.worker.runtime_admission import code_identity, plan_contract, validate_admission, applied_policy
from personal_agent_dal.worker.runtime_process import os_boot_id


def write(path, body):
    path.write_text(json.dumps(body));path.chmod(0o600)


@pytest.fixture
def admission(runtime,tmp_path,monkeypatch):
    t,l,c,s,k=runtime
    # Sandbox denies kern.bootsessionuuid; mock OS identity, never admission.
    monkeypatch.setattr('personal_agent_dal.worker.supervisor.current_boot_id',lambda:'boot')
    identity=dict(k['identity'],boot_id=os_boot_id())
    # This is an inert CLI identity for a plan that never invokes a provider.
    # The CI Python framework binary may be group-writable and is correctly
    # refused by the production executable-protection check.
    binary=Path('/bin/echo')
    pins=[dict(role=name,configuration=conf,executable=str(binary),version='offline-schema-fixture',
        executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest()) for name,conf in c['snapshot']['roles'].items()]
    adapters={'schema':'dal.role-adapters/1.0','roles':{name:dict(mode='codex_login',home=str(tmp_path/'unread-login'),environment={}) for name in c['snapshot']['roles']}}
    r=s.reserve(attempt_id='admission-test',workspace_id='admission-test',generation=1,authority={},read_roots=[])
    config=tmp_path/'config.json';write(config,dict(pins=pins,adapters=adapters))
    path=tmp_path/'admission.json'
    binding=dict(path=str(path),identity=identity,pins=pins,adapters=adapters,config_refs={str(config):_digest(json.loads(config.read_text()))})
    now=int(time.time());roles={}
    for name in c['snapshot']['roles']:
        plan=build_plan(dict(c,execution_role=name),r,pins,adapters)
        result=dict(exit_code=0,stdout_sha256='a'*64,stderr_sha256='b'*64,cli_version=plan.version,
            observations=['report-produced','scratch-write','source-edit','git-add','git-commit','outside-write-denied'] if name=='coder' else ['report-produced','scratch-write','business-write-denied','outside-write-denied'])
        command=list(plan.argv)
        roles[name]=dict(configuration=c['snapshot']['roles'][name],plan=plan_contract(plan,r),smoke=dict(
            command=command,command_sha256=_digest(command),result=result,result_sha256=_digest(result),
            started_at=now-2,ended_at=now-1,scope='synthetic-files-only',provenance='external-native-cli',
            task_paths={key:r[key] for key in ('workspace','temp','git')}))
    evidence=dict(schema='dal.runtime-admission/1.0',code_sha256=code_identity(),policy='trusted-single-user/1.0',
        identity=identity,roles=roles,config_refs=binding['config_refs'],issued_at=now,expires_at=now+300,
        revoked_at=None,scope='single-role-report-only',provenance='operator-attested-external-native-cli')
    write(path,evidence)
    return c,r,binding,evidence


def test_valid_structured_contract_and_exact_applied_policy(admission):
    c,r,b,e=admission
    assert validate_admission(b,context=c,reservation=r)==_digest(e)
    plan=build_plan(c,r,b['pins'],b['adapters'])
    assert r['git']+'/repository' in plan.write_roots
    assert applied_policy(plan)!=r['sandbox_policy_sha256']
    for role in ('planner','reviewer'):
        readonly=build_plan(dict(c,execution_role=role),r,b['pins'],b['adapters'])
        assert readonly.write_roots==(r['temp'],)


@pytest.mark.parametrize('field,value',[
    ('schema','old'),('code_sha256','0'*64),('policy','old'),('expires_at',1),
    ('revoked_at',1),('scope','*'),('provenance','synthetic_fixture'),
])
def test_stale_revoked_fixture_and_wildcard_refused(admission,field,value):
    c,r,b,e=admission;e[field]=value;write(Path(b['path']),e)
    with pytest.raises(SupervisorRefusal):validate_admission(b,context=c,reservation=r)


@pytest.mark.parametrize('mutation',['boot','epoch','worker','role','pin','route','template','smoke','config','permissions','version'])
def test_binding_mismatches(admission,mutation):
    c,r,b,e=admission
    if mutation=='boot':e['identity']=dict(e['identity'],boot_id='stale')
    elif mutation=='epoch':e['identity']=dict(e['identity'],supervisor_epoch=999)
    elif mutation=='worker':e['identity']=dict(e['identity'],worker_id='other')
    elif mutation=='role':e['roles']['*']=e['roles'].pop('coder')
    elif mutation=='pin':b['pins'][0]['executable_sha256']='0'*64
    elif mutation=='route':b['adapters']['roles']['coder']['environment']={'API_KEY':'synthetic'}
    elif mutation=='template':e['roles']['coder']['plan']['argv'].append('--dangerous')
    elif mutation=='smoke':e['roles']['coder']['smoke']['result']['exit_code']=128
    elif mutation=='config':write(Path(next(iter(b['config_refs']))),{})
    elif mutation=='permissions':e['roles']['coder']['plan']['write_roots'].append('/')
    elif mutation=='version':e['roles']['coder']['smoke']['result']['cli_version']='drift'
    write(Path(b['path']),e)
    with pytest.raises(SupervisorRefusal):validate_admission(b,context=c,reservation=r)


def test_owner_only_and_task_root_refusal(admission):
    c,r,b,e=admission;Path(b['path']).chmod(0o644)
    with pytest.raises(SupervisorRefusal):validate_admission(b,context=c,reservation=r)
    b['path']=str(Path(r['temp'])/'admission.json');write(Path(b['path']),e)
    with pytest.raises(SupervisorRefusal):validate_admission(b,context=c,reservation=r)


def test_exact_plan_and_revocation_revalidation(admission):
    c,r,b,e=admission;sha=validate_admission(b,context=c,reservation=r)
    plan=replace(build_plan(c,r,b['pins'],b['adapters']),production_enabled=True,admission=b,admission_sha256=sha)
    assert validate_admission(b,context=c,reservation=r,plan=plan,expected_digest=sha)==sha
    with pytest.raises(SupervisorRefusal):validate_admission(b,context=c,reservation=r,plan=replace(plan,argv=plan.argv+('bad',)),expected_digest=sha)
    e['revoked_at']=int(time.time());write(Path(b['path']),e)
    with pytest.raises(SupervisorRefusal):validate_admission(b,context=c,reservation=r,plan=plan,expected_digest=sha)


def test_signed_plan_tamper_precedes_dispatch(runtime):
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime,execute_runtime
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    t,l,c,s,k=runtime;row=prepare_runtime(t,l,c,**k)
    obs=row['observation'];obs['plan']['max_steps']=1
    with s._db() as db:db.execute('UPDATE runtime_inventory SET observation=? WHERE effective_attempt=?',(json.dumps(obs),c['attempt_id']))
    with pytest.raises(SupervisorRefusal,match='LAUNCH_PLAN_DIGEST_MISMATCH'):execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    assert RuntimeInventory(s).get(c['attempt_id'])['state']=='prepared'


def test_real_worker_composition_reaches_popen_with_signed_admission(admission,runtime,tmp_path,monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from personal_agent_dal.worker.config import load_worker_config
    from personal_agent_dal.worker.prelaunch import worker_prelaunch
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    from personal_agent_dal.storage.machine_models import ProviderAttempt
    from personal_agent_dal.storage.engine import session_factory
    import subprocess
    t,l,c,s,k=runtime;c0,r,b,e=admission
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime,execute_runtime
    k.update(fixture=None,pins=b['pins'],adapter_config=b['adapters'],admission=b)
    row=prepare_runtime(t,l,c,**k)
    assert row['observation']['production_enabled']
    plan=row['observation']['plan']
    import base64
    manifest=json.loads(base64.urlsafe_b64decode(row['observation']['assertion'].split('.')[1]+'=='))
    assert manifest['launcher_plan_sha256']==_digest(plan)
    assert plan['admission_sha256']==_digest(e)
    def no_provider(argv,**kwargs):
        assert tuple(argv)==tuple(plan['argv'])
        raise OSError('offline launch boundary reached')
    monkeypatch.setattr(subprocess,'Popen',no_provider)
    with pytest.raises(OSError,match='offline launch boundary reached'):
        execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    assert RuntimeInventory(s).get(c['attempt_id'])['state']=='unknown'


def test_worker_entry_validates_before_key_and_dispatch(admission,runtime,tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cryptography.hazmat.primitives import serialization
    from personal_agent_dal.worker.prelaunch import worker_prelaunch
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    c,r,b,e=admission;t,l,_,s,k=runtime
    keypath=tmp_path/'disposable-signing.pem'
    keypath.write_bytes(k['key'].private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));keypath.chmod(0o600)
    sup=tmp_path/'supervisor.json';adapt=tmp_path/'adapters.json';cfg=tmp_path/'worker.json'
    write(sup,dict(root=str(s.root),boot_id='boot',supervisor_epoch=1,identity=k['identity'],read_roots=[],runtime_pins=b['pins'],signing_key_path=str(keypath),git_pin={}))
    write(adapt,b['adapters']);write(cfg,{'schema_version':'dal.worker-config/2.1','admission_ref':b['path']})
    b['config_refs']={str(p):_digest(json.loads(p.read_text())) for p in (sup,adapt,cfg)}
    e['config_refs']=b['config_refs'];write(Path(b['path']),e)
    k.update(fixture=None,pins=b['pins'],adapter_config=b['adapters'],admission=b)
    prepare_runtime(t,l,c,**k)
    config=SimpleNamespace(worker_id='w',supervisor_config_path=sup,adapter_config_ref=adapt,
        config_ref=cfg,admission_ref=Path(b['path']),schema_version='dal.worker-config/2.1',
        repos={l.repository_id:SimpleNamespace(local_path=str(tmp_path/'unused-existing-reservation'))},kill_switch_path=tmp_path/'kill')
    # The existing prepared reservation is adopted, not provisioned again.
    def boundary(*args,**kwargs):raise OSError('production composition boundary reached')
    monkeypatch.setattr('subprocess.Popen',boundary)
    with pytest.raises(OSError,match='production composition boundary reached'):worker_prelaunch(t,config,l,c)
    assert RuntimeInventory(s).get(c['attempt_id'])['state']=='unknown'


def test_report_evidence_is_bounded_redacted_and_tests_not_invented(runtime):
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime
    from personal_agent_dal.worker.runtime_evidence import collect_evidence
    from personal_agent_dal.worker.role_adapter import LaunchPlan
    t,l,c,s,k=runtime;row=prepare_runtime(t,l,c,**k);r=s.validate(row['reservation_id'])
    root=Path(r['temp'])/'reports';root.mkdir()
    (root/'result.test.json').write_text('{"assertion":"api_key=synthetic-secret-value"}')
    (root/'change.patch').write_text('+api_key=synthetic-secret-value')
    (root/'skip').symlink_to(root/'result.test.json')
    plan=LaunchPlan(**dict(row['observation']['plan'],task_directories={'reports':str(root)}))
    result=collect_evidence(plan,r,{'raw':b'api_key=synthetic-secret-value','stop':{'process_exited':True}})
    assert len(result['artifacts'])==3 and len(result['tests'])==1
    assert result['tests'][0].startswith('Unverified task-produced')
    assert 'synthetic-secret-value' not in json.dumps(result)


def test_ordinary_descendant_cannot_be_reported_succeeded(runtime,monkeypatch):
    import signal
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime,execute_runtime
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    t,l,c,s,k=runtime;k['fixture']={'mode':'descendant'}
    row=prepare_runtime(t,l,c,**k);r=s.validate(row['reservation_id'])
    try:
        response=execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
        result=RuntimeInventory(s).get(c['attempt_id'])['result']['result']
        assert not result['stop']['process_exited'] and result['outcome']=='unknown'
        assert not response['accepted']
        monkeypatch.setattr('subprocess.Popen',lambda *a,**k:pytest.fail('unknown replay launched'))
        assert not execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])['accepted']
    finally:
        path=Path(r['temp'])/'ordinary-child.pid'
        if path.exists():
            try:os.kill(int(path.read_text()),signal.SIGKILL)
            except ProcessLookupError:pass


def test_v21_explicit_reference_without_enable_boolean(tmp_path):
    from personal_agent_dal.worker.config import load_worker_config
    body=dict(schema_version='dal.worker-config/2.1',worker_id='w',
        transport={'mode':'local','database_path':str(tmp_path/'db')},
        worktree_root=str(tmp_path/'work'),checkpoint_root=str(tmp_path/'cp'),kill_switch_path=str(tmp_path/'kill'),
        lease_ttl_seconds=720,max_attempts=1,repos={'synthetic':{'local_path':str(tmp_path/'repo')}},
        supervisor_config_path=str(tmp_path/'sup.json'),runtime_policy_revision='trusted-single-user/1.0',
        adapter_config_ref=str(tmp_path/'adapters.json'),execution_protocol='dal.worker-execution-transport/1.0',
        admission_ref=str(tmp_path/'admission.json'))
    path=tmp_path/'worker.json';write(path,body)
    assert load_worker_config(path).admission_ref==tmp_path/'admission.json'
    body['production_enabled']=True;write(path,body)
    with pytest.raises(ValueError):load_worker_config(path)
    del body['production_enabled'];del body['admission_ref'];write(path,body)
    with pytest.raises(ValueError):load_worker_config(path)


@pytest.mark.parametrize('role', ['planner', 'coder', 'reviewer'])
def test_outside_write_observation_required(admission, role):
    c, r, b, e = admission
    smoke = e['roles'][role]['smoke']
    smoke['result']['observations'].remove('outside-write-denied')
    smoke['result_sha256'] = _digest(smoke['result'])
    write(Path(b['path']), e)
    with pytest.raises(SupervisorRefusal, match='RUNTIME_ADMISSION_INVALID'):
        validate_admission(b, context=c, reservation=r)


@pytest.mark.parametrize('offset,valid', [(-1, True), (0, False), (1, False)])
def test_expiry_boundary(admission, monkeypatch, offset, valid):
    c, r, b, e = admission
    monkeypatch.setattr('personal_agent_dal.worker.runtime_admission.time.time',
                        lambda: e['expires_at'] + offset)
    if valid:
        assert validate_admission(b, context=c, reservation=r) == _digest(e)
    else:
        with pytest.raises(SupervisorRefusal, match='RUNTIME_ADMISSION_INVALID'):
            validate_admission(b, context=c, reservation=r)


@pytest.mark.parametrize('digest', [None, '', 'a'*63, 'A'*64, 'z'*64, 123, 'missing'])
def test_revalidation_requires_bound_digest_before_dispatch_and_popen(admission, runtime, monkeypatch, digest):
    from types import SimpleNamespace
    from personal_agent_dal.worker.runtime_admission import revalidate_plan
    from personal_agent_dal.worker.runtime_process import run_process
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime, execute_runtime
    c, r, b, e = admission
    t, l, _, s, k = runtime
    k.update(fixture=None, pins=b['pins'], adapter_config=b['adapters'], admission=b)
    prepare_runtime(t, l, c, **k)
    inv = RuntimeInventory(s)
    plan = replace(build_plan(c, r, b['pins'], b['adapters']), admission=b,
                   admission_sha256=digest, production_enabled=True)
    if digest == 'missing':
        raw = asdict(plan); del raw['admission_sha256']
        plan = SimpleNamespace(**raw)
    def forbidden(*args, **kwargs):
        pytest.fail('invalid admission reached an effect boundary')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    monkeypatch.setattr(t, 'dispatch_prelaunch', forbidden)
    with pytest.raises(SupervisorRefusal, match='RUNTIME_ADMISSION_INVALID'):
        revalidate_plan(plan, inv, c['attempt_id'])
    with pytest.raises(SupervisorRefusal, match='RUNTIME_ADMISSION_INVALID'):
        run_process(inv, c['attempt_id'], plan, heartbeat=forbidden, deadline=9999999999)
    # Stored plan mutation also refuses before dispatch, at the signed-plan gate.
    row = inv.get(c['attempt_id']); obs = row['observation']
    if digest == 'missing': del obs['plan']['admission_sha256']
    else: obs['plan']['admission_sha256'] = digest
    with s._db() as db:
        db.execute('UPDATE runtime_inventory SET observation=? WHERE effective_attempt=?',
                   (json.dumps(obs), c['attempt_id']))
    with pytest.raises(SupervisorRefusal, match='LAUNCH_PLAN_DIGEST_MISMATCH'):
        execute_runtime(t, l, supervisor=s, attempt=c['attempt_id'])
    assert inv.get(c['attempt_id'])['state'] == 'prepared'


def test_replacement_valid_evidence_refused_after_binding(admission, runtime, monkeypatch):
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime, execute_runtime
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    c, r, b, e = admission
    t, l, _, s, k = runtime
    k.update(fixture=None, pins=b['pins'], adapter_config=b['adapters'], admission=b)
    prepare_runtime(t, l, c, **k)
    e['expires_at'] += 1
    write(Path(b['path']), e)
    assert validate_admission(b, context=c, reservation=r) == _digest(e)
    def forbidden(*args, **kwargs): pytest.fail('replaced evidence reached dispatch/Popen')
    monkeypatch.setattr(t, 'acknowledge_prelaunch', forbidden)
    monkeypatch.setattr(t, 'dispatch_prelaunch', forbidden)
    monkeypatch.setattr('subprocess.Popen', forbidden)
    with pytest.raises(SupervisorRefusal, match='RUNTIME_ADMISSION_INVALID'):
        execute_runtime(t, l, supervisor=s, attempt=c['attempt_id'])
    assert RuntimeInventory(s).get(c['attempt_id'])['state'] == 'prepared'


@pytest.mark.parametrize('mutation', ['future_issue', 'reversed_probe', 'probe_after_issue', 'long_probe'])
def test_attestation_time_ordering_stays_closed(admission, mutation):
    c, r, b, e = admission
    smoke = e['roles']['coder']['smoke']
    if mutation == 'future_issue': e['issued_at'] = e['expires_at'] - 1
    elif mutation == 'reversed_probe': smoke['started_at'] = smoke['ended_at'] + 1
    elif mutation == 'probe_after_issue': smoke['ended_at'] = e['issued_at'] + 1
    else: smoke['started_at'] = smoke['ended_at'] - 121
    write(Path(b['path']), e)
    with pytest.raises(SupervisorRefusal, match='RUNTIME_ADMISSION_INVALID'):
        validate_admission(b, context=c, reservation=r)


def test_owner_window_has_no_invented_probe_age_or_ttl_cap(admission):
    c, r, b, e = admission
    for record in e['roles'].values():
        record['smoke']['started_at'] -= 86400 * 30
        record['smoke']['ended_at'] -= 86400 * 30
    e['issued_at'] -= 86400 * 30
    e['expires_at'] += 86400 * 30
    write(Path(b['path']), e)
    assert validate_admission(b, context=c, reservation=r) == _digest(e)


def test_heartbeat_revalidates_bound_admission(admission, runtime, monkeypatch):
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime, execute_runtime
    c, r, b, e = admission
    t, l, _, s, k = runtime
    k.update(fixture=None, pins=b['pins'], adapter_config=b['adapters'], admission=b)
    prepare_runtime(t, l, c, **k)
    observed = []
    def process_boundary(inventory, attempt, plan, *, heartbeat, **kwargs):
        assert heartbeat() is True  # real signed ack, dispatch and heartbeat composition
        e['expires_at'] += 1
        write(Path(b['path']), e)
        with pytest.raises(SupervisorRefusal, match='RUNTIME_ADMISSION_INVALID'):
            heartbeat()
        observed.append('bound-evidence-rechecked')
        raise OSError('offline heartbeat boundary reached')
    monkeypatch.setattr('personal_agent_dal.worker.trusted_runtime.run_process', process_boundary)
    with pytest.raises(OSError, match='offline heartbeat boundary reached'):
        execute_runtime(t, l, supervisor=s, attempt=c['attempt_id'])
    assert observed == ['bound-evidence-rechecked']


def test_v3_coder_admission_requires_git_metadata_denial_not_legacy_git_writes(admission):
    from tests.dal.test_timeline_roles import config
    c,r,b,e=admission
    body=config();snapshot=dict(body,digest=_digest(body),source='system')
    c=dict(c,snapshot=snapshot,snapshot_sha256=_digest(snapshot),completion_mode='workflow_result',owner={'kind':'workflow','workflow_id':'synthetic'})
    template=b['pins'][0]
    b['pins']=[dict(schema='dal.runtime-pin/3.0',role=name,configuration=configuration,runtime='codex_cli',provider='openai',
        executable=template['executable'],version=template['version'],executable_sha256=template['executable_sha256'])
        for name,configuration in body['roles'].items()]
    config_path=Path(next(iter(b['config_refs'])))
    write(config_path,dict(pins=b['pins'],adapters=b['adapters']))
    b['config_refs']={str(config_path):_digest(json.loads(config_path.read_text()))}
    e.update(schema='dal.runtime-admission/3.0',scope='timeline-workflow-v3',config_refs=b['config_refs'])
    for name in body['roles']:
        plan=build_plan(dict(c,execution_role=name),r,b['pins'],b['adapters'])
        record=e['roles'][name];record['configuration']=body['roles'][name];record['plan']=plan_contract(plan,r)
        record['smoke']['command']=list(plan.argv);record['smoke']['command_sha256']=_digest(list(plan.argv))
        if name=='coder':
            record['smoke']['result']['observations']=['report-produced','scratch-write','source-edit','git-metadata-write-denied','outside-write-denied']
            record['smoke']['result_sha256']=_digest(record['smoke']['result'])
    write(Path(b['path']),e)
    assert validate_admission(b,context=c,reservation=r)==_digest(e)
    coder=e['roles']['coder']['smoke']
    coder['result']['observations']=['report-produced','scratch-write','source-edit','git-add','git-commit','outside-write-denied']
    coder['result_sha256']=_digest(coder['result']);write(Path(b['path']),e)
    with pytest.raises(SupervisorRefusal):validate_admission(b,context=c,reservation=r)
