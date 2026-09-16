"""Offline public configuration, business API/CLI and provisioning boundaries."""
import copy
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from personal_agent_core.manifest import canonical_json
from personal_agent_dal.service.app import create_app
from personal_agent_dal.service.execution_config import ExecutionConfig, load_execution_config
from personal_agent_dal.service.execution_cli import run
from personal_agent_dal.service.operator_tokens import issue_operator_token
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.worker_models import WorkerJob, FeatureIntakeRequest
from personal_agent_dal.storage.machine_models import WorkflowProfileRevision
from tests.dal.test_trusted_execution_unit1 import world, preparation, request
from tests.dal.test_trusted_execution_closure import launched
from tests.dal.test_operator_api import _post_json

KEY = b'operator-fixture'


def config(engine):
    with session_factory(engine)() as s:
        profile = json.loads(s.get(WorkflowProfileRevision, 'B-1').body)
    profile.pop('fallback')
    profile['revision_id'] = 'B-1'
    return dict(schema_version='dal.execution-profiles/1.0', profiles=[profile], approved_inputs=[dict(
        repository_id='repo', base_sha='0'*40, toolchain_ref='test', toolchain_manifest_sha256='a'*64)])


def token(capabilities=('control','read')):
    return issue_operator_token(operator_id='operator', capabilities=list(capabilities),
        expires_at_epoch=int(time.time())+600, key=KEY)


def client(engine, cfg):
    return TestClient(create_app(engine, service_key=KEY, enrollment_secret=b'enroll', execution_config=cfg))


def post(c, path, body, caps=('control','read')):
    return _post_json(c, path, body, token(caps))


def test_register_prepare_confirm_without_bridge(world):
    c = client(world, config(world))
    body = dict(config(world)['approved_inputs'][0], task_description='Synthetic pending task')
    response = post(c, '/operator/tasks/register', body)
    assert response.status_code == 200, response.text
    registered = response.json()
    replay = post(c, '/operator/tasks/register', body).json()
    assert replay['duplicate'] and replay['feature_id'] == registered['feature_id']
    fid = registered['feature_id']
    prepare = preparation().model_dump(by_alias=True)
    prepare.update(execution_input=registered['execution_input'], expected_feature_version=registered['feature_version'])
    path = f'/operator/features/{fid}/execution-selections'
    p = post(c, path, prepare)
    assert p.status_code == 200, p.text
    assert post(c, path, prepare).json() == p.json()
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(WorkerJob)) == 0
        assert s.get(FeatureIntakeRequest, f'pending:{fid}') is not None
        assert s.scalar(select(func.count()).select_from(WorkflowProfileRevision)) == 1  # no fake A
    start_body = request(p.json()).model_dump()
    stale = dict(start_body, expected_feature_version=999)
    assert post(c, f'/operator/features/{fid}/executions', stale).status_code == 409
    started = post(c, f'/operator/features/{fid}/executions', start_body)
    assert started.status_code == 200, started.text
    assert post(c, f'/operator/features/{fid}/executions', start_body).json() == started.json()
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(WorkerJob)) == 1
    assert post(c, f'/operator/features/{fid}/workflow-selection', dict(request_id='replace',
        profile_revision_id='B-1', expected_feature_version=1, expected_gate_version=2)).status_code == 503
    assert post(c, '/internal/resume-proposals', {'assertion':'untrusted'}).status_code == 503


def test_missing_profile_auth_malformed_and_unapproved(world):
    cfg = config(world)
    c = client(world, cfg)
    body = dict(cfg['approved_inputs'][0], task_description='Synthetic')
    assert _post_json(c, '/operator/tasks/register', body).status_code == 401
    assert post(c, '/operator/tasks/register', body, ('read',)).status_code == 403
    assert post(c, '/operator/tasks/register', dict(body, private_key='no')).status_code == 400
    assert post(c, '/operator/tasks/register', dict(body, base_sha='1'*40)).status_code == 409
    cfg['profiles'] = []
    c = client(world, cfg)
    assert post(c, '/operator/features/f/execution-selections', preparation().model_dump(by_alias=True)).status_code == 409
    c = client(world, None)
    assert post(c, '/operator/tasks/register', body).status_code == 503
    assert post(c, '/operator/features/f/execution-selections', preparation().model_dump(by_alias=True)).status_code == 503
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(WorkerJob)) == 0


