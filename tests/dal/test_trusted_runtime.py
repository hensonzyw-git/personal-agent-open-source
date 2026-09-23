"""Real local control plane + fixed bounded children; no CLI model invocation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from personal_agent.auth.device_keys import encode_device_public_key
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.isolation_evidence import register_supervisor
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.transport_models import WorkerEnrollment
from personal_agent_dal.worker.supervisor import Supervisor, SupervisorRefusal, _digest, _tree
from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
from personal_agent_dal.worker.role_adapter import parse_events, build_plan
from personal_agent_dal.worker.runtime_process import fixture_plan, run_process
from personal_agent_dal.worker.trusted_runtime import prepare_runtime, execute_runtime
from personal_agent_dal.worker.transport import LocalSQLiteAdapter
from tests.dal.test_trusted_execution_unit1 import world, prepared, start

@pytest.fixture
def runtime(world,tmp_path):
    p=prepared(world);start(world,p,expires_at=int(utc_now().timestamp())+720)
    key=ec.generate_private_key(ec.SECP256R1())
    with session_factory(world)() as s,s.begin():
        s.add(WorkerEnrollment(worker_id='w',machine_id='synthetic',capabilities='[]',created_at=utc_now()))
    register_supervisor(world,kid='k',worker_id='w',machine_id='synthetic',public_key=encode_device_public_key(key.public_key()),boot_id='boot',supervisor_epoch=1)
    transport=LocalSQLiteAdapter(world,worker_id='w',lease_ttl_seconds=720,max_attempts=1,checkpoint_root=tmp_path/'checkpoints')
    lease=transport.claim();context=transport.prelaunch_context(lease)
    supervisor=Supervisor(tmp_path.resolve()/'supervisor',boot_id='boot',epoch=1)
    identity=dict(kid='k',worker_id='w',machine_id='synthetic',registration_epoch=1,boot_id='boot',supervisor_epoch=1)
    kwargs=dict(supervisor=supervisor,identity=identity,key=key,pins=[],read_roots=[],fixture={})
    return transport,lease,context,supervisor,kwargs


def test_same_orchestration_real_control_plane_and_replay(runtime,monkeypatch):
    t,l,c,s,k=runtime
    prepared=prepare_runtime(t,l,c,**k)
    result=execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    assert result['accepted'] and result['job_state']=='succeeded'
    inv=RuntimeInventory(s);row=inv.get(c['attempt_id'])
    assert row['state']=='reported' and row['result']['result']['tool_events']==['command_execution']
    assert row['observation']['production_enabled'] is False
    assert s.validate(prepared['reservation_id'])['workspace']==s.validate(row['reservation_id'])['workspace']
    monkeypatch.setattr('subprocess.Popen',lambda *a,**k:pytest.fail('replayed launch'))
    assert execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])==result


def test_result_persisted_before_lost_upload(runtime,monkeypatch):
    t,l,c,s,k=runtime;prepare_runtime(t,l,c,**k)
    original=t.submit_execution_result
    def lost(*args):original(*args);raise OSError('lost')
    monkeypatch.setattr(t,'submit_execution_result',lost)
    with pytest.raises(OSError):execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    assert RuntimeInventory(s).get(c['attempt_id'])['state']=='result_ready'
    monkeypatch.setattr(t,'submit_execution_result',original)
    monkeypatch.setattr('subprocess.Popen',lambda *a,**k:pytest.fail('replayed launch'))
    assert execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])['replay']


def test_dispatch_response_loss_never_spawns(runtime,monkeypatch):
    t,l,c,s,k=runtime;prepare_runtime(t,l,c,**k)
    original=t.dispatch_prelaunch
    def lost(*a):original(*a);raise OSError('lost')
    monkeypatch.setattr(t,'dispatch_prelaunch',lost)
    monkeypatch.setattr('subprocess.Popen',lambda *a,**k:pytest.fail('spawn after lost response'))
    with pytest.raises(SupervisorRefusal,match='DISPATCH_RESPONSE_UNKNOWN'):execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    with pytest.raises(SupervisorRefusal,match='EXECUTION_EFFECTS_UNKNOWN'):execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])


def test_crash_after_starting_never_replays(runtime,monkeypatch):
    t,l,c,s,k=runtime;prepare_runtime(t,l,c,**k)
    monkeypatch.setattr('subprocess.Popen',lambda *a,**k:(_ for _ in ()).throw(OSError('crash')))
    with pytest.raises(OSError):execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    assert RuntimeInventory(s).get(c['attempt_id'])['state']=='unknown'
    with pytest.raises(SupervisorRefusal,match='EXECUTION_EFFECTS_UNKNOWN'):execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])

@pytest.mark.parametrize('mode,wall,limit,reason',[('timeout',.2,4194304,'CLI_TIMEOUT'),('output',2,1024,'CLI_OUTPUT_LIMIT')])
def test_real_bounded_process(runtime,mode,wall,limit,reason):
    t,l,c,s,k=runtime;k['fixture']=dict(mode=mode,wall_seconds=wall,output_bytes=limit)
    prepare_runtime(t,l,c,**k)
    result=execute_runtime(t,l,supervisor=s,attempt=c['attempt_id'])
    row=RuntimeInventory(s).get(c['attempt_id'])
    assert row['result']['result']['reason']==reason
    assert row['result']['result']['stop']['process_exited']
    assert row['result']['result']['outcome']=='failed'


def test_adopt_original_new_and_concurrent_cas(runtime):
    t,l,c,s,k=runtime
    original=s.reserve(attempt_id='old-new-preparation',workspace_id='new-space',generation=2,authority={'original':'evidence'},read_roots=[])
    c=dict(c,isolation={'new_reservation_id':original['reservation_id'],'new_inventory_sha256':_digest(original),
        'new_workspace_id':'new-space','workspace_generation':2},isolation_reserved_by=c['attempt_id'])
    inv=RuntimeInventory(s)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(lambda _:inv.adopt(c,original),range(2)))
    assert rows[0]['reservation_id']==rows[1]['reservation_id']==original['reservation_id']
    assert s.validate(original['reservation_id'])==original
    def claim(_):
        try:inv.transition(c['attempt_id'],'prepared','dispatch_requested');return True
        except SupervisorRefusal:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sum(pool.map(claim,range(2)))==1
    changed=dict(c,attempt_id='another',isolation_reserved_by='another')
    with pytest.raises(SupervisorRefusal,match='ALREADY_ADOPTED'):inv.adopt(changed,original)


def test_adoption_requires_server_reservation_binding(runtime):
    t,l,c,s,k=runtime
    original=s.reserve(attempt_id='old',workspace_id='new',generation=1,authority={},read_roots=[])
    c=dict(c,isolation={'new_reservation_id':original['reservation_id'],'new_inventory_sha256':_digest(original),
        'new_workspace_id':'new','workspace_generation':1},isolation_reserved_by='wrong')
    with pytest.raises(SupervisorRefusal,match='SOURCE_RESERVATION'):RuntimeInventory(s).adopt(c,original)


def test_fixture_cannot_be_arbitrary_command(runtime):
    t,l,c,s,k=runtime;row=prepare_runtime(t,l,c,**k);inv=RuntimeInventory(s)
    inv.transition(c['attempt_id'],'prepared','dispatch_requested');inv.transition(c['attempt_id'],'dispatch_requested','granted')
    reservation=s.validate(row['reservation_id'])
    plan=replace(fixture_plan(reservation),argv=('/bin/sh','-c','echo forbidden','success'))
    with pytest.raises(SupervisorRefusal,match='FIXTURE_PLAN_MISMATCH'):
        run_process(inv,c['attempt_id'],plan,heartbeat=lambda:True,deadline=9999999999)


def stream(*events):return ('\n'.join(json.dumps(x) for x in events)+'\n').encode()

@pytest.mark.parametrize('raw',[b'',b'{}',b'prose\n',b'{bad}\n',stream({'type':'turn.completed'}),
    stream({'type':'item.started','item':{'id':'x','type':'command_execution'}},{'type':'turn.completed'}),
    stream({'type':'turn.completed'},{'type':'turn.completed'})])
def test_malformed_mixed_truncated_events(raw):
    with pytest.raises(SupervisorRefusal):parse_events(raw,'codex_cli')


def test_claude_tools_mixed_with_report_are_allowed():
    raw=stream({'type':'assistant','message':{'content':[{'type':'text','text':'Inspecting'},
        {'type':'tool_use','id':'x','name':'Read','input':{'path':'a'}}]}},
        {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'x','content':'source'}]}},
        {'type':'result','result':'Review complete','is_error':False,'usage':{'input_tokens':1,'output_tokens':2}})
    assert parse_events(raw,'claude_code')['tool_events']==['Read']


def test_business_symlinks_scoped_control_strict(tmp_path):
    (tmp_path/'file').write_text('source');(tmp_path/'link').symlink_to('file')
    _tree(tmp_path,approved_targets=(tmp_path,))
    with pytest.raises(SupervisorRefusal):_tree(tmp_path)
    (tmp_path/'outside').symlink_to('/etc')
    with pytest.raises(SupervisorRefusal):_tree(tmp_path,approved_targets=(tmp_path,))


def test_role_plan_scratch_and_exact_product_models(runtime):
    t,l,c,s,k=runtime
    r=s.reserve(attempt_id='planning',workspace_id='planning',generation=1,authority={},read_roots=[])
    executable='/bin/echo';sha=hashlib.sha256(Path(executable).read_bytes()).hexdigest()
    pins=[dict(role=role,configuration=config,executable=executable,executable_sha256=sha,version='fixture-pin') for role,config in c['snapshot']['roles'].items()]
    for role,model in [('planner','gpt-6-astra'),('coder','gpt-5.6-sol'),('reviewer','gpt-6-astra')]:
        plan=build_plan(dict(c,execution_role=role),r,pins)
        assert model in plan.argv and plan.cwd==r['workspace' if role=='coder' else 'temp']
        assert '--sandbox' in plan.argv and 'workspace-write' in plan.argv
        assert not any('TOKEN' in name or 'SECRET' in name for name in plan.environment)


def test_poll_returns_service_outcome_without_legacy(runtime,monkeypatch,tmp_path):
    from types import SimpleNamespace
    from personal_agent_dal.worker.poll_once import run_poll_once
    t,l,c,s,k=runtime
    monkeypatch.setattr(t,'claim',lambda:l)
    def prelaunch(transport,config,lease,context):
        prepare_runtime(transport,lease,context,**k)
        return execute_runtime(transport,lease,supervisor=s,attempt=context['attempt_id'])
    monkeypatch.setattr('personal_agent_dal.worker.prelaunch.worker_prelaunch',prelaunch)
    monkeypatch.setattr('personal_agent_dal.worker.poll_once._execute_job',lambda *a,**kw:pytest.fail('legacy'))
    monkeypatch.setattr(t,'submit_result',lambda *a,**kw:pytest.fail('legacy submit'))
    outcome=run_poll_once(t,SimpleNamespace(kill_switch_path=tmp_path/'kill'))
    assert outcome.state=='succeeded' and outcome.error is None


def test_v2_closed_config_preserves_old_shape(tmp_path):
    from personal_agent_dal.worker.config import load_worker_config
    body=dict(schema_version='dal.worker-config/1.1',worker_id='w',transport={'mode':'local','database_path':str(tmp_path/'synthetic.db')},
        worktree_root=str(tmp_path/'work'),checkpoint_root=str(tmp_path/'checkpoints'),kill_switch_path=str(tmp_path/'kill'),
        lease_ttl_seconds=720,max_attempts=1,repos={'repo':{'local_path':str(tmp_path/'repo')}})
    path=tmp_path/'worker.json';path.write_text(json.dumps(body))
    assert load_worker_config(path).runtime_policy_revision is None
    body.update(schema_version='dal.worker-config/2.0',runtime_policy_revision='trusted-single-user/1.0',
        execution_protocol='dal.worker-execution-transport/1.0',adapter_config_ref=str(tmp_path/'adapters.json'),
        supervisor_config_path=str(tmp_path/'supervisor.json'),production_enabled=False)
    path.write_text(json.dumps(body));assert load_worker_config(path).schema_version=='dal.worker-config/2.0'
    body['production_enabled']=True;path.write_text(json.dumps(body))
    with pytest.raises(ValueError):load_worker_config(path)


def test_real_plan_cannot_inherit_synthetic_acceptance(runtime,monkeypatch):
    t,l,c,s,k=runtime;row=prepare_runtime(t,l,c,**k);inv=RuntimeInventory(s)
    inv.transition(c['attempt_id'],'prepared','dispatch_requested');inv.transition(c['attempt_id'],'dispatch_requested','granted')
    plan=replace(fixture_plan(s.validate(row['reservation_id'])),runtime='codex_cli')
    monkeypatch.setattr('subprocess.Popen',lambda *a,**kw:pytest.fail('real launch'))
    with pytest.raises(SupervisorRefusal,match='MINI_ACCEPTANCE_REQUIRED'):
        run_process(inv,c['attempt_id'],plan,heartbeat=lambda:True,deadline=9999999999)


def test_local_lease_deadline_stops_even_when_heartbeat_keeps_approving(runtime):
    import time
    t,l,c,s,k=runtime
    row=prepare_runtime(t,l,c,**k);inv=RuntimeInventory(s)
    inv.transition(c['attempt_id'],'prepared','dispatch_requested')
    inv.transition(c['attempt_id'],'dispatch_requested','granted')
    reservation=s.validate(row['reservation_id'])
    plan=fixture_plan(reservation,'timeout',wall_seconds=2)
    started=time.monotonic()
    result=run_process(inv,c['attempt_id'],plan,heartbeat=lambda:True,deadline=time.time()+10.1)
    assert result['reason']=='CLI_TIMEOUT' and result['stop']['process_exited']
    assert time.monotonic()-started<2
