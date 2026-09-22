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
    transport.test_requests = world[2]
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
    from datetime import timedelta
    restarted.now=lambda:bridge.now()+timedelta(seconds=6)
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


def test_events_project_once_into_current_session_and_ack_after_commit(bridge_world):
    from personal_agent.api.dal_timeline_events import TimelineProjector
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.storage.models import Conversation, ConversationEvent, ContextSession
    bridge,auth,state,dal,engine=bridge_world
    projector=TimelineProjector(bridge,SessionManager(default_context_config()))
    queue(bridge,auth);bridge.deliver_pending()
    projector.sync()
    projector.sync()
    with bridge.sessions() as s:
        events=list(s.scalars(select(ConversationEvent)))
        assert len(events)==1
        assert s.get(ContextSession,events[0].session_id).status=='open'
    assert projector.cursor()['received_seq']==projector.cursor()['acked_seq']==1


def test_projector_rejects_event_digest_conflict_without_advancing(bridge_world):
    from personal_agent.api.dal_timeline_events import TimelineProjector
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.storage.models import Conversation
    bridge,auth,_,dal,_=bridge_world
    queue(bridge,auth);bridge.deliver_pending()
    projector=TimelineProjector(bridge,SessionManager(default_context_config()))
    original=bridge.transport.call
    def corrupt(**kwargs):
        result=original(**kwargs)
        if kwargs['operation']=='events_read':result['items'][0]['body']['request_id']='forged'
        return result
    bridge.transport.call=corrupt
    with pytest.raises(ValueError,match='EVENT_STREAM_CONFLICT'):projector.sync()
    assert projector.cursor() is None


def test_all_progress_pages_are_aggregated_in_one_business_call(bridge_world):
    bridge,auth,_,dal,_=bridge_world
    for n in range(51):dal.submit(command_id=f'cmd{n}',source_message_ref=f'msg{n}',subject=auth.subject_id,body=f'Synthetic {n}')
    result=bridge.progress(auth)
    assert result['complete'] is True and result['total']==51 and len(result['items'])==51


def test_progress_host_projects_each_task_without_model_rewriting(engine,token_ring,keyring,bridge_world):
    from test_runtime_v2_api import client_for, _auth
    from test_adk_runtime import fc
    from personal_agent.auth.tokens import issue_access_token
    from personal_agent.storage.models import ConversationEvent
    bridge,auth,_,dal,_=bridge_world
    dal.submit(command_id='progress',source_message_ref='progress-source',subject=auth.subject_id,body='Synthetic task')
    def call(context,meta):return [fc('dal_query_progress','read-progress',arguments={},task=meta)]
    client,_,deps=client_for(engine,token_ring,keyring,[call])
    with deps.session_factory() as s,s.begin():s.get(Device,'dev-1').scopes=json.dumps(['dal.read'])
    deps.dal_timeline=TimelineBridge(session_factory=deps.session_factory,keyring=keyring,transport=bridge.transport)
    token=issue_access_token(token_ring,device_id='dev-1',device_key_thumbprint='THUMB',scopes=['dal.read'],allowed_tools_version='v1',now=NOW)
    result=client.post('/v1/chat/messages',headers={**_auth(token_ring),'Authorization':'Bearer '+token,'X-Client-Wire-Version':'4'},json={'conversation_id':'c1','text':'开发进度'})
    assert result.status_code==200,result.text
    assert '1 个' in result.json()['result_envelope']['text']
    with deps.session_factory() as s:assert len(list(s.scalars(select(ConversationEvent).where(ConversationEvent.event_type=='development_update'))))==1


def context_fixture(bridge, auth):
    from datetime import timedelta
    from personal_agent.api.dal_contexts import delivered
    from personal_agent_dal.timeline.requests import digest
    # FK points at a real projected event rather than an invented identifier.
    from personal_agent.api.dal_timeline_events import TimelineProjector
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.storage.models import DalEventInbox
    queue(bridge, auth)
    bridge.deliver_pending()
    TimelineProjector(bridge, SessionManager(default_context_config())).sync()
    with bridge.sessions() as s, s.begin():
        event_id = s.scalar(select(DalEventInbox)).timeline_event_id
        binding = dict(workflow_id='synthetic', version=1)
        decision = dict(decision_id='decision', kind='prd', binding=binding,
                        binding_digest=digest(binding), expires_at=(bridge.now()+timedelta(hours=1)).isoformat())
        from personal_agent.storage.models import DalDecisionState
        from personal_agent_dal.storage.timeline_models import DevelopmentDecisionRequest, DevelopmentWorkflow
        dal=bridge.transport.test_requests
        dal.now=bridge.now
        with dal.sessions() as ds,ds.begin():
            wf=ds.scalar(select(DevelopmentWorkflow))
            ds.add(DevelopmentDecisionRequest(decision_id='decision',workflow_id=wf.workflow_id,kind='prd',version=1,binding_digest=decision['binding_digest'],sealed_binding=dal._seal(DevelopmentDecisionRequest,'decision','sealed_binding',binding),status='pending',expires_at=bridge.now()+timedelta(hours=1)))
        s.add(DalDecisionState(decision_id='decision',request_id='synthetic',binding_digest=decision['binding_digest'],event_id=event_id,status='pending',source_seq=1))
        s.flush()
        from personal_agent.storage.models import ConversationEvent
        from personal_agent_core.manifest import canonical_json
        event=s.get(ConversationEvent,event_id)
        event.encrypted_content=bridge.keyring.encrypt(canonical_json({'decision':decision}).encode(),table='conversation_events',column='encrypted_content',row_id=event_id)
        delivered(s, bridge, auth, [SimpleNamespace(event_type='development_update', event_id=event_id, content={'decision':decision})])
    return event_id


