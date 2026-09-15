"""K3 remaining issues: synthetic failures at the composed authority boundary."""
from datetime import timedelta
import httpx
import pytest
from personal_agent.api.dal_resume import ProposalRequest
from personal_agent.storage.engine import session_factory
from personal_agent.storage.models import DalResumeDelivery
from tests.integration.test_dal_resume_bridge import bridge_world, click, token_ring, keyring
from tests.dal.test_p0_02_review_regressions import world


def test_proposal_replay_is_local_and_does_not_revive_expiry(bridge_world):
    _, bridge, auth, p, _ = bridge_world
    class Offline:
        def propose(self, body): raise httpx.ConnectError('synthetic-private-url')
    bridge.transport = Offline()
    body = ProposalRequest(request_id='pa-proposal', feature_id='f', selection_id=p['binding']['selection_id'])
    bridge.now = lambda: __import__('datetime').datetime.fromisoformat(p['expires_at']) + timedelta(seconds=1)
    assert bridge.proposal(auth, body) == p
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        bridge.proposal(auth, body.model_copy(update={'feature_id': 'other'}))
    with pytest.raises(ValueError, match='PROPOSAL_INVALID'): bridge.decide(auth, click(p))


def test_lost_delivery_expires_as_unknown(bridge_world):
    engine, bridge, auth, p, transport = bridge_world
    result = bridge.decide(auth, click(p))
    bridge.deliver_pending()
    bridge.now = lambda: __import__('datetime').datetime.fromisoformat(p['expires_at']) + timedelta(seconds=1)
    engine.dispose()
    bridge.deliver_pending()
    bridge.deliver_pending()
    with session_factory(engine)() as s:
        delivery = s.get(DalResumeDelivery, result['decision_id'])
        assert (delivery.status, delivery.attempts) == ('delivery_unknown', 1)
    assert transport.sent == 1


def test_operator_revoke_is_authenticated_durable_and_idempotent(world):
    from fastapi.testclient import TestClient
    from personal_agent_dal.service.app import create_app
    from tests.dal.test_operator_api import _post_json, _operator_token, SERVICE_KEY, ENROLLMENT_SECRET
    app = TestClient(create_app(world, service_key=SERVICE_KEY, enrollment_secret=ENROLLMENT_SECRET,
        resume_config={'issuer':'pa','audience':'dal','keys':{},'profiles':[]}))
    url = '/operator/human-decisions/not-yet-imported/revoke'
    payload = {'request_id':'revoke-one'}
    assert _post_json(app, url, payload).status_code == 401
    assert app.post(url, json=payload, headers={'Authorization':'Bearer '+_operator_token()}).status_code == 400
    first = _post_json(app, url, payload, _operator_token())
    assert first.status_code == 200, first.text
    assert first.json()['status'] == 'revoked_for_future_use'
    world.dispose()
    assert _post_json(app, url, payload, _operator_token()).json() == first.json()
    assert _post_json(app, url.replace('not-yet-imported','other'), payload, _operator_token()).status_code == 409
    assert _post_json(app, url, dict(payload, decision=True), _operator_token()).status_code == 400


@pytest.mark.parametrize('failure, expected', [('offline',503),(503,503),(429,503),(409,409)])
def test_proposal_http_failures_are_safe_and_replay_skips_transport(bridge_world, token_ring, keyring, failure, expected):
    from fastapi.testclient import TestClient
    from tests.integration.test_dal_control_plane_repairs import composed_app
    from personal_agent.auth.tokens import issue_access_token
    from personal_agent_core.timeutil import utc_now
    _, bridge, auth, p, _ = bridge_world
    calls=[]
    class Offline:
        def propose(self, body):
            calls.append(body)
            request=httpx.Request('POST','https://synthetic-private.invalid')
            if failure=='offline':raise httpx.ConnectError('synthetic-secret',request=request)
            raise httpx.HTTPStatusError('synthetic-secret',request=request,response=httpx.Response(failure,request=request,text='synthetic-secret'))
    bridge.transport=Offline()
    client=TestClient(composed_app(bridge_world,token_ring,keyring))
    token=issue_access_token(token_ring,device_id='phone',device_key_thumbprint=auth.key_thumbprint,
        scopes=['dal.resume.approve'],allowed_tools_version='v1',now=utc_now())
    headers={'Authorization':'Bearer '+token}
    body=dict(request_id='new-request',feature_id='f',selection_id=p['binding']['selection_id'])
    response=client.post('/v1/dal/resume-proposals',json=body,headers=headers)
    assert response.status_code==expected
    assert 'synthetic' not in response.text
    body['request_id']='pa-proposal'
    assert client.post('/v1/dal/resume-proposals',json=body,headers=headers).json()==p
    assert len(calls)==1
    body['feature_id']='wrong-binding'
    assert client.post('/v1/dal/resume-proposals',json=body,headers=headers).status_code==403
    assert len(calls)==1


