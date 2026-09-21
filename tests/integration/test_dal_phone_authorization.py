"""Actual PA + DAL databases and signed envelopes, including lost responses."""
import json
from datetime import timedelta
from sqlalchemy import select
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
from personal_agent.storage.models import Device, DalTimelineCommand
from personal_agent_dal.storage.timeline_models import DevelopmentProjectAuthorization
from personal_agent.api.dal_timeline import mount_routes
from tests.integration.test_dal_timeline_bridge import bridge_world
from tests.dal.test_timeline_requests import world
from tests.dal.test_phone_authorization import setup_case


def case(bridge_world):
    bridge,auth,state,r,engine=bridge_world
    bridge.project_authorization_enabled=True
    auth.client_wire_version=7
    auth.scopes.append('dal.project.authorize')
    with bridge.sessions() as s,s.begin():s.get(Device,auth.device_id).scopes=json.dumps(auth.scopes)
    _,service,rid,payload=setup_case((None,None,r))
    # Use the authorized synthetic subject of the template in this integration fixture.
    from personal_agent_dal.timeline.authorization_contracts import ProjectTemplate
    from personal_agent_dal.storage.timeline_models import DevelopmentProjectTemplate
    with r.sessions() as s:
        row=s.scalar(select(DevelopmentProjectTemplate))
        value=r._open(DevelopmentProjectTemplate,row.template_id,'sealed_template',row.sealed_template)
    value.update(revision=2,allow_subjects=[auth.subject_id])
    service.register_template(ProjectTemplate(**value),actor='operator',observed_at=r.now(),expires_at=r.now()+timedelta(hours=1),evidence_digest='e'*64)
    payload.update(template_revision=2,template_digest=service.read(rid,subject=auth.subject_id)['candidates'][0]['template_digest'])
    payload.pop('request_id')
    app=FastAPI();mount_routes(app,SimpleNamespace(session_factory=bridge.sessions,dal_timeline=bridge),lambda req,s:auth)
    return bridge,auth,state,r,rid,payload,TestClient(app)


def post(client,path,command_id,body):
    return client.post(path,json=dict(command_id=command_id,**body),headers={'Idempotency-Key':command_id})


def prepared(case):
    bridge,auth,state,r,rid,payload,client=case
    res=post(client,f'/v1/dal/tasks/{rid}/authorization/previews','preview-phone',payload)
    assert res.status_code==202,res.text
    bridge.deliver_pending()
    status=client.get('/v1/dal/commands/preview-phone').json()
    assert status['status']=='accepted',status
    p=client.get(f'/v1/dal/tasks/{rid}/authorization').json()['current_proposal']
    token=client.post(f"/v1/dal/authorization-proposals/{p['proposal_id']}/context",json={'binding_digest':p['binding_digest']})
    assert token.status_code==200,token.text
    return p,dict(context_token=token.json()['token'],binding_digest=p['binding_digest'])


def test_phone_explicit_context_and_readback(bridge_world):
    c=case(bridge_world);bridge,auth,state,r,rid,payload,client=c
    p,body=prepared(c)
    with r.sessions() as s:assert s.scalar(select(DevelopmentProjectAuthorization)) is None
    path=f"/v1/dal/authorization-proposals/{p['proposal_id']}/approve"
    assert post(client,path,'approve-phone',body).status_code==202
    bridge.deliver_pending()
    result=client.get('/v1/dal/commands/approve-phone').json()
    assert result['status']=='accepted' and result['receipt']['authorization_applied'] is True
    assert post(client,path,'approve-phone',body).status_code==202
    with r.sessions() as s:assert len(list(s.scalars(select(DevelopmentProjectAuthorization))))==1


def test_lost_reply_then_revocation_still_reconciles_without_regrant(bridge_world):
    c=case(bridge_world);bridge,auth,state,r,rid,payload,client=c;p,body=prepared(c)
    post(client,f"/v1/dal/authorization-proposals/{p['proposal_id']}/approve",'approve-phone',body)
    state['lose']=True;bridge.deliver_pending()
    with bridge.sessions() as s,s.begin():
        d=s.get(Device,auth.device_id);d.status='revoked';d.revoked_at=bridge.now()
    state['lose']=False;bridge.deliver_pending()
    with bridge.sessions() as s:assert s.get(DalTimelineCommand,'approve-phone').status=='accepted'
    with r.sessions() as s:assert len(list(s.scalars(select(DevelopmentProjectAuthorization))))==1
    assert client.get('/v1/dal/commands/approve-phone').status_code==403


def test_read_only_scope_cannot_preview_or_approve(bridge_world):
    c=case(bridge_world);bridge,auth,state,r,rid,payload,client=c
    auth.scopes.remove('dal.project.authorize')
    assert post(client,f'/v1/dal/tasks/{rid}/authorization/previews','forbidden',payload).status_code==403
    with bridge.sessions() as s:assert s.get(DalTimelineCommand,'forbidden') is None


def test_context_cannot_be_consumed_under_second_command(bridge_world):
    c=case(bridge_world);bridge,auth,state,r,rid,payload,client=c;p,body=prepared(c)
    path=f"/v1/dal/authorization-proposals/{p['proposal_id']}/approve"
    assert post(client,path,'approve-phone',body).status_code==202
    assert post(client,path,'approve-second',body).status_code==409


def test_expired_unqueued_confirmation_is_definitive_and_replay_safe(bridge_world):
    c=case(bridge_world);bridge,auth,state,r,rid,payload,client=c;p,body=prepared(c)
    before=bridge.now();bridge.now=lambda:before+timedelta(minutes=16)
    path=f"/v1/dal/authorization-proposals/{p['proposal_id']}/approve"
    response=post(client,path,'expired-before-queue',body)
    assert response.status_code==400 and response.json()['detail']=='CONFIRMATION_EXPIRED_NOT_QUEUED'
    with bridge.sessions() as s:assert s.get(DalTimelineCommand,'expired-before-queue') is None
    with r.sessions() as s:assert s.scalar(select(DevelopmentProjectAuthorization)) is None


def test_unknown_after_deadline_only_reads_receipt_even_when_peer_unavailable(bridge_world):
    c=case(bridge_world);bridge,auth,state,r,rid,payload,client=c;p,body=prepared(c)
    post(client,f"/v1/dal/authorization-proposals/{p['proposal_id']}/approve",'expire-unknown',body)
    original=bridge.transport.call
    writes=[]
    def unavailable(**kwargs):
        if kwargs['operation']=='authorization_approve':writes.append(kwargs)
        raise OSError('synthetic unavailable')
    bridge.transport.call=unavailable
    bridge.deliver_pending();assert len(writes)==1
    before=bridge.now();bridge.now=lambda:before+timedelta(minutes=16)
    bridge.deliver_pending();assert len(writes)==1
    with bridge.sessions() as s:
        row=s.get(DalTimelineCommand,'expire-unknown')
        assert row.status=='delivery_unknown' and row.delivery_error=='RETRY_EXHAUSTED'
    bridge.transport.call=original


def test_context_migration_cannot_discard_delivered_approval(bridge_world):
    import pytest
    from personal_agent.storage import db
    c=case(bridge_world);bridge,*_=c;prepared(c)
    with pytest.raises(RuntimeError,match='Authorization history exists'):
        db.downgrade(bridge.sessions.kw['bind'],'0022_dal_command_retry')