def test_public_config_version_and_no_defaults(world, tmp_path):
    path = tmp_path/'profiles.json'
    path.write_text(json.dumps(config(world))); path.chmod(0o600)
    loaded = load_execution_config(path)
    assert [p.profile for p in loaded.profiles] == ['B']
    malformed = config(world); malformed['schema_version']='future'
    with pytest.raises(ValueError): ExecutionConfig.model_validate(malformed)
    malformed = config(world); malformed['keys']={'unexpected':'trust'}
    with pytest.raises(ValueError): ExecutionConfig.model_validate(malformed)


def test_report_read_auth_service_attestation_and_feature_semantics(world, launched):
    t, lease, result, _ = launched
    assert t.submit_execution_result(lease,result)['accepted']
    c = client(world,None)
    url = f"/operator/provider-attempts/{result['result']['attempt_id']}/report"
    assert c.get(url).status_code == 401
    assert c.get(url,headers={'Authorization':'Bearer '+token(('control',))}).status_code == 403
    response = c.get(url,headers={'Authorization':'Bearer '+token(('read',))})
    assert response.status_code == 200, response.text
    value=response.json(); body=value['report']
    assert body['evidence'][0]['result']['report']=='Synthetic complete report'
    assert body['feature_state']=='coding' and body['feature_completed_by_report'] is False
    expected=hmac.new(KEY,b'dal.operator-execution-report/1.0\0'+canonical_json(body).encode(),hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected,value['attestation']['signature'])
    body['feature_completed_by_report']=True
    assert hmac.new(KEY,b'dal.operator-execution-report/1.0\0'+canonical_json(body).encode(),hashlib.sha256).hexdigest()!=expected


def test_cli_refuses_confirmation_tampering_before_http(world,tmp_path,monkeypatch):
    from tests.dal.test_trusted_execution_unit1 import prepared
    p = prepared(world)
    path=tmp_path/'prepared.json';path.write_text(json.dumps(p))
    calls=[]
    monkeypatch.setattr('personal_agent_dal.service.operator_cli._request',lambda *a,**kw:calls.append((a,kw)))
    args=SimpleNamespace(command='start',prepared_file=path,confirmed_digest='f'*64,
        request_id='cli-start',expires_at=int(time.time())+600,yes=True)
    with pytest.raises(SystemExit,match='Invalid'): run(args,'token')
    assert calls==[]
    p['execution_input']['task_description']='tampered';path.write_text(json.dumps(p))
    args.confirmed_digest=p['confirmed_execution_sha256']
    with pytest.raises(SystemExit,match='Invalid'): run(args,'token')
    assert calls==[]


def test_public_only_signed_prelaunch_and_report(world,tmp_path,monkeypatch):
    # Reuse the signed contract exercise, changing only production composition.
    import tests.dal.test_trusted_execution_closure as closure
    original = closure.create_app
    def public_app(engine, **kwargs):
        kwargs.pop('resume_config')
        return original(engine, execution_config=config(engine), **kwargs)
    monkeypatch.setattr(closure,'create_app',public_app)
    transport, lease, result, _ = closure.launched.__wrapped__(world,tmp_path)
    assert transport.submit_execution_result(lease,result)['accepted']
    assert transport.execution_status(lease)['classification']=='report_complete'


def test_supervisor_local_cli_identity_epoch_and_public_only(world,tmp_path,capsys):
    from cryptography.hazmat.primitives.asymmetric import ec
    from personal_agent.auth.device_keys import encode_device_public_key
    from personal_agent_core.timeutil import utc_now
    from personal_agent_dal.storage.transport_models import WorkerEnrollment,SupervisorIdentity
    from personal_agent_dal.service.deployment_cli import main
    with session_factory(world)() as s,s.begin():
        s.add(WorkerEnrollment(worker_id='w',machine_id='machine',capabilities='[]',created_at=utc_now()))
    public=tmp_path/'public.txt'
    public.write_text(encode_device_public_key(ec.generate_private_key(ec.SECP256R1()).public_key()))
    base=['--database',str(world.url.database),'register-supervisor','--worker-id','w',
        '--machine-id','machine','--boot-id','boot','--public-key-file',str(public)]
    assert main(base+['--kid','key1','--supervisor-epoch','2'])==0
    with pytest.raises(SystemExit): main(base+['--kid','key2','--supervisor-epoch','1'])
    assert main(base+['--kid','key2','--supervisor-epoch','3'])==0
    public.write_text('-----BEGIN PRIVATE KEY-----')
    with pytest.raises(SystemExit): main(base+['--kid','key3','--supervisor-epoch','4'])
    with session_factory(world)() as s:
        assert s.get(WorkerEnrollment,'w').registration_epoch==2
        assert s.get(SupervisorIdentity,'key3') is None


