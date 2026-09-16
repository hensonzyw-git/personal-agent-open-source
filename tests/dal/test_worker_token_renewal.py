"""Failure-first renewal contract: no identity mutation or enrollment fallback.

Enumerated before implementation: repeated/idle renewal, explicit registration,
wrong secret and each identity field, revocation, closed token/request shapes,
expired bearer alone, concurrent registration/revoke, redirects/lost responses,
private cache, bounded 401 recovery and production Supervisor config wiring.
All credentials and databases below are synthetic; no provider is composed.
"""
import base64
import hashlib
import hmac
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from personal_agent_core.timeutil import utc_now
from personal_agent_dal.service.app import create_app, RateLimiter
from personal_agent_dal.service.tokens import issue_token, verify_token, TokenError
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory
from personal_agent_dal.storage.transport_models import WorkerEnrollment, SupervisorIdentity
from personal_agent_dal.worker.remote import RemoteHttpAdapter, RemoteTransportSettings
from personal_agent_dal.worker.transport import TransportError

KEY = b'synthetic-service-key'
SECRET = 'synthetic-enrollment-secret'
IDENTITY = dict(worker_id='worker-test', machine_id='machine-test', registration_epoch=1,
                kid='kid-test', boot_id='boot-test', supervisor_epoch=1)
BASE = dict(schema_version='dal.worker-transport/1.0', request_id='request-test',
            worker_id='worker-test', machine_id='machine-test', capabilities=['coding'])
REFRESH = dict(BASE, expected_registration_epoch=1,
               supervisor=dict(kid='kid-test', boot_id='boot-test', supervisor_epoch=1))


def post(client, path, body, secret=SECRET, token=None):
    data = json.dumps(body).encode()
    headers = {'content-type':'application/json', 'x-transport-body-digest': hashlib.sha256(data).hexdigest()}
    if secret is not None: headers['x-enrollment-secret'] = secret
    if token: headers['authorization'] = 'Bearer ' + token
    return client.post(path, content=data, headers=headers)


@pytest.fixture
def setup(tmp_path):
    engine = create_database_engine(tmp_path / 'service.db')
    db.upgrade(engine)
    with session_factory(engine)() as s:
        s.add(WorkerEnrollment(worker_id=BASE['worker_id'], machine_id=BASE['machine_id'],
                              capabilities='["coding"]', registration_epoch=1, created_at=utc_now()))
        s.flush()
        s.add(SupervisorIdentity(**IDENTITY, public_key='synthetic-public-key'))
        s.commit()
    with TestClient(create_app(engine, service_key=KEY, enrollment_secret=SECRET.encode())) as client:
        yield engine, client
    engine.dispose()


def claim(client, token):
    return post(client, '/jobs/claim', {k: BASE[k] for k in ('schema_version','request_id','worker_id')}, secret=None, token=token)


def test_repeated_refresh_preserves_supervisor_and_epoch(setup):
    engine, client = setup
    for _ in range(3):
        before = int(time.time())
        response = post(client, '/token/refresh', REFRESH)
        assert response.status_code == 200, response.text
        assert 2592000 <= response.json()['token_expires_at'] - before <= 2592001
        assert claim(client, response.json()['token']).status_code == 204
    from personal_agent_dal.machine.isolation_evidence import _current
    with session_factory(engine)() as s:
        _current(s, IDENTITY)
        assert s.get(WorkerEnrollment, BASE['worker_id']).registration_epoch == 1


def test_explicit_enrollment_invalidates_token_and_supervisor(setup):
    engine, client = setup
    response = post(client, '/token/refresh', REFRESH)
    assert response.status_code == 200
    assert post(client, '/enroll', BASE).status_code == 200
    assert claim(client, response.json()['token']).status_code == 403
    assert post(client, '/token/refresh', REFRESH).status_code == 403
    with session_factory(engine)() as s:
        assert s.get(WorkerEnrollment, BASE['worker_id']).registration_epoch == 2


@pytest.mark.parametrize('field,value', [('worker_id','unknown'),('machine_id','wrong'),
    ('capabilities',['verification']),('expected_registration_epoch',2),
    ('expected_registration_epoch',True),('schema_version','unknown'),('extra','bad'),
    ('supervisor',None),('supervisor',dict(kid='wrong',boot_id='boot-test',supervisor_epoch=1)),
    ('supervisor',dict(kid='kid-test',boot_id='wrong',supervisor_epoch=1)),
    ('supervisor',dict(kid='kid-test',boot_id='boot-test',supervisor_epoch=2))])
