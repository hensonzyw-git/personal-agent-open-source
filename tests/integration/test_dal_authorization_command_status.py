"""The new HTTP read must work without a Timeline event or a live context token."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
from sqlalchemy import select
from personal_agent.storage.models import DalTimelineCommand
from personal_agent.api.dal_timeline import mount_routes
from tests.integration.test_dal_timeline_bridge import bridge_world, queue
from tests.dal.test_timeline_requests import world


def client_for(bridge, auth):
    app=FastAPI()
    mount_routes(app,SimpleNamespace(session_factory=bridge.sessions,dal_timeline=bridge),lambda req,s:auth)
    return TestClient(app)


@pytest.mark.parametrize('status,attempts,error',[
    ('queued',0,None),('queued',0,'INPUT_INTEGRITY_FAILED'),
    ('cancelled',0,'IDENTITY_REVOKED'),('delivery_unknown',1,None),
    ('delivery_unknown',1,'RESPONSE_INVALID'),('delivery_unknown',5,'RETRY_EXHAUSTED'),
])
def test_read_persistent_delivery_state_without_event(bridge_world,status,attempts,error):
    bridge,auth,*_=bridge_world
    queue(bridge,auth)
    with bridge.sessions() as s,s.begin():
        row=s.scalar(select(DalTimelineCommand));row.status=status;row.attempts=attempts;row.delivery_error=error
    response=client_for(bridge,auth).get('/v1/dal/commands/command')
    assert response.status_code==200
    assert response.json()==dict(schema_version='dal.command-status/1.0',command_id='command',
        status=status,receipt=None,delivery_halted=error is not None,
        delivery_error=error,dispatch_attempted=attempts>0)


def test_accepted_read_does_not_depend_on_authorize_scope(bridge_world):
    bridge,auth,*_=bridge_world
    queue(bridge,auth);bridge.deliver_pending()
    r=client_for(bridge,auth).get('/v1/dal/commands/command')
    assert r.status_code==200 and r.json()['status']=='accepted'
    assert r.json()['receipt'] and not r.json()['delivery_halted']


def test_foreign_or_missing_command_has_same_not_found(bridge_world):
    bridge,auth,*_=bridge_world
    queue(bridge,auth)
    with bridge.sessions() as s,s.begin():
        # A different, existing device owns the row; do not weaken foreign keys.
        from personal_agent.storage.models import Device
        from personal_agent_core.timeutil import utc_now
        s.add(Device(device_id='other',display_name='Synthetic',public_key='synthetic2',
            device_key_thumbprint='other-thumb',status='active',scopes='["dal.read"]',
            allowed_tools_version='v1',created_at=utc_now()))
        s.flush();s.scalar(select(DalTimelineCommand)).device_id='other'
    client=client_for(bridge,auth)
    a=client.get('/v1/dal/commands/command');b=client.get('/v1/dal/commands/missing')
    assert a.status_code==b.status_code==404 and a.json()==b.json()


def test_unknown_persisted_error_is_not_exposed_or_guessed(bridge_world):
    bridge,auth,*_=bridge_world
    queue(bridge,auth)
    with bridge.sessions() as s,s.begin():s.scalar(select(DalTimelineCommand)).delivery_error='private-exception-content'
    r=client_for(bridge,auth).get('/v1/dal/commands/command')
    assert r.status_code==503 and 'private' not in r.text


def test_receipt_is_not_visible_to_replacement_key_under_same_device_id(bridge_world):
    bridge,auth,*_=bridge_world
    queue(bridge,auth)
    with bridge.sessions() as s,s.begin():
        s.scalar(select(DalTimelineCommand)).key_thumbprint='previous-device-key'
    assert client_for(bridge,auth).get('/v1/dal/commands/command').status_code==404
