"""Two real databases, signed dispatch and crash/revocation boundaries."""
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, text

from personal_agent.api.dal_timeline import TimelineBridge
from personal_agent.api.dal_timeline_client import TimelineTransport
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent.storage.models import Device, DalTimelineCommand
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.timeline.transport import TimelineEndpoint
from tests.dal.test_timeline_requests import world
from test_agent_api import engine, token_ring, keyring, NOW


@pytest.fixture
def bridge_world(world, tmp_path):
    pa_key, dal_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    endpoint = TimelineEndpoint(world[2], trusted_keys={'pa':pa_key.public_key()}, signing_key=dal_key, kid='dal')
    transport = TimelineTransport(base_url='http://127.0.0.1:8820', key=pa_key, kid='pa', trusted_keys={'dal':dal_key.public_key()})
    state = {'lose':False,'calls':0}
    def post(path, payload):
        state['calls'] += 1
        result = endpoint.dispatch(payload, command=path.endswith('/commands'))
        if state['lose']: raise OSError('synthetic response loss')
        return result
    transport._post = post
    engine = create_database_engine(tmp_path/'pa-timeline.db')
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as s, s.begin():
        s.add(Device(device_id='phone', display_name='Synthetic', public_key='synthetic',
            device_key_thumbprint='synthetic-thumb', status='active',
            scopes=json.dumps(['dal.request','dal.read']),allowed_tools_version='v1',created_at=utc_now()))
    auth = SimpleNamespace(device_id='phone',subject_id='device:phone',key_thumbprint='synthetic-thumb',scopes=['dal.request','dal.read'])
    ring = KeyRing([generate_key('pa-test')],service='pa')
    bridge = TimelineBridge(session_factory=sessions,keyring=ring,transport=transport)
    yield bridge, auth, state, world[2], engine
    engine.dispose()


def queue(bridge, auth):
    return bridge.queue_submit(auth,command_id='command',source_message_ref='source',body='Synthetic secret request')


def test_lost_reply_reopen_replays_same_command(bridge_world):
    bridge,auth,state,dal,engine = bridge_world
    assert queue(bridge,auth)['status']=='queued'
    assert state['calls']==0
    state['lose']=True
    bridge.deliver_pending()
    assert bridge.command(auth,'command')['status']=='delivery_unknown'
    assert dal.list_tasks(subject=auth.subject_id)['total']==1
    restarted=TimelineBridge(session_factory=bridge.sessions,keyring=bridge.keyring,transport=bridge.transport)
    state['lose']=False
    restarted.deliver_pending()
    result=restarted.command(auth,'command')
    assert result['status']=='accepted'
    assert result['receipt']['status']=='accepted' and result['receipt']['request']['status']=='accepted_not_started'
    assert dal.list_tasks(subject=auth.subject_id)['total']==1
    with engine.connect() as c:
        assert 'Synthetic secret request' not in str(c.execute(text('SELECT * FROM dal_timeline_commands')).all())


def test_revocation_before_dispatch_cancels_without_sending(bridge_world):
    bridge,auth,state,_,_ = bridge_world
    queue(bridge,auth)
    with bridge.sessions() as s,s.begin(): s.get(Device,'phone').status='revoked';s.get(Device,'phone').revoked_at=utc_now()
    bridge.deliver_pending()
    assert state['calls']==0
    with bridge.sessions() as s:
        assert s.get(DalTimelineCommand,'command').status=='cancelled'
    with pytest.raises(ValueError,match='DEVICE_INACTIVE'): bridge.command(auth,'command')


def test_revocation_after_unknown_does_not_claim_cancellation(bridge_world):
    bridge,auth,state,_,_ = bridge_world
    queue(bridge,auth);state['lose']=True;bridge.deliver_pending()
    with bridge.sessions() as s,s.begin(): s.get(Device,'phone').status='revoked';s.get(Device,'phone').revoked_at=utc_now()
    bridge.deliver_pending()
    assert state['calls']==1
    with bridge.sessions() as s:
        assert s.get(DalTimelineCommand,'command').status=='delivery_unknown'


def test_payload_conflict_and_scope_are_checked_before_replay(bridge_world):
    bridge,auth,state,_,_ = bridge_world
    queue(bridge,auth)
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        bridge.queue_submit(auth,command_id='command',source_message_ref='source',body='different')
    auth.scopes=[]
    with pytest.raises(ValueError,match='SCOPE_REQUIRED'):queue(bridge,auth)
    assert state['calls']==0


def test_query_rechecks_authorization_after_network(bridge_world):
    bridge,auth,_,_,_ = bridge_world
    original=bridge.transport.call
    def revoke(**kwargs):
        result=original(**kwargs)
        with bridge.sessions() as s,s.begin():s.get(Device,'phone').status='revoked';s.get(Device,'phone').revoked_at=utc_now()
        return result
    bridge.transport.call=revoke
    with pytest.raises(ValueError,match='DEVICE_INACTIVE'):
        bridge.query(auth,operation='request_list',body={})


def test_response_from_another_request_cannot_be_accepted(bridge_world):
    bridge,auth,_,_,_ = bridge_world
    from personal_agent_dal.timeline.transport import envelope
    attacker=ec.generate_private_key(ec.SECP256R1())
    bridge.transport.trusted_keys={'wrong':attacker.public_key()}
    bridge.transport._post=lambda *a: envelope(key=attacker,kid='wrong',issuer='dal-timeline',audience='pa-timeline',
        operation='request_list',request_id='different',subject=auth.subject_id,scope='dal.read',body={})
    with pytest.raises(ValueError,match='DAL_RESPONSE_INVALID'):
        bridge.query(auth,operation='request_list',body={})