def test_explicit_reply_context_cannot_be_retargeted_or_reused(bridge_world):
    from personal_agent.api.dal_contexts import mint, pending, select_reply
    from personal_agent.storage.models import DalContextBinding
    bridge, auth, *_ = bridge_world
    event_id = context_fixture(bridge, auth)
    reply = mint(bridge, auth, event_id)
    contexts = pending(bridge, auth)
    assert select_reply(bridge, auth, reply, contexts) == contexts
    with pytest.raises(ValueError):
        select_reply(bridge, auth, dict(reply, event_id='other'), contexts)
    other = SimpleNamespace(**{**vars(auth), 'key_thumbprint':'different'})
    with pytest.raises(ValueError):
        select_reply(bridge, other, reply, contexts)
    with bridge.sessions() as s, s.begin():
        s.scalar(select(DalContextBinding)).consumed = 1
    with pytest.raises(ValueError):
        select_reply(bridge, auth, reply, pending(bridge, auth))


def test_reply_target_survives_sealing_and_changes_idempotency(bridge_world):
    from personal_agent.api.request_payload import ChatRequestPayload, seal_chat_request, open_chat_request, validate_reply_context
    from personal_agent.api.operation_store import chat_request_fingerprint
    bridge, *_ = bridge_world
    context = {'event_id':'event', 'token':'synthetic-signed-context'}
    payload = ChatRequestPayload(conversation_id='timeline', text='通过', dal_reply_context=context)
    sealed = seal_chat_request(bridge.keyring, request_id='request', payload=payload)
    assert open_chat_request(bridge.keyring, request_id='request', envelope=sealed) == payload
    args = dict(conversation_id='timeline', text='通过')
    assert chat_request_fingerprint(**args) != chat_request_fingerprint(**args, dal_reply_context=context)
    assert chat_request_fingerprint(**args, dal_reply_context=context) != chat_request_fingerprint(**args, dal_reply_context=dict(context,event_id='other'))
    for value in [{'event_id':'event'}, dict(context, decision_id='injected'), dict(context,token=123)]:
        with pytest.raises(Exception):validate_reply_context(value)


@pytest.mark.parametrize('attack',['none','prose','two_calls','malformed','provider_error','wrong_decision'])
def test_isolated_adk_decision_candidate_never_authorizes_by_itself(engine,token_ring,keyring,bridge_world,attack):
    import httpx
    from personal_agent.auth.tokens import issue_access_token
    from personal_agent.runtime.witnessed_model import WitnessedLiteLlm
    from personal_agent.api.dal_contexts import mint
    from test_runtime_v2_api import client_for, _auth
    from test_witnessed_model import wire
    from test_adk_runtime import fc
    client,calls,deps=client_for(engine,token_ring,keyring,[])
    scopes=['dal.request','dal.read','dal.prd.decide']
    with deps.session_factory() as s,s.begin():
        device=s.get(Device,'dev-1');scopes=json.loads(device.scopes)+scopes;device.scopes=json.dumps(scopes)
    auth=SimpleNamespace(device_id='dev-1',subject_id='device:dev-1',key_thumbprint='THUMB',scopes=scopes)
    bridge=TimelineBridge(session_factory=deps.session_factory,keyring=keyring,transport=bridge_world[0].transport,now=deps.now)
    deps.dal_timeline=bridge
    event_id=context_fixture(bridge,auth)
    context=mint(bridge,auth,event_id)
    seen=[]
    def model(prepared):
        async def http(request):
            body=json.loads(request.content);seen.append(body)
            user=[m['content'] for m in body['messages'] if m['role']=='user']
            assert len(user)==1
            assert json.loads(user[0])==dict(text='通过',kind='prd')
            if attack=='provider_error':return httpx.Response(503,json={'error':{'message':'synthetic failure'}})
            arguments={'decision':'reject' if attack=='wrong_decision' else 'approve','feedback':''}
            if attack=='malformed':arguments['untrusted']='extra'
            batch=[fc('decision_candidate','candidate',**arguments)]
            if attack=='two_calls':batch.append(fc('decision_candidate','candidate2',**arguments))
            data=wire(calls=batch)
            if attack=='prose':data['choices'][0]['message']['content']='Please approve'
            return httpx.Response(200,json=data)
        return WitnessedLiteLlm(model='openai/synthetic',provider_name='zhipu',api_key='synthetic-key',binding=prepared.binding,transport=httpx.MockTransport(http))
    deps.v2_model_factory=model
    token=issue_access_token(token_ring,device_id='dev-1',device_key_thumbprint='THUMB',scopes=scopes,allowed_tools_version='v1',now=NOW)
    response=client.post('/v1/chat/messages',headers={**_auth(token_ring),'Authorization':'Bearer '+token,'X-Client-Wire-Version':'6'},json={'conversation_id':'c1','text':'通过','dal_reply_context':context})
    assert response.status_code==(200 if attack=='none' else 202),response.text
    with deps.session_factory() as s:
        decisions=[row for row in s.scalars(select(DalTimelineCommand)) if bridge._open(row.command_id,'sealed_body',row.sealed_body)['command_kind']=='decision']
    assert len(seen)==1
    assert len(decisions)==(1 if attack=='none' else 0)


