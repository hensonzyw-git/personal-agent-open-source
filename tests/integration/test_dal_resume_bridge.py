"""The PA click surface has no caller-provided identity or authority fields."""
import pytest
from pydantic import ValidationError
from personal_agent.api.dal_resume import DecisionRequest


@pytest.mark.parametrize('extra', [{'approved': True}, {'device_id': 'forged'}, {'subject_id': 'device:x'}, {'key_thumbprint': 'fake'}])
def test_click_identity_cannot_be_supplied(extra):
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(dict(request_id='r', proposal_id='p', binding_sha256='a'*64,
                                            decision='approve_once', **extra))


def test_rejection_is_not_boolean_approval():
    with pytest.raises(ValidationError):
        DecisionRequest(request_id='r', proposal_id='p', binding_sha256='a'*64, decision=True)

import json
from types import SimpleNamespace
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, func, event
from personal_agent_core.timeutil import utc_now
from personal_agent.auth.device_keys import encode_device_public_key, device_key_thumbprint
from personal_agent.api.dal_resume import ResumeBridge, ProposalRequest
from personal_agent.storage import db as pa_db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent.storage.models import Device, DalResumeDecision, DalResumeDelivery
from tests.dal.test_p0_02_review_regressions import world
from tests.dal.test_p0_02_resume_authority import setup_resume, approve


@pytest.fixture
def bridge_world(world,tmp_path):
    a,p,key,claims=setup_resume(world)
    engine=create_database_engine(tmp_path/'pa.db')
    pa_db.upgrade(engine)
    public=encode_device_public_key(ec.generate_private_key(ec.SECP256R1()).public_key())
    thumb=device_key_thumbprint(public)
    with session_factory(engine)() as s,s.begin():
        s.add(Device(device_id='phone',display_name='Synthetic',public_key=public,
            device_key_thumbprint=thumb,status='active',scopes=json.dumps(['dal.resume.approve']),
            allowed_tools_version='v1',created_at=utc_now()))
    class Transport:
        sent=0
        lose_response=True
        def propose(self,body): return p
        def deliver(self,assertion):
            from personal_agent_dal.machine.resume_authority import import_decision
            self.sent+=1
            result=import_decision(world,assertion=assertion,keys={'resume':key.public_key()},issuer='pa-resume',audience='dal-resume')
            if self.lose_response: raise OSError('response lost after DAL commit')
            return result
    transport=Transport()
    bridge=ResumeBridge(session_factory=session_factory(engine),transport=transport,key=key,kid='resume')
    auth=SimpleNamespace(device_id='phone',subject_id='device:phone',key_thumbprint=thumb,scopes=['dal.resume.approve'])
    body=ProposalRequest(request_id='pa-proposal',feature_id='f',selection_id=p['binding']['selection_id'])
    bridge.proposal(auth,body)
    yield engine,bridge,auth,p,transport
    engine.dispose()


def click(p):
    return DecisionRequest(request_id='click',proposal_id=p['proposal_id'],binding_sha256=p['binding_sha256'],decision='approve_once')


def test_two_database_reopen_lost_response_and_later_device_revocation(bridge_world):
    engine,bridge,auth,p,transport=bridge_world
    result=bridge.decide(auth,click(p))
    assert result['delivery_status']=='queued' and transport.sent==0
    engine.dispose()
    bridge.deliver_pending()
    with session_factory(engine)() as s,s.begin():
        assert s.get(DalResumeDelivery,result['decision_id']).status=='queued'
        d=s.get(Device,'phone'); d.status='revoked'; d.revoked_at=utc_now()
    transport.lose_response=False
    bridge.deliver_pending()
    bridge.deliver_pending()
    with session_factory(engine)() as s:
        row=s.get(DalResumeDelivery,result['decision_id'])
        assert row.status=='accepted' and row.approval_id
        assert s.scalar(select(func.count()).select_from(DalResumeDecision))==1
    assert transport.sent==2


@pytest.mark.parametrize('problem',['scope','revoked','subject','fingerprint'])
def test_click_rechecks_device_identity(bridge_world,problem):
    engine,bridge,auth,p,transport=bridge_world
    if problem=='scope': auth.scopes=[]
    elif problem=='subject': auth.subject_id='device:forged'
    elif problem=='fingerprint': auth.key_thumbprint='x'*43
    else:
        with session_factory(engine)() as s,s.begin():
            d=s.get(Device,'phone');d.status='revoked';d.revoked_at=utc_now()
    with pytest.raises(ValueError): bridge.decide(auth,click(p))
    with session_factory(engine)() as s:
        assert s.scalar(select(func.count()).select_from(DalResumeDecision))==0
        assert s.scalar(select(func.count()).select_from(DalResumeDelivery))==0
    assert transport.sent==0


