"""Synthetic helper and public metadata only; no real credentials/provider calls."""
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from personal_agent_dal.machine.workflow_selection import digest
from personal_agent_dal.worker.role_adapter import build_plan, load_adapter_config
from personal_agent_dal.worker.supervisor import SupervisorRefusal
from tests.dal.test_versioned_role_contract import new_snapshot


@pytest.fixture
def route_world(tmp_path):
    root = tmp_path.resolve()
    helper = root / 'auth helper; sentinel'
    helper.write_text('#!/bin/sh\nprintf SENTINEL_AUTH_OUTPUT\ntouch MUST_NOT_RUN\n')
    helper.chmod(0o700)
    from personal_agent_dal.worker.reviewer_route import PUBLIC_ROUTE
    public = root / 'public-route.json'
    public.write_text(json.dumps(PUBLIC_ROUTE)); public.chmod(0o600)
    route = dict(mode='claude_existing_third_party', route_id=PUBLIC_ROUTE['route_id'],
        revision=1, endpoint='http://127.0.0.1:3456',
        helper=dict(path=str(helper), sha256=hashlib.sha256(helper.read_bytes()).hexdigest()),
        config_ref=dict(path=str(public), sha256=digest(PUBLIC_ROUTE)))
    config = dict(schema='dal.role-adapters/2.0', roles={
        name:dict(mode='codex_login', home=str(root/'unread-login'), environment={})
        for name in ('planner','coder')})
    config['roles']['reviewer'] = route
    body = new_snapshot()
    binary = Path('/bin/echo')  # Inert, protected fake CLI; never executed.
    pins = [dict(role=name, configuration=value, executable=str(binary),
        version='2.1.231' if name=='reviewer' else 'synthetic',
        executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
        for name,value in body['roles'].items()]
    context=dict(snapshot=body, snapshot_sha256=digest(body), execution_role='reviewer')
    reservation={key:str(root/'task'/key) for key in ('workspace','temp','git')}
    return context,reservation,pins,config


def test_high_plan_is_reference_only_and_never_executes_helper(route_world, monkeypatch):
    def forbidden(*a, **k): pytest.fail('static preparation executed a process')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    plan=build_plan(*route_world)
    settings_arg=plan.argv[plan.argv.index('--settings')+1]
    assert plan.argv == (
        route_world[2][-1]['executable'], '--print', '--safe-mode',
        '--no-session-persistence', '--no-chrome', '--output-format',
        'stream-json', '--verbose', '--model', 'changhe/ch-g/kimi-k3',
        '--effort', 'high', '--permission-mode', 'dontAsk',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--max-turns', '64', '--allowedTools',
        'Read,Glob,Grep,Bash,Edit,Write,WebFetch,WebSearch',
        '--disallowedTools', 'Agent,Task', '--settings', settings_arg,
        '--bare', '--setting-sources', '')
    assert plan.argv[plan.argv.index('--model')+1]=='changhe/ch-g/kimi-k3'
    assert '--bare' in plan.argv
    assert plan.argv[plan.argv.index('--setting-sources')+1]==''
    import shlex
    settings=json.loads(plan.argv[plan.argv.index('--settings')+1])
    assert shlex.split(settings['apiKeyHelper'])==[route_world[3]['roles']['reviewer']['helper']['path']]
    assert 'SENTINEL_AUTH_OUTPUT' not in json.dumps(asdict(plan))
    assert plan.environment['ANTHROPIC_BASE_URL']=='http://127.0.0.1:3456'
    assert plan.environment['CLAUDE_CONFIG_DIR'].startswith(route_world[1]['temp'])
    assert not plan.production_enabled


@pytest.mark.parametrize('change', ['endpoint','query','userinfo','fragment','path','port','env',
    'route','revision','schema','sha','mode','symlink','parent_symlink','writable',
    'oversize','fifo','billing','effort','version','public','extra_helper'])
def test_fail_closed(route_world, change, tmp_path):
    c,r,p,a=route_world; route=a['roles']['reviewer']; helper=Path(route['helper']['path'])
    if change in ('endpoint','query','userinfo','fragment','path','port'):
        route['endpoint']={'endpoint':'https://example.invalid','query':'http://127.0.0.1:3456?x=1',
            'userinfo':'http://x@127.0.0.1:3456','fragment':'http://127.0.0.1:3456#x',
            'path':'http://127.0.0.1:3456/other','port':'http://127.0.0.1:3457'}[change]
    elif change=='env': route['environment']={'ANTHROPIC_API_KEY':'sentinel'}
    elif change=='route': route['route_id']='unknown'
    elif change=='revision': route['revision']=2
    elif change=='schema': a['schema']='dal.role-adapters/999.0'
    elif change=='sha': route['helper']['sha256']='0'*64
    elif change=='mode': helper.chmod(0o755)
    elif change=='symlink':
        link=tmp_path/'link';link.symlink_to(helper);route['helper']['path']=str(link)
    elif change=='parent_symlink':
        link=tmp_path/'linked-dir';link.symlink_to(helper.parent);route['helper']['path']=str(link/helper.name)
    elif change=='writable': r['workspace']=str(helper.parent)
    elif change=='oversize': helper.write_bytes(b'x'*1048577)
    elif change=='fifo':
        import os
        helper.unlink();os.mkfifo(helper,0o700)
    elif change in ('billing','effort'):
        c['snapshot']['roles']['reviewer']['billing' if change=='billing' else 'reasoning']='subscription' if change=='billing' else 'medium'
        p[-1]['configuration']=copy.deepcopy(c['snapshot']['roles']['reviewer'])
        c['snapshot_sha256']=digest(c['snapshot'])
    elif change=='version': p[-1]['version']='2.1.230'
    elif change=='public': Path(route['config_ref']['path']).write_text('{}')
    elif change=='extra_helper': route['helper']['arguments']=['--secret']
    with pytest.raises((SupervisorRefusal, ValueError)): build_plan(c,r,p,a)


def test_versioned_loader_and_legacy_refusal(route_world,tmp_path):
    config=route_world[3]; path=tmp_path/'adapters.json'
    path.write_text(json.dumps(config)); assert load_adapter_config(path)==config
    config['schema']='dal.role-adapters/1.0'; path.write_text(json.dumps(config))
    with pytest.raises(SupervisorRefusal): load_adapter_config(path)


def test_git_helper_does_not_inherit_plan_auth(route_world, monkeypatch):
    from personal_agent_dal.worker.runtime_evidence import bounded_git_patch, git_environment
    plan=build_plan(*route_world)
    plan.environment.update(ANTHROPIC_API_KEY='SENTINEL_SECRET',
        API_KEY_HELPER='SENTINEL_HELPER', GIT_CONFIG_KEY_9='credential.helper',
        GIT_CONFIG_VALUE_9='SENTINEL_HELPER', HTTPS_PROXY='SENTINEL_PROXY')
    seen=[]
    def intercept(*args,**kwargs):
        seen.append(kwargs['env'])
        raise RuntimeError('stopped before spawn')
    monkeypatch.setattr(subprocess,'Popen',intercept)
    monkeypatch.setattr('personal_agent_dal.worker.supervisor.verify_executable',lambda p:Path('/synthetic/git'))
    with pytest.raises(RuntimeError,match='stopped before spawn'):
        bounded_git_patch(plan,route_world[1],{})
    assert seen==[git_environment(route_world[1])]
    assert 'SENTINEL' not in json.dumps(seen)
    assert not any(k.startswith(('ANTHROPIC','CLAUDE','CODEX','API_KEY')) for k in seen[0])


# Reuse only offline synthetic admission fixtures. These are schema tests, not
# evidence that any native route has been exercised.
from tests.dal.test_runtime_admission import admission, runtime, world, write
from personal_agent_dal.worker.runtime_admission import (
    validate_admission, plan_contract, ROUTE_SCHEMA,
)


@pytest.fixture
def route_admission(admission,route_world):
    c,r,b,e=admission
    rc,rr,pins,adapters=route_world
    c['snapshot']=rc['snapshot'];c['snapshot_sha256']=rc['snapshot_sha256']
    c['execution_role']='reviewer'
    b['pins']=pins;b['adapters']=adapters
    ref=adapters['roles']['reviewer']['config_ref']
    b['config_refs'][ref['path']]=ref['sha256']
    e['schema']=ROUTE_SCHEMA
    for name in e['roles']:
        plan=build_plan(dict(c,execution_role=name),r,pins,adapters)
        record=e['roles'][name]
        record['configuration']=c['snapshot']['roles'][name]
        record['plan']=plan_contract(plan,r)
        smoke=record['smoke'];smoke['command']=list(plan.argv)
        smoke['command_sha256']=digest(smoke['command'])
        smoke['result']['cli_version']=plan.version
        if name=='reviewer':smoke['result']['observations'] += [
            'helper-auth-native','personal-auth-isolated','gateway-route-attested','fallback-disabled-attested']
        smoke['result_sha256']=digest(smoke['result'])
    write(Path(b['path']),e)
    return c,r,b,e


def test_route_admission_requires_native_owner_evidence(route_admission):
    c,r,b,e=route_admission
    assert validate_admission(b,context=c,reservation=r)==digest(e)
    with pytest.raises(SupervisorRefusal,match='MINI_ACCEPTANCE_REQUIRED'):
        validate_admission(None,context=c,reservation=r)
    e['roles']['reviewer']['smoke']['result']['observations'].pop()
    e['roles']['reviewer']['smoke']['result_sha256']=digest(e['roles']['reviewer']['smoke']['result'])
    write(Path(b['path']),e)
    with pytest.raises(SupervisorRefusal):validate_admission(b,context=c,reservation=r)


@pytest.mark.parametrize('change', ['helper','helper_ref','endpoint','metadata','metadata_ref',
    'proxy','fallback','model','revision','schema','missing_ref'])
def test_admission_rechecks_route_drift(route_admission,change):
    c,r,b,e=route_admission
    sha=validate_admission(b,context=c,reservation=r)
    route=b['adapters']['roles']['reviewer']
    if change=='helper':Path(route['helper']['path']).write_text('#!/bin/sh\necho changed\n')
    elif change=='helper_ref':route['helper']['sha256']='a'*64
    elif change=='endpoint':route['endpoint']='http://localhost:3456'
    elif change in ('metadata','proxy','fallback','model'):
        path=Path(route['config_ref']['path']);data=json.loads(path.read_text())
        data[{'metadata':'transport','proxy':'proxy','fallback':'fallback_mode','model':'upstream_model'}[change]]='changed'
        write(path,data)
    elif change=='metadata_ref':route['config_ref']['sha256']='a'*64
    elif change=='revision':route['revision']=99
    elif change=='schema':e['schema']='dal.runtime-admission/1.0';write(Path(b['path']),e)
    elif change=='missing_ref':b['config_refs'].pop(route['config_ref']['path']);write(Path(b['path']),e)
    with pytest.raises(SupervisorRefusal):
        validate_admission(b,context=c,reservation=r,expected_digest=sha)


@pytest.mark.parametrize('drift',[False,True])
def test_real_process_boundary_revalidates_after_heartbeat(route_admission,runtime,monkeypatch,drift):
    from dataclasses import replace
    import time
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    from personal_agent_dal.worker.runtime_process import run_process
    c,r,b,e=route_admission
    supervisor=runtime[3]
    inventory=RuntimeInventory(supervisor)
    inventory.adopt(c,r)
    inventory.transition(c['attempt_id'],'prepared','dispatch_requested')
    inventory.transition(c['attempt_id'],'dispatch_requested','granted')
    plan=replace(build_plan(c,r,b['pins'],b['adapters']), production_enabled=True,
        admission=b, admission_sha256=validate_admission(b,context=c,reservation=r))
    seen=[]
    def intercept(argv,**kwargs):
        seen.append((argv,kwargs['env']))
        assert argv==plan.argv and kwargs['env']==plan.environment
        assert 'SENTINEL_AUTH_OUTPUT' not in json.dumps([argv,kwargs['env']])
        raise OSError('offline Popen boundary')
    monkeypatch.setattr(subprocess,'Popen',intercept)
    def heartbeat():
        if drift:Path(b['adapters']['roles']['reviewer']['helper']['path']).chmod(0o755)
        return True
    with pytest.raises(SupervisorRefusal if drift else OSError):
        run_process(inventory,c['attempt_id'],plan,heartbeat=heartbeat,deadline=time.time()+120)
    assert len(seen)==(0 if drift else 1)


@pytest.mark.parametrize('shape',['relative','missing','hardlink','directory','owner','growth'])
def test_helper_additional_filesystem_refusals(route_world,monkeypatch,shape,tmp_path):
    import os
    c,r,p,a=route_world;ref=a['roles']['reviewer']['helper'];path=Path(ref['path'])
    if shape=='relative':ref['path']='relative-helper'
    elif shape=='missing':path.unlink()
    elif shape=='hardlink':os.link(path,tmp_path/'hardlink')
    elif shape=='directory':path.unlink();path.mkdir(mode=0o700)
    elif shape=='owner':monkeypatch.setattr(os,'getuid',lambda:-1)
    elif shape=='growth':
        original=os.fstat
        def changed(fd):
            result=original(fd)
            if result.st_ino==path.stat().st_ino:
                with path.open('ab') as stream:stream.write(b'x')
            return result
        monkeypatch.setattr(os,'fstat',changed)
    with pytest.raises(SupervisorRefusal):build_plan(c,r,p,a)


@pytest.mark.parametrize('shape',['fifo','oversized','directory','invalid_json'])
def test_adapter_reader_bounded_fail_closed(tmp_path,shape):
    import os
    path=tmp_path/'bad-adapter'
    if shape=='fifo':os.mkfifo(path)
    elif shape=='directory':path.mkdir()
    else:path.write_bytes(b'x'*1048577 if shape=='oversized' else b'{')
    with pytest.raises(SupervisorRefusal):load_adapter_config(path)