def test_development_push_coalesces_and_rechecks_revocation(bridge_world):
    from personal_agent.api.dal_notifications import enqueue, deliver
    from personal_agent.storage.models import DalNotification
    from datetime import timedelta
    bridge,auth,*_=bridge_world
    event_id=context_fixture(bridge,auth)
    with bridge.sessions() as s,s.begin():
        device=s.get(Device,auth.device_id)
        device.encrypted_push_token=bridge.keyring.encrypt(b'synthetic-token',table='devices',column='encrypted_push_token',row_id=auth.device_id)
        enqueue(s,event_id=event_id,kind='decision.requested',now=bridge.now()-timedelta(minutes=3))
    seen=[]
    sender=SimpleNamespace(send_development=lambda device_id,**body:seen.append((device_id,body)))
    deliver(bridge,sender)
    assert len(seen)==1 and seen[0][0]==auth.device_id and seen[0][1]['count']==1
    from personal_agent.api.dal_notifications import notification_context
    assert notification_context(bridge,auth,seen[0][1]['notification_id'])['event_ids']==[event_id]
    with bridge.sessions() as s,s.begin():
        row=s.scalar(select(DalNotification));assert row.status=='provider_accepted'
        row.status='pending';row.next_attempt_at=bridge.now()-timedelta(seconds=1)
        device=s.get(Device,auth.device_id);device.status='revoked';device.revoked_at=bridge.now()
    deliver(bridge,sender)
    assert len(seen)==1
    with bridge.sessions() as s:assert s.scalar(select(DalNotification)).status=='undeliverable'


def test_restart_preserves_original_authority_and_never_adds_new_scopes(engine,token_ring,keyring):
    from personal_agent.api.runtime_v2 import resumable
    from personal_agent.api.request_payload import open_chat_request
    from personal_agent.runtime.run_repository import RunRepository
    from personal_agent.storage.models import Operation
    from personal_agent.auth.tokens import issue_access_token
    from test_runtime_v2_api import client_for, _auth, answer
    client,calls,deps=client_for(engine,token_ring,keyring,[answer()])
    with deps.session_factory() as s,s.begin():
        device=s.get(Device,'dev-1');scopes=json.loads(device.scopes)+['dal.read'];device.scopes=json.dumps(scopes)
    token=issue_access_token(token_ring,device_id='dev-1',device_key_thumbprint='THUMB',scopes=scopes,allowed_tools_version='v1',now=NOW)
    response=client.post('/v1/chat/messages',headers={**_auth(token_ring),'Authorization':'Bearer '+token,'X-Client-Wire-Version':'6'},json={'conversation_id':'c1','text':'你好'})
    assert response.status_code==200,response.text
    id=response.json()['operation_id'];repo=RunRepository(deps.session_factory,keyring)
    with deps.session_factory() as s,s.begin():
        op=s.get(Operation,id)
        payload=open_chat_request(keyring,request_id=op.request_id,envelope=op.api_request.encrypted_request_payload)
        assert payload.device_authority['client_wire_version']==6
        assert payload.device_authority['key_thumbprint']=='THUMB'
        # Emulate a recoverable run to test discovery without re-executing an already finished business operation.
        s.execute(repo.runs.update().where(repo.runs.c.operation_id==id).values(state='accepted',lease_until_ms=None))
        op.state='accepted'
        s.get(Device,'dev-1').scopes=json.dumps(scopes+['dal.request'])
    found=dict(resumable(deps))
    assert found[id].client_wire_version==6
    assert found[id].subject_id=='device:dev-1'
    assert 'dal.request' not in found[id].scopes
    with deps.session_factory() as s,s.begin():s.get(Device,'dev-1').device_key_thumbprint='rotated'
    assert id not in dict(resumable(deps))