def test_console_business_commands_use_real_api(world,tmp_path,monkeypatch,capsys):
    from personal_agent_dal.service.operator_cli import main
    from personal_agent_dal.machine.workflow_selection import digest
    c=client(world,config(world))
    def transport(method,url,auth,payload=None,timeout=None):
        from urllib.parse import urlsplit
        path=urlsplit(url).path
        response=(_post_json(c,path,payload,auth) if method=='POST' else
            c.get(path,headers={'Authorization':'Bearer '+auth}))
        return response.status_code,response.json()
    monkeypatch.setattr('personal_agent_dal.service.operator_cli._request',transport)
    token_path=tmp_path/'token';token_path.write_text(token());token_path.chmod(0o600)
    common=['--base-url','https://testserver','--token-file',str(token_path)]
    source=tmp_path/'task.json';registered=tmp_path/'registered.json'
    source.write_text(json.dumps(dict(config(world)['approved_inputs'][0],task_description='CLI synthetic')))
    assert main(common+['register','--input-file',str(source),'--output-file',str(registered)])==0
    reg=json.loads(capsys.readouterr().out)
    assert json.loads(registered.read_text()) == reg['execution_input']
    prepared_file=tmp_path/'prepared.json'
    assert main(common+['prepare','--input-file',str(registered),'--feature-id',reg['feature_id'],
        '--request-id','cli-prepare','--action-key','cli-action','--role','planner',
        '--profile-revision-id','B-1','--feature-version',str(reg['feature_version']),
        '--gate-version','0','--output-file',str(prepared_file)])==0
    p=json.loads(prepared_file.read_text());assert digest(p['confirmation'])==p['confirmed_execution_sha256']
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(WorkerJob))==0
    assert main(common+['start','--prepared-file',str(prepared_file),'--request-id','cli-start',
        '--confirmed-digest',p['confirmed_execution_sha256'],'--expires-at',str(int(time.time())+600),'--yes'])==0
    from personal_agent_dal.storage.machine_models import ProviderAttempt
    with session_factory(world)() as s:
        attempt=s.scalar(select(ProviderAttempt))
        aid=attempt.attempt_id
    assert main(common+['report',aid])==0
    output=capsys.readouterr().out
    assert 'report_only' in output and 'wall_seconds' in output and 'gpt-6-astra' in output
    assert 'CLI synthetic' in output


def test_prelaunch_missing_profile_and_replacement_without_trust(world,launched):
    from personal_agent_dal.service.tokens import issue_token
    from personal_agent_dal.storage.machine_models import ExecutionJobBinding
    _,lease,result,_=launched
    auth=issue_token(worker_id='w',capabilities=[],key=KEY,expires_at_epoch=int(time.time())+600)
    cfg=config(world);cfg['profiles']=[]
    c=client(world,cfg)
    route=f'/worker/jobs/{lease.job_id}/prelaunch-context'
    response=_post_json(c,route,{'job_lease_epoch':lease.lease_epoch},auth)
    assert response.status_code==409 and 'PROFILE_UNAVAILABLE' in response.text
    with session_factory(world)() as s,s.begin():
        s.get(ExecutionJobBinding,result['result']['attempt_id']).origin='replacement'
    c=client(world,config(world))
    for path,body in [('prelaunch-context',{'job_lease_epoch':lease.lease_epoch}),
        ('prelaunch-manifest',{'job_lease_epoch':lease.lease_epoch,'assertion':'synthetic'}),
        ('prelaunch-dispatch',{'job_lease_epoch':lease.lease_epoch,'manifest_sha256':'a'*64})]:
        response=_post_json(c,f'/worker/jobs/{lease.job_id}/{path}',body,auth)
        assert response.status_code==503 and 'DAL_RESUME_UNAVAILABLE' in response.text


def test_service_entrypoint_wires_explicit_profile_file(world,tmp_path,monkeypatch):
    from personal_agent_dal.service.cli import main
    path=tmp_path/'profiles.json';path.write_text(json.dumps(config(world)));path.chmod(0o600)
    monkeypatch.delenv('PERSONAL_AGENT_DAL_RESUME_CONFIG',raising=False)
    monkeypatch.setenv('PERSONAL_AGENT_DAL_EXECUTION_PROFILES_CONFIG',str(path))
    monkeypatch.setattr('personal_agent_dal.service.cli._read_secret_file',lambda *args:KEY)
    captured=[]
    monkeypatch.setattr('personal_agent_dal.service.cli.uvicorn.run',lambda app,**kw:captured.append(app))
    assert main(['--database',str(world.url.database),'--service-key-file','synthetic-unused',
        '--enrollment-secret-file','synthetic-unused'])==0
    c=TestClient(captured[0])
    body=dict(config(world)['approved_inputs'][0],task_description='Composition synthetic')
    assert post(c,'/operator/tasks/register',body).status_code==200
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(WorkerJob))==0