def test_click_outbox_failure_rolls_back_decision(bridge_world):
    engine,bridge,auth,p,transport=bridge_world
    def fail(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith('INSERT INTO dal_resume_deliveries'): raise RuntimeError('outbox failed')
    event.listen(engine,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError):bridge.decide(auth,click(p))
    finally:event.remove(engine,'before_cursor_execute',fail)
    with session_factory(engine)() as s:
        assert s.scalar(select(func.count()).select_from(DalResumeDecision))==0
    assert transport.sent==0

from fastapi.testclient import TestClient
from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.auth.tokens import issue_access_token
from tests.integration.test_agent_api import token_ring, keyring, IDENTIFIER_KEY, CURSOR_KEY


def test_both_service_roots_authenticate_and_deliver(bridge_world,world,token_ring,keyring):
    from personal_agent_dal.service.app import create_app
    from personal_agent.api.dal_client import ProposalBridgeClaims
    from personal_agent_dal.storage.machine_models import WorkflowProfileRevision, DispatchIntent
    engine,bridge,auth,p,transport=bridge_world
    dal=TestClient(create_app(world,service_key=b'synthetic-service-key',enrollment_secret=b'synthetic-enroll',
        resume_config={'issuer':'pa-resume','audience':'dal-resume','keys':{'resume':bridge.key.public_key()},'profiles':[]}))
    class ThroughDalRoutes:
        def propose(self,body):
            now=int(utc_now().timestamp())
            from personal_agent.api.dal_client import sign_decision
            assertion=sign_decision(dict(iss='pa-resume',aud='dal-resume',jti='bridge',iat=now,exp=now+60,
                operation='resume-proposal',**body),key=bridge.key,kid='resume')
            r=dal.post('/internal/resume-proposals',json={'assertion':assertion})
            assert r.status_code==200,r.text
            return r.json()
        def deliver(self,assertion):
            r=dal.post('/internal/human-decisions',json={'assertion':assertion})
            assert r.status_code==200,r.text
            return r.json()
    bridge.transport=ThroughDalRoutes()
    def no_executor(*a,**kw):raise AssertionError('executor must never be called')
    deps=AgentApiDeps(session_factory=session_factory(engine),token_ring=token_ring,keyring=keyring,
        identifier_key=IDENTIFIER_KEY,cursor_key=CURSOR_KEY,build_interpreter=no_executor,
        build_envelope=no_executor,build_dispatcher=no_executor,build_authorizer=no_executor,
        capabilities=lambda a:[],now=utc_now,dal_resume=bridge)
    pa=TestClient(build_app(deps))
    token=issue_access_token(token_ring,device_id='phone',device_key_thumbprint=auth.key_thumbprint,
        scopes=['dal.resume.approve'],allowed_tools_version='v1',now=utc_now())
    headers={'Authorization':'Bearer '+token}
    # Existing displayed proposal is replayed via authenticated service transport.
    r=pa.post('/v1/dal/resume-proposals',json={'request_id':'pa-proposal','feature_id':'f',
        'selection_id':p['binding']['selection_id']},headers=headers)
    assert r.status_code==200,r.text
    r=pa.post('/v1/dal/resume-decisions',json=click(p).model_dump(),headers=headers)
    assert r.status_code==200 and r.json()['delivery_status']=='queued',r.text
    bridge.deliver_pending()
    with session_factory(engine)() as s:
        assert s.get(DalResumeDelivery,r.json()['decision_id']).status=='accepted'
    with __import__('personal_agent_dal.storage.engine',fromlist=['session_factory']).session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(DispatchIntent))==0
    assert pa.post('/v1/dal/resume-decisions',json=click(p).model_dump()).status_code==401
    assert dal.post('/internal/human-decisions',json={'assertion':token}).status_code!=200


def test_explicit_scope_is_not_default():
    from personal_agent.device_cli import known_scopes
    from personal_agent.auth.enrollment import DEFAULT_DEVICE_SCOPES
    assert 'dal.resume.approve' in known_scopes()
    assert 'dal.resume.approve' not in DEFAULT_DEVICE_SCOPES


@pytest.mark.parametrize('url',['http://dal.invalid','https://user:pass@dal.invalid','https://dal.invalid/path','https://dal.invalid?host=evil','https://dal.invalid/#fragment'])
def test_fixed_transport_rejects_unpinned_destinations(url):
    from personal_agent.api.dal_client import FixedDalTransport
    with pytest.raises(ValueError):
        FixedDalTransport(base_url=url,key=ec.generate_private_key(ec.SECP256R1()),kid='resume',issuer='pa-resume',audience='dal-resume')


def test_pa_authority_downgrade_refused(bridge_world):
    engine,bridge,auth,p,transport=bridge_world
    bridge.decide(auth,click(p))
    with pytest.raises(RuntimeError,match='resume decisions exist'):
        pa_db.downgrade(engine,'0005_finance_safe_retry')


def test_rejected_click_never_delivers(bridge_world):
    engine,bridge,auth,p,transport=bridge_world
    body=click(p).model_copy(update={'decision':'reject'})
    result=bridge.decide(auth,body)
    assert result['delivery_status']=='rejected'
    bridge.deliver_pending()
    assert transport.sent==0