def test_http_replay_cannot_elevate_sealed_turn_permissions():
    from personal_agent.api.app import AuthContext
    from personal_agent.api.runtime_v2 import _action_authority
    from personal_agent.api.request_payload import ChatRequestPayload
    auth=AuthContext('device',('dal.read','dal.prd.decide'),'v1',6,'device:device','thumb')
    payload=ChatRequestPayload('timeline','通过',device_authority=dict(subject_id='device:device',key_thumbprint='thumb',scopes=['dal.read'],client_wire_version=4))
    frozen=_action_authority(auth,payload)
    assert frozen.scopes==('dal.read',) and frozen.client_wire_version==4
    assert _action_authority(auth,ChatRequestPayload('timeline','通过')).scopes==()
    from dataclasses import replace
    with pytest.raises(Exception):_action_authority(replace(auth,key_thumbprint='rotated'),payload)


def test_decision_tombstone_prevents_historical_context_recreation(bridge_world):
    from personal_agent.api.dal_contexts import pending, mint, delivered
    from personal_agent.storage.models import DalDecisionState, DalContextBinding
    bridge,auth,*_=bridge_world
    event_id=context_fixture(bridge,auth)
    contexts=pending(bridge,auth)
    assert len(contexts)==1
    decision={k:v for k,v in contexts[0].items() if k not in ('event_id','context_id')}
    with bridge.sessions() as s,s.begin():
        s.get(DalDecisionState,'decision').status='superseded'
        s.delete(s.scalar(select(DalContextBinding)))
    with bridge.sessions() as s,s.begin():
        delivered(s,bridge,auth,[SimpleNamespace(event_type='development_update',event_id=event_id,content={'decision':decision})])
    assert pending(bridge,auth)==[]
    with pytest.raises(ValueError,match='DAL_CONTEXT_UNAVAILABLE'):mint(bridge,auth,event_id)


@pytest.mark.parametrize('failure',['invalid_response','network','revoked'])
def test_command_delivery_halts_visibly_without_claiming_remote_failure(bridge_world,failure):
    from datetime import timedelta
    from personal_agent.api.dal_timeline_events import TimelineProjector
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.storage.models import ConversationEvent
    from personal_agent.api.events import _entry
    bridge,auth,state,dal,engine=bridge_world
    bridge.projector=TimelineProjector(bridge,SessionManager(default_context_config()))
    clock=[utc_now()];bridge.now=lambda:clock[0]
    original=bridge.transport.call
    def call(**kwargs):
        if kwargs['operation']=='submit':
            original(**kwargs)  # The DAL may already have accepted the command.
            if failure=='invalid_response':raise ValueError('DAL_RESPONSE_INVALID')
            raise OSError('synthetic response loss')
        return original(**kwargs)
    bridge.transport.call=call
    queue(bridge,auth);bridge.deliver_pending()
    if failure=='revoked':
        with bridge.sessions() as s,s.begin():
            s.get(Device,'phone').status='revoked';s.get(Device,'phone').revoked_at=clock[0]
    if failure=='network':
        with bridge.sessions() as s:assert s.get(DalTimelineCommand,'command').attempts==1
        bridge.deliver_pending()
        with bridge.sessions() as s:assert s.get(DalTimelineCommand,'command').attempts==1
    for _ in range(10):
        clock[0]+=timedelta(minutes=10);bridge.deliver_pending()
    with bridge.sessions() as s:
        row=s.get(DalTimelineCommand,'command')
        assert row.status=='delivery_unknown' and row.sealed_receipt is None
        assert row.delivery_error=={'invalid_response':'RESPONSE_INVALID','network':'RETRY_EXHAUSTED','revoked':'IDENTITY_REVOKED'}[failure]
        assert row.attempts==(5 if failure=='network' else 1)
        notices=[_entry(bridge.keyring,e).content for e in s.scalars(select(ConversationEvent))
            if _entry(bridge.keyring,e).content.get('kind')=='command.delivery_halted']
        assert len(notices)==1 and notices[0]['command_id']=='command'
        assert '未知' in notices[0]['text']
    assert dal.list_tasks(subject=auth.subject_id)['total']==1
    restarted=TimelineBridge(session_factory=bridge.sessions,keyring=bridge.keyring,transport=bridge.transport,now=bridge.now)
    calls=state['calls'];restarted.deliver_pending()
    assert state['calls']==calls