@pytest.mark.parametrize('which',['active','previous','dedicated'])
def test_load_bridge_uses_real_nonempty_ring(tmp_path, which):
    import json
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption
    from personal_agent.auth.tokens import TokenKeyRing, SigningKey
    from personal_agent.api.dal_client import load_bridge
    keys={name:ec.generate_private_key(ec.SECP256R1()) for name in ('active','previous','dedicated')}
    ring=TokenKeyRing(active=SigningKey('active',keys['active'],keys['active'].public_key()),
        previous=[SigningKey('previous',None,keys['previous'].public_key())])
    path=tmp_path/'key.pem'
    path.write_bytes(keys[which].private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()));path.chmod(0o600)
    config=tmp_path/'config.json'
    config.write_text(json.dumps(dict(base_url='https://synthetic.invalid',signing_key_file=str(path),kid='dedicated',issuer='pa',audience='dal')));config.chmod(0o600)
    if which=='dedicated':assert load_bridge(config,session_factory=None,token_ring=ring).kid=='dedicated'
    else:
        with pytest.raises(ValueError,match='KEY_NOT_DEDICATED'):load_bridge(config,session_factory=None,token_ring=ring)


@pytest.mark.parametrize('problem',['owner','symlink','mode','oversize','directory','fifo'])
def test_dal_config_uses_safe_reader(tmp_path,monkeypatch,problem):
    import os
    from personal_agent_dal.service.resume_routes import load_config
    path=tmp_path/'config';path.write_text('{}');path.chmod(0o600)
    if problem=='owner':monkeypatch.setattr(os,'getuid',lambda:-1)
    elif problem=='symlink':
        link=tmp_path/'link';link.symlink_to(path);path=link
    elif problem=='mode':path.chmod(0o644)
    elif problem=='oversize':path.write_bytes(b' '*65537)
    elif problem=='directory':path.unlink();path.mkdir(mode=0o600)
    elif problem=='fifo':path.unlink();os.mkfifo(path,0o600)
    with pytest.raises(ValueError,match='CONFIG_FILE_INVALID'):load_config(path)


@pytest.mark.parametrize('consumed',[False,True])
def test_lost_reply_through_expiry_preserves_dal_truth(bridge_world,world,consumed):
    from sqlalchemy import select,func
    from personal_agent_dal.storage.engine import session_factory as dal_sessions
    from personal_agent_dal.storage.machine_models import ResumeApprovalBinding,DispatchIntent
    from personal_agent_dal.machine.resume_authority import resume, ResumeRequest, decision_status
    from datetime import datetime
    engine,bridge,auth,p,transport=bridge_world
    result=bridge.decide(auth,click(p));bridge.deliver_pending()
    with dal_sessions(world)() as s:approval_id=s.scalar(select(ResumeApprovalBinding.approval_id))
    body=ResumeRequest(request_id='consume-after-lost-reply',approval_id=approval_id)
    if consumed:receipt=resume(world,feature_id='f',body=body)
    after=datetime.fromisoformat(p['expires_at'])+timedelta(seconds=1)
    bridge.now=lambda:after
    bridge.deliver_pending();engine.dispose();bridge.deliver_pending()
    assert bridge.decide(auth,click(p))['delivery_status']=='delivery_unknown'
    if consumed:
        assert resume(world,feature_id='f',body=body,now=after)==receipt
        assert decision_status(world,decision_id=result['decision_id'])['status']=='consumed'
    else:
        with pytest.raises(ValueError,match='APPROVAL_INVALID'):resume(world,feature_id='f',body=body,now=after)
    with dal_sessions(world)() as s:
        assert s.scalar(select(func.count()).select_from(DispatchIntent))==int(consumed)
    assert transport.sent==1