def test_wrong_identity_closed_request(setup, field, value):
    _, client = setup
    response = post(client, '/token/refresh', dict(REFRESH, **{field:value}))
    assert response.status_code in (400,403)
    assert 'token' not in response.json()


@pytest.mark.parametrize('secret', [None, 'wrong'])
def test_secret_required_even_with_expired_bearer(setup, secret):
    _, client = setup
    expired = issue_token(worker_id=BASE['worker_id'], capabilities=['coding'], expires_at_epoch=1, key=KEY)
    response = post(client, '/token/refresh', REFRESH, secret=secret, token=expired)
    assert response.status_code == 401
    assert SECRET not in response.text and expired not in response.text


@pytest.mark.parametrize('model', [WorkerEnrollment, SupervisorIdentity])
def test_revocation_fresh_on_next_request(setup, model):
    engine, client = setup
    response = post(client, '/token/refresh', REFRESH)
    assert response.status_code == 200
    with session_factory(engine)() as s:
        s.execute(update(model).values(revoked_at=utc_now()))
        s.commit()
    assert post(client, '/token/refresh', REFRESH).status_code == 403
    if model is WorkerEnrollment:
        assert claim(client, response.json()['token']).status_code == 403


def signed(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return raw + '.' + hmac.new(KEY, raw.encode(), hashlib.sha256).hexdigest()


@pytest.mark.parametrize('change', [{'schema':'unknown'}, {'extra':1}, {'exp':True}, {'capabilities':['coding','coding']}, {'capabilities':['verification','coding']}])
def test_token_shape_is_closed(change):
    payload = dict(schema='dal.worker-token/1.0',worker_id='worker-test', capabilities=['coding'],exp=int(time.time())+100)
    with pytest.raises(TokenError):
        verify_token(signed(dict(payload, **change)), key=KEY, now_epoch=int(time.time()))


def test_legacy_token_cannot_authorize_bound_worker(setup):
    _, client = setup
    token = issue_token(worker_id=BASE['worker_id'],capabilities=['coding'],expires_at_epoch=int(time.time())+100,key=KEY)
    assert claim(client, token).status_code == 403


def settings(tmp_path):
    secret = tmp_path / 'synthetic.secret'
    secret.write_text(SECRET)
    secret.chmod(0o600)
    return RemoteTransportSettings(endpoint='https://service.invalid',worker_id=BASE['worker_id'],
        machine_id=BASE['machine_id'],capabilities=('coding',),enrollment_secret_path=secret,
        token_cache_path=tmp_path/'cache'/'token.json',checkpoint_root=tmp_path/'checkpoint',
        ca_bundle_path=None,request_timeout_seconds=1,retry_attempts=2,
        backoff_base_seconds=0,backoff_max_seconds=0,identity=IDENTITY)


def test_remote_missing_expired_restart_uses_refresh(setup, tmp_path):
    engine, client = setup
    paths=[]
    def handle(req):
        paths.append(req.url.path)
        assert req.url.host == 'service.invalid'
        response = client.request(req.method, req.url.path, content=req.content, headers=dict(req.headers))
        return httpx.Response(response.status_code,content=response.content,headers=dict(response.headers))
    config = settings(tmp_path)
    for now in (lambda:int(time.time()), lambda:int(time.time()), lambda:int(time.time())+2592001):
        # Server clock remains real: force renewal via cached expiry, then use real now.
        adapter = RemoteHttpAdapter(config,client=httpx.Client(transport=httpx.MockTransport(handle)),now_epoch=now)
        if now() > int(time.time())+100: 
            adapter._now_epoch = lambda:int(time.time())
            cache=json.loads(config.token_cache_path.read_text()); cache['expires_at']=1
            config.token_cache_path.write_text(json.dumps(cache))
        assert adapter.claim() is None
    assert '/enroll' not in paths
    assert paths.count('/token/refresh') == 2
    assert config.token_cache_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('kind', ['redirect','lost','legacy','unauthorized','revoked'])
def test_remote_failure_bounded_without_enroll(setup,tmp_path,kind):
    _, client=setup
    paths=[]
    def handle(req):
        paths.append(req.url.path)
        if req.url.path == '/token/refresh':
            if kind=='redirect':return httpx.Response(307,headers={'location':'https://evil.invalid'})
            if kind=='lost':raise httpx.ReadError(SECRET)
            if kind=='legacy':return httpx.Response(404,json={'code':'unsupported'})
            response = client.request(req.method,req.url.path,content=req.content,headers=dict(req.headers))
            return httpx.Response(response.status_code,content=response.content,headers=dict(response.headers))
        return httpx.Response(403 if kind=='revoked' else 401,json={'code':'worker_revoked' if kind=='revoked' else 'token_invalid'})
    with pytest.raises(TransportError) as error:
        RemoteHttpAdapter(settings(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle))).claim()
    assert SECRET not in str(error.value)
    assert '/enroll' not in paths
    assert paths.count('/token/refresh') <= 2
    assert paths.count('/jobs/claim') <= 2