def test_invalid_http_response_stops_automatic_command_replay(bridge_world):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    bridge,auth,_,_,_=bridge_world
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200);self.end_headers();self.wfile.write(b'{"unexpected":"synthetic"}')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        bridge.transport.base_url='http://127.0.0.1:'+str(server.server_port)
        del bridge.transport._post  # Exercise the actual socket client and response verifier.
        queue(bridge,auth);bridge.deliver_pending();bridge.deliver_pending()
        assert len(calls)==1
        with bridge.sessions() as s:
            row=s.get(DalTimelineCommand,'command')
            assert row.status=='delivery_unknown' and row.delivery_error=='RESPONSE_INVALID'
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_retry_migration_preserves_old_unknown_and_blocks_unsafe_downgrade(bridge_world):
    bridge,auth,state,_,engine=bridge_world
    queue(bridge,auth)
    db.downgrade(engine,'0021_dal_decision_projection')
    with engine.begin() as c:
        c.execute(text("UPDATE dal_timeline_commands SET status='delivery_unknown',attempts=9"))
        before=c.execute(text('SELECT sealed_body,body_sha256 FROM dal_timeline_commands')).one()
    db.upgrade(engine)
    with engine.connect() as c:
        assert c.execute(text('SELECT sealed_body,body_sha256 FROM dal_timeline_commands')).one()==before
    bridge.deliver_pending()
    assert state['calls']==0
    with bridge.sessions() as s:
        row=s.get(DalTimelineCommand,'command')
        assert row.attempts==9 and row.status=='delivery_unknown' and row.delivery_error=='RETRY_EXHAUSTED'
    with pytest.raises(RuntimeError,match='evidence must be preserved'):
        db.downgrade(engine,'0021_dal_decision_projection')


@pytest.mark.parametrize('case',['unique','multiple','empty','wrong_phase','model_text','foreign_source','mixed','chat'])
def test_phone_supplement_uses_existing_intake_or_clarifies(engine,token_ring,keyring,bridge_world,case):
    from test_runtime_v2_api import client_for, _auth, answer
    from test_adk_runtime import fc
    from personal_agent.auth.tokens import issue_access_token
    from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow
    dal=bridge_world[3]
    original=dal.submit(command_id='original',subject='device:dev-1',source_message_ref='original-source',body='Synthetic health acquisition')['request_id'] if case!='empty' else None
    if case=='multiple':dal.submit(command_id='second',subject='device:dev-1',source_message_ref='second-source',body='Synthetic other request')
    if case=='wrong_phase':
        with dal.sessions() as s,s.begin():s.get(DevelopmentWorkflow,original).phase='project_routing'
    def supplement(context,meta):
        calls=[fc('dal_answer_clarification','supplement',arguments={'text':'forged'} if case=='model_text' else {},task=meta,
            **({'write_source_refs':['foreign']} if case=='foreign_source' else {}))]
        if case=='mixed':calls.append(fc('dal_submit_request','new',arguments={},task=meta))
        return calls
    client,calls,deps=client_for(engine,token_ring,keyring,[answer('聊别的话题') if case=='chat' else supplement])
    with deps.session_factory() as s,s.begin():
        device=s.get(Device,'dev-1');scopes=json.loads(device.scopes)+['dal.request','dal.read'];device.scopes=json.dumps(scopes)
    deps.dal_timeline=TimelineBridge(session_factory=deps.session_factory,keyring=keyring,transport=bridge_world[0].transport)
    token=issue_access_token(token_ring,device_id='dev-1',device_key_thumbprint='THUMB',scopes=scopes,allowed_tools_version='v1',now=NOW)
    headers={**_auth(token_ring),'Authorization':'Bearer '+token,'X-Client-Wire-Version':'6'}
    text='聊别的话题' if case=='chat' else '本期只做数据获取和存储，月度 review 后置'
    response=client.post('/v1/chat/messages',headers=headers,json={'conversation_id':'c1','text':text})
    assert response.status_code==(202 if case in ('multiple','empty','wrong_phase') else 200),response.text
    with deps.session_factory() as s:
        rows=list(s.scalars(select(DalTimelineCommand)))
        assert len(rows)==(1 if case=='unique' else 0)
        if rows:
            body=deps.dal_timeline._open(rows[0].command_id,'sealed_body',rows[0].sealed_body)
            assert body['command_kind']=='recovery'
            assert body['payload']==dict(workflow_id=original,expected_version=1,action='clarification',text=text)
    if case=='multiple':
        visible=response.json()['result_envelope']['text']
        assert original not in visible and '任务ID' not in visible
        assert '点选对应任务' in visible
    if case=='unique':
        deps.dal_timeline.deliver_pending()
        assert dal.detail(original)['request_version']==2
        assert dal.detail(original)['text'].endswith(text)
    elif original:assert dal.detail(original)['request_version']==1
    assert dal.list_tasks(subject='device:dev-1')['total']==(0 if case=='empty' else 2 if case=='multiple' else 1)


