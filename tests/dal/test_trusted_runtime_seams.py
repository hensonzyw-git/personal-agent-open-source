"""Restart ownership, public plans, event budgets and full original-NEW recovery."""
import json
import hashlib
import subprocess
import sys
from pathlib import Path
import pytest
from tests.dal.test_trusted_runtime import runtime, world, stream
from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
from personal_agent_dal.worker.supervisor import Supervisor, SupervisorRefusal
from personal_agent_dal.worker.trusted_runtime import prepare_runtime, execute_runtime, reconcile_runtime
from personal_agent_dal.worker.role_adapter import build_plan, load_adapter_config, parse_events


def test_full_original_new_replacement(tmp_path):
    from scripts.dal_runtime_synthetic_preflight import run_synthetic
    evidence=run_synthetic(tmp_path/'fixed runtime with spaces')
    assert evidence['production_enabled'] is False
    assert evidence['stop']['requested'] and evidence['stop']['process_exited']
    assert evidence['manifest_version']=='dal.launch-manifest/1.1'
    assert len(evidence['checks'])==12


@pytest.mark.parametrize('state',['dispatch_requested','granted','starting'])
def test_reopened_states_never_spawn(runtime,monkeypatch,state):
    t,l,c,s,k=runtime;prepare_runtime(t,l,c,**k);inv=RuntimeInventory(s)
    inv.transition(c['attempt_id'],'prepared','dispatch_requested')
    if state in ('granted','starting'): inv.transition(c['attempt_id'],'dispatch_requested','granted')
    if state=='starting':inv.transition(c['attempt_id'],'granted','starting')
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('second spawn'))
    reopened=Supervisor(s.root,boot_id=s.boot_id,epoch=s.epoch)
    result=reconcile_runtime(reopened,t)
    assert result['observations'][0]['state']=='unknown'
    assert inv.get(c['attempt_id'])['observation']['reconciliation_stop']['process_exited'] is (state!='starting')
    with pytest.raises(SupervisorRefusal,match='EXECUTION_EFFECTS_UNKNOWN'):
        execute_runtime(t,l,supervisor=reopened,attempt=c['attempt_id'])


def test_reconcile_reuploads_identical_persisted_envelope(runtime,monkeypatch):
    t,l,c,s,k=runtime;prepare_runtime(t,l,c,**k)
    submit=t.submit_execution_result
    def lost(*args):submit(*args);raise OSError('lost response')
    monkeypatch.setattr(t,'submit_execution_result',lost)
    with pytest.raises(OSError):execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    before=RuntimeInventory(s).get(c['attempt_id'])['result']
    def replay(lease,body):
        assert body==before
        return submit(lease,body)
    monkeypatch.setattr(t,'submit_execution_result',replay)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('result replay spawned'))
    assert reconcile_runtime(s,t)['observations'][0]['state']=='reported'
    assert RuntimeInventory(s).get(c['attempt_id'])['result']==before


def test_bounded_inventory_pagination(runtime):
    t,l,c,s,k=runtime;prepare_runtime(t,l,c,**k)
    result=reconcile_runtime(s,limit=1)
    assert result['next_cursor']==c['attempt_id']
    assert reconcile_runtime(s,after=result['next_cursor'],limit=1)['observations']==[]
    with pytest.raises(SupervisorRefusal):reconcile_runtime(s,limit=65)


def test_closed_public_auth_plan_no_login_read(runtime,tmp_path,monkeypatch):
    t,l,c,s,k=runtime
    reservation=s.reserve(attempt_id='plans',workspace_id='plans',generation=1,authority={},read_roots=[])
    binary=Path(sys.executable).resolve()
    pins=[dict(role=r,configuration=conf,executable=str(binary),executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),version='fixture-python') for r,conf in c['snapshot']['roles'].items()]
    body={'schema':'dal.role-adapters/1.0','roles':{r:{'mode':'codex_login','home':str(tmp_path/'DO-NOT-READ-login'),'environment':{}} for r in c['snapshot']['roles']}}
    config_path=tmp_path/'public-adapters.json';config_path.write_text(json.dumps(body))
    config=load_adapter_config(config_path)
    original=Path.read_bytes
    def deny_login(path):
        if 'DO-NOT-READ' in str(path):pytest.fail('login read')
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',deny_login)
    for name in ('planner','coder','reviewer'):
        plan=build_plan(dict(c,execution_role=name),reservation,pins,config)
        assert plan.auth_route=='codex_login' and plan.max_steps==64
        assert set(plan.task_directories)=={'scratch','reports','test-copy','cache'}
        assert plan.environment['CODEX_HOME']==str(tmp_path/'DO-NOT-READ-login')
        assert reservation['temp'] in plan.write_roots
        assert (reservation['workspace'] in plan.write_roots)==(name=='coder')
        assert reservation['git'] not in plan.write_roots
        assert 'sandbox_workspace_write.exclude_slash_tmp=true' in plan.argv
    config['roles']['coder']['environment']={'OPENAI_BASE_URL':'https://synthetic.invalid'}
    with pytest.raises(SupervisorRefusal,match='AUTH_ENV_UNSUPPORTED'):build_plan(c,reservation,pins,config)
    config['roles']['coder'].update(environment={},mode='api-key')
    with pytest.raises(SupervisorRefusal,match='AUTH_ROUTE_MISMATCH'):build_plan(c,reservation,pins,config)
    body['extra']=True;config_path.write_text(json.dumps(body))
    with pytest.raises(SupervisorRefusal,match='ADAPTER_CONFIG_INVALID'):load_adapter_config(config_path)