def test_revoke_receipt_audit_and_tombstone_are_atomic(world):
    from sqlalchemy import event,select,func
    from personal_agent_dal.machine.resume_authority import revoke_decision,RevokeRequest
    from personal_agent_dal.storage.engine import session_factory
    from personal_agent_dal.storage.machine_models import ResumeRevocation,ResumeRevokeReceipt
    def fail(conn,cursor,statement,*args):
        if statement.startswith('INSERT INTO audit_events'):raise RuntimeError('synthetic-audit-failure')
    event.listen(world,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError):revoke_decision(world,decision_id='not-imported',body=RevokeRequest(request_id='revoke-atomic'),actor='operator')
    finally:event.remove(world,'before_cursor_execute',fail)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(ResumeRevocation))==0
        assert s.scalar(select(func.count()).select_from(ResumeRevokeReceipt))==0


@pytest.mark.parametrize('service',['pa','dal'])
@pytest.mark.parametrize('field',['issuer','audience','kid'])
@pytest.mark.parametrize('invalid',['',123,'bad space'])
def test_trust_ids_are_strict_in_both_loaders(tmp_path,token_ring,service,field,invalid):
    import json
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption
    from personal_agent.auth.device_keys import encode_device_public_key
    from personal_agent.api.dal_client import load_bridge
    from personal_agent_dal.service.resume_routes import load_config
    key=ec.generate_private_key(ec.SECP256R1())
    pem=tmp_path/'key';pem.write_bytes(key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()));pem.chmod(0o600)
    if service=='pa':
        body=dict(base_url='https://synthetic.invalid',signing_key_file=str(pem),kid='resume',issuer='pa',audience='dal')
        body[field]=invalid
    else:
        # JSON object keys are strings; numeric kid is not representable there.
        if field=='kid' and invalid==123:invalid='非法'
        body=dict(issuer='pa',audience='dal',keys={'resume':encode_device_public_key(key.public_key())},profiles=[{'profile':'A'},{'profile':'B'}])
        if field=='kid':body['keys']={invalid:encode_device_public_key(key.public_key())}
        else:body[field]=invalid
    config=tmp_path/'config';config.write_text(json.dumps(body));config.chmod(0o600)
    with pytest.raises(ValueError,match='TRUST_IDS_INVALID'):
        if service=='pa':load_bridge(config,session_factory=None,token_ring=token_ring)
        else:load_config(config)


def test_dal_startup_config_refusal_is_safe(tmp_path,monkeypatch,capsys):
    from personal_agent_dal.service import cli
    database=tmp_path/'db';database.touch()
    monkeypatch.setattr(cli,'_read_secret_file',lambda *args:b'synthetic-only')
    monkeypatch.setenv('PERSONAL_AGENT_DAL_RESUME_CONFIG',str(tmp_path/'private-path'))
    monkeypatch.setattr(cli.uvicorn,'run',lambda *a,**kw:pytest.fail('must not serve'))
    assert cli.main(['--database',str(database),'--service-key-file','unused','--enrollment-secret-file','unused'])==1
    assert capsys.readouterr().err=='DAL resume configuration refused\n'


def test_pa_cli_uses_only_bridge_environment(tmp_path,monkeypatch):
    import sys
    from personal_agent import cli
    captured=[]
    async def serve(config,*args,**kwargs):captured.append(config)
    monkeypatch.setattr(cli,'_serve',serve)
    monkeypatch.setattr(cli,'load_write_switch',lambda:None)
    monkeypatch.setattr(sys,'argv',['personal-agent-api','--database',str(tmp_path/'db')])
    monkeypatch.setenv('PERSONAL_AGENT_USER_ID','synthetic-user')
    monkeypatch.setenv('PERSONAL_AGENT_DAL_RESUME_CONFIG','dal-only')
    monkeypatch.setenv('PERSONAL_AGENT_DAL_RESUME_BRIDGE_CONFIG','pa-only')
    cli.main()
    assert str(captured[-1].dal_resume_config)=='pa-only'
    monkeypatch.delenv('PERSONAL_AGENT_DAL_RESUME_BRIDGE_CONFIG')
    cli.main()
    assert captured[-1].dal_resume_config is None