@pytest.mark.parametrize('reason',['STALE_BINDING','INPUT_LIMIT'])
def test_rejected_supplement_is_visible_once_in_phone_timeline(bridge_world,reason):
    from personal_agent.storage.models import ConversationEvent
    from personal_agent.api.events import _entry
    bridge,auth,_,dal,_=bridge_world
    rid=dal.submit(command_id='intake',source_message_ref='intake-source',subject=auth.subject_id,
        body='x'*32760 if reason=='INPUT_LIMIT' else 'Synthetic request')['request_id']
    with bridge.sessions() as s,s.begin():
        bridge.queue_recovery(auth,command_id='supplement',source_message_ref='phone-source',
            payload=dict(workflow_id=rid,expected_version=99 if reason=='STALE_BINDING' else 1,
                         action='clarification',text='Synthetic additional input'),_session=s)
    bridge.deliver_pending();bridge.deliver_pending()
    assert bridge.command(auth,'supplement')['status']=='refused'
    with bridge.sessions() as s:
        notices=[_entry(bridge.keyring,e).content for e in s.scalars(select(ConversationEvent))]
    notices=[x for x in notices if x.get('kind')=='command.refused']
    assert len(notices)==1
    assert notices[0]['task_id']==rid and notices[0]['command_id']=='supplement'
    assert notices[0]['reason']==reason
    assert '未被接纳' in notices[0]['text']
    assert dal.detail(rid)['request_version']==1


@pytest.mark.parametrize('reason',[None,{},[],123,'untrusted body must not reach timeline'])
def test_invalid_signed_recovery_reason_halts_visibly(bridge_world,reason):
    from personal_agent_dal.timeline.transport import envelope
    from personal_agent_dal.timeline.requests import digest
    from personal_agent.storage.models import ConversationEvent
    from personal_agent.api.events import _entry
    bridge,auth,_,_,_=bridge_world
    key=ec.generate_private_key(ec.SECP256R1())
    bridge.transport.trusted_keys={'synthetic':key.public_key()}
    def post(path,payload):
        body=payload['body']
        result=dict(command_id=body['command_id'],workflow_id='synthetic-workflow',status='refused',reason=reason)
        return envelope(key=key,kid='synthetic',issuer='dal-timeline',audience='pa-timeline',
            operation='recovery',request_id=body['command_id'],subject=auth.subject_id,scope='dal.request',
            body=result,request_body_sha256=digest(body))
    bridge.transport._post=post
    with bridge.sessions() as s,s.begin():
        bridge.queue_recovery(auth,command_id='invalid-reply',source_message_ref='phone',_session=s,
            payload=dict(workflow_id='synthetic-workflow',expected_version=1,action='clarification',text='Synthetic input'))
    bridge.deliver_pending();bridge.deliver_pending()
    with bridge.sessions() as s:
        command=s.get(DalTimelineCommand,'invalid-reply')
        assert command.status=='delivery_unknown' and command.delivery_error=='RESPONSE_INVALID'
        notices=[_entry(bridge.keyring,e).content for e in s.scalars(select(ConversationEvent))]
    assert len(notices)==1 and notices[0]['kind']=='command.delivery_halted'
    assert 'untrusted body' not in str(notices)


def _clarification_event(bridge_world, questions):
    from personal_agent_dal.storage.timeline_models import DevelopmentRequest
    bridge,auth,_,dal,_=bridge_world
    queue(bridge,auth);bridge.deliver_pending()
    rid=dal.list_tasks(subject=auth.subject_id)['items'][0]['task_id']
    with dal.sessions() as s,s.begin():
        dal._append_event(s,s.get(DevelopmentRequest,rid),'workflow.clarification',
            dict(summary='Synthetic blocker',questions=questions,status='blocked',phase='clarify'))
    return bridge


def test_clarification_questions_project_once_and_enter_same_session_history(bridge_world):
    from personal_agent.api.dal_timeline_events import TimelineProjector
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.storage.models import ConversationEvent
    from personal_agent.api import events
    from personal_agent.runtime.dal_history import development_history
    bridge=_clarification_event(bridge_world,['Import or sync?','Which types?'])
    projector=TimelineProjector(bridge,SessionManager(default_context_config()))
    projector.sync();projector.sync()
    with bridge.sessions() as s,s.begin():
        rows=list(s.scalars(select(ConversationEvent).order_by(ConversationEvent.timeline_sequence)))
        assert len(rows)==2
        content=events._entry(bridge.keyring,rows[-1]).content
        assert '1. Import or sync?\n2. Which types?' in content['text']
        anchor=events.append_event(s,bridge.keyring,conversation_id=rows[-1].conversation_id,
            session_id=rows[-1].session_id,turn_id='question',event_type='user_message',
            content={'text':'What directory?'},operation_id=None,now=bridge.now())
    owner=SimpleNamespace(deps=SimpleNamespace(dal_timeline=bridge,session_factory=bridge.sessions,keyring=bridge.keyring),
        auth=bridge_world[1],anchor=SimpleNamespace(event_id=anchor))
    history=development_history(owner)
    assert len(history)==2 and 'Import or sync?' in history[-1]
    owner.auth=SimpleNamespace(scopes=[])
    assert development_history(owner)==[]
    assert projector.cursor()['received_seq']==2