def test_public_query_routes_use_device_authentication(bridge_world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from personal_agent.api.dal_timeline import mount_routes
    bridge,auth,_,_,_=bridge_world
    app=FastAPI()
    deps=SimpleNamespace(session_factory=bridge.sessions,dal_timeline=bridge)
    mount_routes(app,deps,lambda request,session:auth)
    client=TestClient(app)
    assert client.get('/v1/dal/tasks').json()['total']==0
    auth.scopes=[]
    assert client.get('/v1/dal/tasks').status_code==403
    assert client.get('/v1/dal/tasks?limit=51').status_code==400
    assert client.post('/v1/dal/tasks',json={}).status_code==405


@pytest.mark.parametrize('attack',['none','model_text','foreign_source'])
def test_model_host_queues_original_user_text_atomically(engine,token_ring,keyring,bridge_world,attack):
    from test_runtime_v2_api import client_for, _auth
    from test_adk_runtime import fc
    from personal_agent.api.dal_timeline import TimelineBridge
    from personal_agent.storage.models import Base
    from sqlalchemy import func
    def submit_call(context,meta):
        return [fc('dal_submit_request','submit',arguments={'text':'forged'} if attack=='model_text' else {},task=meta,
            **({'write_source_refs':['foreign-source']} if attack=='foreign_source' else {}))]
    client,calls,deps=client_for(engine,token_ring,keyring,[submit_call])
    # Fixture auth and durable device both need explicit grants.
    with deps.session_factory() as s,s.begin():
        device=s.get(Device,'dev-1')
        scopes=json.loads(device.scopes)
        device.scopes=json.dumps(scopes+['dal.request','dal.read'])
    deps.dal_timeline=TimelineBridge(session_factory=deps.session_factory,keyring=keyring,transport=bridge_world[0].transport)
    from personal_agent.auth.tokens import issue_access_token
    token=issue_access_token(token_ring,device_id='dev-1',device_key_thumbprint='THUMB',scopes=scopes+['dal.request','dal.read'],allowed_tools_version='v1',now=NOW)
    headers={**_auth(token_ring),'Authorization':'Bearer '+token,'X-Client-Wire-Version':'4'}
    # Avoid lifespan here: assert the Host commits the outbox before dispatch.
    response=client.post('/v1/chat/messages',headers=headers,json={'conversation_id':'c1','text':'开发一个合成测试功能'})
    assert response.status_code==200,response.text
    if attack != 'none':
        with deps.session_factory() as s:assert s.scalar(select(func.count()).select_from(DalTimelineCommand))==0
        return
    assert '开发尚未开始' in response.json()['result_envelope']['text']
    with deps.session_factory() as s:
        row=s.scalar(select(DalTimelineCommand))
        assert row.status=='queued'
        assert deps.dal_timeline._open(row.command_id,'sealed_body',row.sealed_body)['payload']['text']=='开发一个合成测试功能'
        assert s.scalar(select(func.count()).select_from(Base.metadata.tables['agent_run_outcomes']))==1


def test_outbox_rolls_back_with_owning_host_transaction(bridge_world):
    bridge,auth,state,_,_=bridge_world
    with pytest.raises(RuntimeError):
        with bridge.sessions() as s,s.begin():
            bridge.queue_submit(auth,command_id='rollback',source_message_ref='source',body='Synthetic',_session=s)
            s.flush()
            raise RuntimeError('synthetic Timeline projection failure')
    with bridge.sessions() as s:assert s.get(DalTimelineCommand,'rollback') is None
    bridge.deliver_pending()
    assert state['calls']==0


def test_pa_outbox_migration_matches_metadata_and_refuses_destructive_downgrade(bridge_world):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from personal_agent.storage.models import Base
    bridge,auth,_,_,engine=bridge_world
    with engine.connect() as c:
        assert compare_metadata(MigrationContext.configure(c),Base.metadata)==[]
    queue(bridge,auth)
    with pytest.raises(RuntimeError,match='preserve outbox'):
        db.downgrade(engine,'0015_dal_delivery_status')


def test_protected_config_composes_dedicated_transport(bridge_world,tmp_path,token_ring):
    from cryptography.hazmat.primitives import serialization
    from personal_agent.api.dal_timeline_client import load_bridge
    from tests.dal.test_timeline_composition import protected
    bridge,_,_,_,_=bridge_world
    transport=bridge.transport
    config=dict(base_url='http://127.0.0.1:8820',kid='pa',
        signing_key_file=protected(tmp_path/'pa-key',transport.key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,serialization.NoEncryption())),
        dal_public_keys={'dal':protected(tmp_path/'dal-public',transport.trusted_keys['dal'].public_bytes(
            serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))})
    path=tmp_path/'pa-config.json';protected(path,json.dumps(config).encode())
    actual=load_bridge(path,session_factory=bridge.sessions,keyring=bridge.keyring,token_ring=token_ring)
    assert actual.transport.base_url=='http://127.0.0.1:8820'
    path.chmod(0o644)
    with pytest.raises(ValueError):load_bridge(path,session_factory=bridge.sessions,keyring=bridge.keyring,token_ring=token_ring)