def test_pa_startup_config_refusal_precedes_network(tmp_path,monkeypatch,token_ring):
    import asyncio
    from personal_agent.api import composition as c
    from personal_agent.api.composition import AgentServiceConfig,CompositionError
    from personal_agent.storage.engine import create_database_engine
    from personal_agent.storage import db
    path=tmp_path/'pa.db';engine=create_database_engine(path);db.upgrade(engine);engine.dispose()
    for name in ('load_agent_data_keyring','load_service_signing_ring','load_cursor_key','load_identifier_key'):
        monkeypatch.setattr(c,name,lambda:None)
    monkeypatch.setattr(c,'load_access_token_ring',lambda:token_ring)
    async def network(*a,**kw):pytest.fail('config must fail before discovery')
    monkeypatch.setattr(c,'_discover',network)
    config=AgentServiceConfig(database=path,finance_mcp_url='http://127.0.0.1:9999/mcp',
        finance_control_url='http://127.0.0.1:9999',user_id='synthetic',dal_resume_config=tmp_path/'private-config')
    async def run():
        with pytest.raises(CompositionError,match='^DAL resume configuration refused$'):
            async with c.agent_service(config,write_switch=None,build_gateway=lambda:None,build_structured_client=lambda **kw:None):
                pytest.fail('must not serve')
    asyncio.run(run())


@pytest.mark.parametrize('status,attempts,approval_id', [('delivery_unknown',0,None),('expired',1,None),('accepted',1,None),('queued',-1,None)])
def test_delivery_status_constraints(bridge_world,status,attempts,approval_id):
    from sqlalchemy.exc import IntegrityError
    engine,bridge,auth,p,_=bridge_world
    result=bridge.decide(auth,click(p))
    with pytest.raises(IntegrityError):
        with session_factory(engine)() as s,s.begin():
            row=s.get(DalResumeDelivery,result['decision_id'])
            row.status,row.attempts,row.approval_id=status,attempts,approval_id


def test_dal_startup_profile_refusal_is_safe(tmp_path,monkeypatch,capsys):
    import json
    from cryptography.hazmat.primitives.asymmetric import ec
    from personal_agent.auth.device_keys import encode_device_public_key
    from personal_agent_dal.service import cli
    database=tmp_path/'db';database.touch()
    config=tmp_path/'config'
    config.write_text(json.dumps(dict(issuer='pa',audience='dal',keys={'resume':encode_device_public_key(ec.generate_private_key(ec.SECP256R1()).public_key())},profiles=[{'profile':'A'},{'profile':'B'}])));config.chmod(0o600)
    monkeypatch.setattr(cli,'_read_secret_file',lambda *args:b'synthetic-only')
    monkeypatch.setenv('PERSONAL_AGENT_DAL_RESUME_CONFIG',str(config))
    monkeypatch.setattr(cli.uvicorn,'run',lambda *a,**kw:pytest.fail('must not serve'))
    assert cli.main(['--database',str(database),'--service-key-file','unused','--enrollment-secret-file','unused'])==1
    assert capsys.readouterr().err=='DAL resume configuration refused\n'


def test_operator_revoke_before_import_blocks_signed_decision(world):
    from fastapi.testclient import TestClient
    from personal_agent_dal.service.app import create_app
    from tests.dal.test_operator_api import _post_json,_operator_token,SERVICE_KEY,ENROLLMENT_SECRET
    from tests.dal.test_p0_02_resume_authority import setup_resume,approve
    _,_,key,claims=setup_resume(world)
    app=TestClient(create_app(world,service_key=SERVICE_KEY,enrollment_secret=ENROLLMENT_SECRET,
        resume_config={'issuer':'pa','audience':'dal','keys':{},'profiles':[]}))
    response=_post_json(app,'/operator/human-decisions/decision/revoke',{'request_id':'explicit-revoke'},_operator_token())
    assert response.status_code==200
    world.dispose()
    with pytest.raises(ValueError,match='APPROVAL_REVOKED'):approve(world,key,claims)


def test_migration_preserves_existing_ambiguous_delivery(bridge_world):
    from personal_agent.storage import db
    from sqlalchemy import text
    engine,bridge,auth,p,_=bridge_world
    bridge.decide(auth,click(p))
    db.downgrade(engine,'0006_dal_resume_decisions')
    with engine.begin() as c:c.execute(text("UPDATE dal_resume_deliveries SET status='expired',attempts=1"))
    db.upgrade(engine)
    with engine.connect() as c:
        assert c.execute(text('SELECT status,attempts FROM dal_resume_deliveries')).one()==('delivery_unknown',1)
        assert c.execute(text('PRAGMA foreign_key_check')).all()==[]
    with pytest.raises(RuntimeError,match='ambiguous delivery'):db.downgrade(engine,'0006_dal_resume_decisions')