@pytest.mark.parametrize('questions',[None,[],{},'Question?',[None],[''],['valid',3]])
def test_invalid_clarification_questions_do_not_advance_or_publish(bridge_world,questions):
    from personal_agent.api.dal_timeline_events import TimelineProjector
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.storage.models import ConversationEvent
    bridge=_clarification_event(bridge_world,questions)
    projector=TimelineProjector(bridge,SessionManager(default_context_config()))
    with pytest.raises(ValueError,match='EVENT_STREAM_CONFLICT'):projector.sync()
    assert projector.cursor()['received_seq']==1
    with bridge.sessions() as s:assert len(list(s.scalars(select(ConversationEvent))))==1


def test_historical_question_repair_is_append_only_bound_and_idempotent(bridge_world,monkeypatch):
    import personal_agent.api.dal_timeline_events as module
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.storage.models import ConversationEvent
    from personal_agent.api import events
    bridge=_clarification_event(bridge_world,['Import or sync?'])
    projector=module.TimelineProjector(bridge,SessionManager(default_context_config()))
    with monkeypatch.context() as patch:
        patch.setattr(module,'clarification_text',lambda body:body['summary'])
        projector.sync()
    cursor=projector.cursor()
    item=projector._call('events_read',{'after_seq':1,'limit':1})['items'][0]
    repaired=projector.repair_clarification(item)
    assert projector.repair_clarification(item)==repaired
    assert projector.cursor()==cursor
    with bridge.sessions() as s:
        rows=list(s.scalars(select(ConversationEvent).order_by(ConversationEvent.timeline_sequence)))
        assert len(rows)==3
        assert events._entry(bridge.keyring,rows[1]).content['text']=='Synthetic blocker'
        assert 'Import or sync?' in events._entry(bridge.keyring,rows[2]).content['text']
    item['body']['questions']=['forged']
    with pytest.raises(ValueError,match='EVENT_STREAM_CONFLICT'):projector.repair_clarification(item)


def test_development_history_excludes_future_other_session_and_revoked_device(bridge_world):
    from personal_agent.api.dal_timeline_events import TimelineProjector
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.storage.models import ConversationEvent,ContextSession
    from personal_agent.api import events
    from personal_agent.runtime.dal_history import development_history
    bridge=_clarification_event(bridge_world,['Visible question?'])
    projector=TimelineProjector(bridge,SessionManager(default_context_config()));projector.sync()
    with bridge.sessions() as s,s.begin():
        row=s.scalar(select(ConversationEvent).order_by(ConversationEvent.timeline_sequence.desc()))
        anchor=events.append_event(s,bridge.keyring,conversation_id=row.conversation_id,session_id=row.session_id,
            turn_id='question',event_type='user_message',content={'text':'Why?'},operation_id=None,now=bridge.now())
        events.append_event(s,bridge.keyring,conversation_id=row.conversation_id,session_id=row.session_id,
            turn_id='future',event_type='development_update',content={'text':'Future secret'},operation_id=None,now=bridge.now())
        # Move the previously visible question into another existing closed session.
        session=s.get(ContextSession,row.session_id)
        session.status='closed'
        session.closed_at=bridge.now()
    with bridge.sessions() as s,s.begin():
        sid=projector.manager.system_event_session(s,conversation_id=row.conversation_id,now=bridge.now())
        other=events.append_event(s,bridge.keyring,conversation_id=row.conversation_id,session_id=sid,
            turn_id='other',event_type='user_message',content={'text':'New topic'},operation_id=None,now=bridge.now())
    owner=SimpleNamespace(deps=SimpleNamespace(dal_timeline=bridge,session_factory=bridge.sessions,keyring=bridge.keyring),
        auth=bridge_world[1],anchor=SimpleNamespace(event_id=anchor))
    assert 'Future secret' not in ''.join(development_history(owner))
    owner.anchor.event_id=other
    assert development_history(owner)==[]
    with bridge.sessions() as s,s.begin():
        device=s.get(Device,'phone');device.status='revoked';device.revoked_at=bridge.now()
    with pytest.raises(ValueError,match='DEVICE_INACTIVE'):development_history(owner)