def test_same_pending_registration_race_uses_real_sqlite(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    from sqlalchemy import event
    from personal_agent_dal.storage.models import Feature
    c = client(world, config(world))
    body = dict(config(world)['approved_inputs'][0], task_description='Concurrent pending synthetic')
    barrier, seen = Barrier(2), local()
    # Both independent transactions have read absence before either writes.
    def synchronize(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT') and 'feature_intake_requests' in statement.lower() and not getattr(seen, 'arrived', False):
            seen.arrived = True
            barrier.wait(timeout=10)
    event.listen(world, 'after_cursor_execute', synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(post, c, '/operator/tasks/register', body) for _ in range(2)]
            responses = [f.result(timeout=20) for f in futures]
    finally:
        event.remove(world, 'after_cursor_execute', synchronize)
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    rows = [r.json() for r in responses]
    assert rows[0]['feature_id'] == rows[1]['feature_id']
    assert sorted(r['duplicate'] for r in rows) == [False, True]
    assert rows[0]['execution_input'] == rows[1]['execution_input']
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(Feature).where(Feature.feature_id == rows[0]['feature_id'])) == 1
        assert s.scalar(select(func.count()).select_from(FeatureIntakeRequest)) == 1
        assert s.scalar(select(func.count()).select_from(WorkerJob)) == 0
    cfg = config(world)
    cfg['approved_inputs'].append(dict(cfg['approved_inputs'][0], toolchain_ref='different'))
    conflict = post(client(world, cfg), '/operator/tasks/register', dict(body, toolchain_ref='different'))
    assert conflict.status_code == 409 and 'toolchain_conflict' in conflict.text


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'symlink', 'huge', 'utf8', 'private', 'invalid'])
def test_public_key_file_refuses_unsafe_inputs_without_blocking(tmp_path, kind):
    import os
    import subprocess
    import sys
    path = tmp_path/'public'
    if kind == 'fifo': os.mkfifo(path)
    elif kind == 'directory': path.mkdir()
    elif kind == 'symlink':
        target = tmp_path/'target'; target.write_text('synthetic'); path.symlink_to(target)
    elif kind == 'huge':
        with path.open('wb') as stream: stream.truncate(1024*1024*1024)
    else:
        path.write_bytes({'utf8': b'\xff', 'private': b'-----BEGIN PRIVATE KEY-----', 'invalid': b'synthetic'}[kind])
    # A subprocess timeout makes a blocking FIFO regression fail boundedly.
    database = tmp_path/'db'; database.touch()
    result = subprocess.run([sys.executable, '-m', 'personal_agent_dal.service.deployment_cli',
        '--database', str(database), 'register-supervisor', '--kid', 'synthetic',
        '--worker-id', 'w', '--machine-id', 'm', '--boot-id', 'b',
        '--supervisor-epoch', '1', '--public-key-file', str(path)],
        capture_output=True, timeout=5)
    assert result.returncode == 1
    assert b'Supervisor public registration refused' in result.stderr
    assert database.stat().st_size == 0  # refused before database composition


@pytest.mark.parametrize('mode', [0o020000, 0o060000])
def test_public_key_rejects_device_descriptor_before_read(tmp_path, monkeypatch, mode):
    import os
    from personal_agent_dal.service.deployment_cli import _read_public_key
    path = tmp_path/'synthetic-device'; path.write_bytes(b'synthetic')
    monkeypatch.setattr(os, 'fstat', lambda fd: SimpleNamespace(st_mode=mode, st_size=0))
    monkeypatch.setattr(os, 'read', lambda *a, **kw: pytest.fail('device descriptor was read'))
    with pytest.raises(ValueError, match='PUBLIC_KEY_REQUIRED'):
        _read_public_key(path)


def test_public_key_bounds_read_even_if_file_grows_after_fstat(tmp_path, monkeypatch):
    import os
    from personal_agent_dal.service.deployment_cli import _read_public_key
    path = tmp_path/'growing'; path.write_bytes(b'x')
    actual_fstat = os.fstat
    def grow(fd):
        before = actual_fstat(fd)
        path.write_bytes(b'x' * 8192)
        return before
    monkeypatch.setattr(os, 'fstat', grow)
    actual_read = os.read
    reads = []
    def read(fd, size):
        reads.append(size)
        return actual_read(fd, size)
    monkeypatch.setattr(os, 'read', read)
    with pytest.raises(ValueError, match='PUBLIC_KEY_REQUIRED'):
        _read_public_key(path)
    assert reads == [4097]