def test_real_prepare_guard_precedes_signing_and_reservation(runtime,monkeypatch):
    t,l,c,s,k=runtime;k['fixture']=None
    monkeypatch.setattr(s,'reserve',lambda **kw:pytest.fail('real preparation'))
    monkeypatch.setattr('personal_agent_dal.worker.trusted_runtime.sign_execution_manifest',lambda *a,**kw:pytest.fail('real signing'))
    with pytest.raises(SupervisorRefusal,match='MINI_ACCEPTANCE_REQUIRED'):prepare_runtime(t,l,c,**k)


@pytest.mark.parametrize('raw,reason',[
    (stream(*[{'type':'thread.started'}]*65),'CLI_EVENT_STEP_LIMIT'),
    (stream({'type':'item.completed','item':{'id':'a','type':'agent_message','text':'中'*131072}}, {'type':'turn.completed'}),'CLI_REPORT_LIMIT'),
    (stream({'type':'assistant','message':{'content':None}}),'CLI_EVENTS_INVALID'),
    (stream({'type':'assistant','message':{'content':[{'type':'tool_use','id':'t','name':'Read','input':'bad'}]}}),'CLI_EVENTS_INVALID'),
])
def test_event_and_encoded_report_bounds(raw,reason):
    with pytest.raises(SupervisorRefusal,match=reason):parse_events(raw,'claude_code' if b'assistant' in raw else 'codex_cli')


def test_linux_import_never_loads_darwin_library():
    code="import sys,ctypes; import personal_agent_dal.worker.role_adapter; sys.platform='linux'; ctypes.CDLL=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Darwin library loaded')); import personal_agent_dal.worker.runtime_process"
    result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('mode,reason',[
    ('events','CLI_EVENT_STEP_LIMIT'),('unicode_report','CLI_REPORT_LIMIT'),
    ('escaped_report','CLI_RESULT_ENVELOPE_LIMIT'),('malformed','CLI_EVENTS_INVALID'),
    ('truncated','CLI_STREAM_TRUNCATED'),('provider_error','CLI_PROVIDER_ERROR')])
def test_fixed_child_failure_evidence(runtime,mode,reason):
    t,l,c,s,k=runtime;k['fixture']={'mode':mode}
    prepare_runtime(t,l,c,**k)
    execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    result=RuntimeInventory(s).get(c['attempt_id'])['result']['result']
    assert result['outcome']=='failed' and result['reason']==reason
    assert result['stop']['process_exited']
    assert len(json.dumps(result).encode())<256*1024


def test_stderr_redacted_before_durable_result(runtime):
    t,l,c,s,k=runtime;k['fixture']={'mode':'stderr_secret'}
    prepare_runtime(t,l,c,**k)
    assert execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])['accepted']
    result=RuntimeInventory(s).get(c['attempt_id'])['result']['result']
    assert result['redacted'] and '[REDACTED]' in json.dumps(result)
    assert 'synthetic-private-value' not in json.dumps(result)


def test_lazy_darwin_identity_is_stable_for_actual_process():
    if sys.platform!='darwin':pytest.skip('Darwin-only ABI check')
    import os
    from personal_agent_dal.worker.runtime_process import process_identity
    identity=process_identity(os.getpid())
    assert identity and identity==process_identity(os.getpid())
    seconds,microseconds=map(int,identity.split(':'))
    assert seconds>0 and 0<=microseconds<1000000


def test_stale_start_identity_never_stops_another_live_fixture(runtime):
    from personal_agent_dal.worker.runtime_process import fixture_plan, stop_registered
    t,l,c,s,k=runtime;row=prepare_runtime(t,l,c,**k)
    plan=fixture_plan(s.validate(row['reservation_id']),'timeout')
    child=subprocess.Popen(plan.argv,cwd=plan.cwd,env=plan.environment,start_new_session=True,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        stop=stop_registered({'boot_id':s.boot_id,'pid':child.pid,'pgid':child.pid,'process_start':'stale'},boot_id=s.boot_id)
        assert not stop['requested'] and not stop['process_exited'] and child.poll() is None
    finally:child.kill();child.wait(timeout=5)


def test_reservation_parent_remains_strict(runtime):
    t,l,c,s,k=runtime;row=prepare_runtime(t,l,c,**k)
    r=s.validate(row['reservation_id'])
    (Path(r['parent'])/'unregistered').symlink_to(r['workspace'])
    with pytest.raises(SupervisorRefusal,match='RESERVATION_PARENT_CONTENT_INVALID'):s.validate(row['reservation_id'])