@pytest.mark.parametrize('mutation', ['worker_revoke','supervisor_revoke','reregister'])
def test_concurrent_mutation_cannot_leave_accepted_stale_authority(setup, monkeypatch, mutation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import personal_agent_dal.service.app as module
    engine, client = setup
    checked, resume = Event(), Event()
    original = module.issue_token
    def pause_issue(**kwargs):
        checked.set()
        assert resume.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(module, 'issue_token', pause_issue)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(post,client,'/token/refresh',REFRESH)
        assert checked.wait(5)
        try:
            with session_factory(engine)() as s:
                if mutation == 'reregister':
                    s.execute(update(WorkerEnrollment).values(registration_epoch=2))
                else:
                    model = WorkerEnrollment if mutation == 'worker_revoke' else SupervisorIdentity
                    s.execute(update(model).values(revoked_at=utc_now()))
                s.commit()
        finally:
            resume.set()
        response = pending.result(timeout=5)
    # Signing can race a commit; the resulting old token must never authenticate.
    assert response.status_code == 200
    assert claim(client,response.json()['token']).status_code == 403
    assert post(client,'/token/refresh',REFRESH).status_code == 403


def test_parallel_renewals_do_not_advance_epoch(setup):
    from concurrent.futures import ThreadPoolExecutor
    engine, client = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:post(client,'/token/refresh',REFRESH),range(2)))
    assert [r.status_code for r in results] == [200,200]
    with session_factory(engine)() as s:
        assert s.get(WorkerEnrollment,BASE['worker_id']).registration_epoch == 1


def test_legacy_unbound_communication_and_capability_change(setup):
    engine, client = setup
    legacy = dict(BASE,worker_id='legacy-worker',machine_id='legacy-machine')
    response = post(client,'/enroll',legacy)
    assert response.status_code == 200
    token = response.json()['token']
    body = {k:legacy[k] for k in ('schema_version','request_id','worker_id')}
    assert post(client,'/jobs/claim',body,token=token).status_code == 204
    with session_factory(engine)() as s:
        s.execute(update(WorkerEnrollment).where(WorkerEnrollment.worker_id=='legacy-worker').values(capabilities='["verification"]'))
        s.commit()
    assert post(client,'/jobs/claim',body,token=token).status_code == 403


@pytest.mark.parametrize('payload', [[],None,{'schema':'dal.worker-token/2.0'},
    dict(schema='dal.worker-token/2.0',worker_id='w',machine_id='m',registration_epoch=True,capabilities=['coding'],exp=9999999999)])
def test_malformed_signed_payloads_refused(payload):
    with pytest.raises(TokenError):
        verify_token(signed(payload),key=KEY,now_epoch=1)


def test_refresh_rate_limit_and_redacted_audit(setup):
    engine, _ = setup
    with TestClient(create_app(engine,service_key=KEY,enrollment_secret=SECRET.encode(),rate_limiter=RateLimiter(1,60))) as client:
        assert post(client,'/token/refresh',REFRESH,secret='wrong').status_code == 401
        assert post(client,'/token/refresh',REFRESH).status_code == 429
    from sqlalchemy import text
    with engine.connect() as conn:
        rows=conn.execute(text("SELECT event_type,redacted_summary FROM audit_events WHERE event_type='worker.token_refresh'")).all()
    assert rows and all(SECRET not in str(row) for row in rows)


