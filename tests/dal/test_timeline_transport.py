"""The internal business bridge must not accept an operator token or forged scope."""
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.dal.test_timeline_requests import world
from personal_agent_dal.timeline.transport import TimelineEndpoint, envelope, mount_routes, verify_envelope


@pytest.fixture
def bridge(world):
    pa,dal=ec.generate_private_key(ec.SECP256R1()),ec.generate_private_key(ec.SECP256R1())
    endpoint=TimelineEndpoint(world[2],trusted_keys={'pa':pa.public_key()},signing_key=dal,kid='dal')
    app=FastAPI();mount_routes(app,endpoint)
    return TestClient(app),pa,dal


def request(pa,body=None,**changes):
    payload=body or dict(schema_version='dal.timeline/1.0',command_id='command',source_message_ref='message',command_kind='submit',payload={'text':'Synthetic only'})
    args=dict(operation='submit',request_id='command',subject='device:synthetic',scope='dal.request',body=payload)
    args.update(changes)
    return envelope(key=pa,kid='pa',issuer='pa-timeline',audience='dal-timeline',**args)


def test_signed_response_bound_to_request_and_deduplicated(bridge):
    client,pa,dal=bridge
    message=request(pa)
    response=client.post('/internal/development/commands',json=message)
    assert response.status_code==200
    claims,body=verify_envelope(response.json(),keys={'dal':dal.public_key()},issuer='dal-timeline',audience='pa-timeline')
    assert claims['request_id']=='command'
    assert body['status']=='accepted' and body['request']['status']=='accepted_not_started'
    repeated=client.post('/internal/development/commands',json=message)
    assert repeated.json()['body']==body


@pytest.mark.parametrize('change',[{'scope':'dal.read'},{'operation':'shell'},{'subject':'operator:root'}])
def test_wrong_scope_operation_or_subject_does_not_write(bridge,change):
    client,pa,_=bridge
    assert client.post('/internal/development/commands',json=request(pa,**change)).status_code in (400,403)


def test_tampered_body_signature_and_expired_assertion_refused(bridge):
    client,pa,_=bridge
    body=request(pa);body['body']['payload']['text']='injected'
    assert client.post('/internal/development/commands',json=body).status_code==403
    attacker=ec.generate_private_key(ec.SECP256R1())
    assert client.post('/internal/development/commands',json=request(attacker)).status_code==403
    old=envelope(key=pa,kid='pa',issuer='pa-timeline',audience='dal-timeline',operation='submit',
        request_id='command',subject='device:synthetic',scope='dal.request',body={},now=int(time.time())-1000)
    assert client.post('/internal/development/commands',json=old).status_code==403


def test_unknown_and_duplicate_json_fields_fail_closed(bridge):
    import json
    client,pa,_=bridge
    body=request(pa)
    body['body']['subject']='forged'
    assert client.post('/internal/development/commands',json=body).status_code==403
    message=request(pa)
    raw=json.dumps(message)[:-1]+',"body":{}}'
    assert client.post('/internal/development/commands',content=raw).status_code==400
    assert client.post('/internal/development/commands',content='x'*(1024*1024+1)).status_code==413


def test_unconfigured_endpoint_cannot_accept_an_unsigned_call():
    app=FastAPI();mount_routes(app,None)
    assert TestClient(app).post('/internal/development/commands',json={}).status_code==503