@pytest.mark.parametrize('tool',['dal_submit_request','dal_answer_clarification','dal_query_progress'])
def test_blocker_explanation_does_not_admit_dal_actions(engine,token_ring,keyring,bridge_world,tool):
    from test_runtime_v2_api import client_for, _auth
    from test_adk_runtime import fc
    from personal_agent.auth.tokens import issue_access_token
    from personal_agent.storage.models import ConversationEvent
    client,calls,deps=client_for(engine,token_ring,keyring,[lambda context,meta:[fc(tool,'misroute',arguments={},task=meta)]])
    with deps.session_factory() as s,s.begin():
        device=s.get(Device,'dev-1');scopes=json.loads(device.scopes)+['dal.request','dal.read'];device.scopes=json.dumps(scopes)
    deps.dal_timeline=TimelineBridge(session_factory=deps.session_factory,keyring=keyring,transport=bridge_world[0].transport)
    token=issue_access_token(token_ring,device_id='dev-1',device_key_thumbprint='THUMB',scopes=scopes,allowed_tools_version='v1',now=NOW)
    response=client.post('/v1/chat/messages',headers={**_auth(token_ring),'Authorization':'Bearer '+token,'X-Client-Wire-Version':'6'},
        json={'conversation_id':'c1','text':'source应该是apple health kit的数据，你检查了什么目标directory'})
    assert response.status_code==200,response.text
    assert calls and all(not t['function']['name'].startswith('dal_') for t in calls[0]['tools'])
    with deps.session_factory() as s:
        assert list(s.scalars(select(DalTimelineCommand)))==[]
        assert list(s.scalars(select(ConversationEvent).where(ConversationEvent.event_type=='development_update')))==[]


def test_phone_side_invalidation_on_remote_attachment_amend(bridge_world):
    """workflow.authorization_granted invalidation refs retire the phone card."""
    import hashlib
    from types import SimpleNamespace
    from personal_agent.api.dal_timeline_events import TimelineProjector
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    from personal_agent.api.dal_contexts import delivered, pending
    from personal_agent.api import events
    from personal_agent.storage.models import ConversationEvent, DalDecisionState, DalContextBinding
    from tests.dal.test_authorization_unbounded import remoteless_case
    from tests.dal.test_phone_authorization import preview, approve
    from personal_agent_dal.timeline.authorization_contracts import ProjectTemplate
    from personal_agent_dal.timeline.projects import catalog
    from personal_agent_dal.timeline.decisions import DecisionService
    from personal_agent_dal.timeline.requests import digest
    from personal_agent_dal.storage.timeline_models import (DevelopmentProjectTemplate as Template,
        DevelopmentWorkflow as Workflow, DevelopmentArtifact as Artifact)
    bridge,auth,_,dal,_=bridge_world
    r,service,rid,payload=remoteless_case((None,None,dal))
    old=approve(service,preview(service,payload)['proposal'])
    with r.sessions() as s,s.begin():
        wf=s.get(Workflow,rid);wf.status='active';wf.blocker_reason=None
        candidates=catalog(r,s,rid);body=dict(kind='project_route',text='Synthetic route',candidates=candidates)
        s.add(Artifact(artifact_id='route',workflow_id=rid,kind='project_route',revision=1,
            body_sha256=hashlib.sha256(body['text'].encode()).hexdigest(),sealed_body=r._seal(Artifact,'route','sealed_body',body),
            source_step_id='step',source_receipt_digest=digest(body)))
    decision=DecisionService(r).propose(rid,'route',kind='project_selection',candidates=candidates)
    projector=TimelineProjector(bridge,SessionManager(default_context_config()))
    projector.sync()
    with bridge.sessions() as s,s.begin():
        state=s.get(DalDecisionState,decision['decision_id'])
        assert state is not None and state.status=='pending'
        row=s.get(ConversationEvent,state.event_id)
        entries=[SimpleNamespace(event_type='development_update',event_id=state.event_id,
            content=events._entry(bridge.keyring,row).content)]
        delivered(s,bridge,auth,entries)
    assert [c['decision_id'] for c in pending(bridge,auth)]==[decision['decision_id']]
    with r.sessions() as s:
        trow=s.scalar(select(Template).where(Template.active==1))
        value=r._open(Template,trow.template_id,'sealed_template',trow.sealed_template)
    value.update(revision=3,remote_repository='synthetic/new-repo')
    service.register_template(ProjectTemplate.model_validate(value),actor='operator',observed_at=r.now(),
        expires_at=None,evidence_digest='f'*64)
    state=service.read(rid,subject='device:synthetic')
    payload.update(operation='amend',requested_actions=['read','write'],template_revision=3,
        template_digest=state['candidates'][0]['template_digest'],expected=state['expected'],
        expected_grant=dict(id=old['grant_id'],version=old['grant_version'],digest=old['grant_digest']))
    updated=approve(service,preview(service,payload,'attach-preview')['proposal'],'attach-approve')
    assert updated['status']=='accepted'
    projector.sync()
    with bridge.sessions() as s:
        assert s.get(DalDecisionState,decision['decision_id']).status=='superseded'
        binding=s.scalar(select(DalContextBinding).where(DalContextBinding.decision_id==decision['decision_id']))
        assert binding is not None and binding.consumed==1
    assert pending(bridge,auth)==[]