@pytest.mark.parametrize('cache_kind', ['wrong_epoch','legacy','permissions','missing'])
def test_bad_cache_renews_bound_identity(setup,tmp_path,cache_kind):
    _, client=setup
    config=settings(tmp_path)
    response=post(client,'/token/refresh',REFRESH).json()
    cache=dict(schema_version='dal.worker-token-cache/1.0',worker_id=BASE['worker_id'],
               token=response['token'],expires_at=response['token_expires_at'])
    if cache_kind=='wrong_epoch':
        cache['token']=issue_token(worker_id=BASE['worker_id'],machine_id=BASE['machine_id'],registration_epoch=2,
            capabilities=['coding'],expires_at_epoch=cache['expires_at'],key=KEY)
    elif cache_kind=='legacy':
        cache['token']=issue_token(worker_id=BASE['worker_id'],capabilities=['coding'],expires_at_epoch=cache['expires_at'],key=KEY)
    config.token_cache_path.parent.mkdir()
    if cache_kind!='missing':
        config.token_cache_path.write_text(json.dumps(cache))
        config.token_cache_path.chmod(0o644 if cache_kind=='permissions' else 0o600)
    paths=[]
    def handle(req):
        paths.append(req.url.path)
        response=client.request(req.method,req.url.path,content=req.content,headers=dict(req.headers))
        return httpx.Response(response.status_code,content=response.content,headers=dict(response.headers))
    adapter=RemoteHttpAdapter(config,client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert adapter.claim() is None
    assert paths==['/token/refresh','/jobs/claim']
    assert config.token_cache_path.stat().st_mode & 0o777 == 0o600


def test_production_transport_loads_existing_supervisor_identity(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from personal_agent_dal.worker.cli import _open_transport
    import personal_agent_dal.worker.cli as cli
    config=settings(tmp_path)
    path=tmp_path/'supervisor.json'
    path.write_text(json.dumps(dict(root=str(tmp_path/'root'),boot_id='boot-test',supervisor_epoch=1,
        identity=IDENTITY,read_roots=[],runtime_pins=[],signing_key_path=str(tmp_path/'never-read'),git_pin={})))
    worker=SimpleNamespace(transport=config,worker_id=BASE['worker_id'],checkpoint_root=tmp_path/'checkpoint',
        supervisor_config_path=path,schema_version='dal.worker-config/2.1')
    captured=[]
    class Adapter:
        def __init__(self, settings):captured.append(settings)
        def close(self):pass
    monkeypatch.setattr(cli,'RemoteHttpAdapter',Adapter)
    with _open_transport(worker):pass
    assert captured[0].identity == IDENTITY
    assert not (tmp_path/'never-read').exists()


@pytest.mark.parametrize('change', [{'schema_version':'unknown'}, {'extra':1}, {'capabilities':None}, {'token':'unsupported'}])
def test_bad_refresh_response_does_not_downgrade(setup,tmp_path,change):
    _, client=setup
    response=post(client,'/token/refresh',REFRESH).json()
    paths=[]
    def handle(req):
        paths.append(req.url.path)
        return httpx.Response(200,json=dict(response,**change))
    with pytest.raises(TransportError):
        RemoteHttpAdapter(settings(tmp_path),client=httpx.Client(transport=httpx.MockTransport(handle))).claim()
    assert paths==['/token/refresh']


def test_lost_committed_refresh_recovers_on_restart_without_mutation(setup,tmp_path):
    engine,client=setup
    config=settings(tmp_path)
    calls=[]
    def lost(req):
        calls.append(req.url.path)
        response=client.request(req.method,req.url.path,content=req.content,headers=dict(req.headers))
        assert response.status_code == 200
        raise httpx.ReadError(SECRET)
    import traceback
    with pytest.raises(TransportError) as caught:
        RemoteHttpAdapter(config,client=httpx.Client(transport=httpx.MockTransport(lost))).claim()
    assert SECRET not in ''.join(traceback.format_exception(caught.value))
    assert calls==['/token/refresh'] and not config.token_cache_path.exists()
    def recovered(req):
        calls.append(req.url.path)
        response=client.request(req.method,req.url.path,content=req.content,headers=dict(req.headers))
        return httpx.Response(response.status_code,content=response.content,headers=dict(response.headers))
    assert RemoteHttpAdapter(config,client=httpx.Client(transport=httpx.MockTransport(recovered))).claim() is None
    assert calls==['/token/refresh','/token/refresh','/jobs/claim']
    with session_factory(engine)() as s:
        assert s.get(WorkerEnrollment,BASE['worker_id']).registration_epoch==1


def test_near_expiry_renews_once_and_pin_keeps_mount_prefix(setup,tmp_path):
    _,client=setup
    config=replace(settings(tmp_path),endpoint='https://service.invalid/dal/')
    from personal_agent_dal.worker.remote import CachedToken
    expires=int(time.time())+30
    token=issue_token(worker_id=BASE['worker_id'],machine_id=BASE['machine_id'],registration_epoch=1,
        capabilities=['coding'],expires_at_epoch=expires,key=KEY)
    paths=[]
    def handle(req):
        paths.append(req.url.path)
        assert req.url.host=='service.invalid'
        response=client.request(req.method,req.url.path.removeprefix('/dal'),content=req.content,headers=dict(req.headers))
        return httpx.Response(response.status_code,content=response.content,headers=dict(response.headers))
    adapter=RemoteHttpAdapter(config,client=httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=True))
    adapter._token=CachedToken(BASE['worker_id'],token,expires)
    assert adapter.claim() is None
    assert paths==['/dal/token/refresh','/dal/jobs/claim']
