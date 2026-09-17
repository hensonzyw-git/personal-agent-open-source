"""Closed, signed PA/DAL requests. This slice only persists and reads intake."""
import asyncio
import json
import time
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import StrictInt

from personal_agent.api.dal_client import sign_decision, verify_closed_assertion
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest
from personal_agent_dal.timeline.requests import TimelineRefusal, digest


class Claims(Closed):
    iss: Id
    aud: Id
    jti: Id
    iat: StrictInt
    exp: StrictInt
    domain: Literal['dal.timeline-command/1.0','dal.timeline-response/1.0']
    operation: Literal['submit','request_detail','request_list','events_read','events_ack','artifact_read','roles_read','decision']
    request_id: Id
    subject: Id
    scope: Literal['dal.read','dal.request','dal.events','dal.prd.decide','dal.delivery.decide']
    body_sha256: Digest
    request_body_sha256: Digest | None = None


class DecisionPayload(Closed):
    decision_id: Id
    binding_digest: Digest
    kind: Literal['prd','project_selection','delivery']
    text: str


class DecisionCommand(Closed):
    schema_version: Literal['dal.timeline/1.0']
    command_id: Id
    source_message_ref: Id
    command_kind: Literal['decision']
    payload: DecisionPayload


class EventRead(Closed):
    after_seq: StrictInt = 0
    limit: StrictInt = 100
    stream_id: Id | None = None


class EventAck(Closed):
    stream_id: Id
    through_seq: StrictInt
    tail_digest: Digest


class ArtifactRead(Closed):
    artifact_id: Id
    offset: StrictInt = 0


class RolesRead(Closed):
    pass


class SubmitPayload(Closed):
    text: str


class Submit(Closed):
    schema_version: Literal['dal.timeline/1.0']
    command_id: Id
    source_message_ref: Id
    command_kind: Literal['submit']
    payload: SubmitPayload


class Detail(Closed):
    request_id: Id


class ListRequests(Closed):
    view: Literal['ongoing','all','waiting']='ongoing'
    limit: StrictInt=50
    cursor: str | None=None


def envelope(*, key,kid,issuer,audience,operation,request_id,subject,scope,body,now=None,request_body_sha256=None):
    now=int(time.time()) if now is None else now
    claims=dict(iss=issuer,aud=audience,jti=request_id,iat=now,exp=now+120,
        domain='dal.timeline-response/1.0' if issuer=='dal-timeline' else 'dal.timeline-command/1.0',operation=operation,request_id=request_id,
        subject=subject,scope=scope,body_sha256=digest(body),request_body_sha256=request_body_sha256)
    return dict(assertion=sign_decision(claims,key=key,kid=kid),body=body)


def verify_envelope(value, *, keys,issuer,audience,now=None):
    if not isinstance(value,dict) or set(value)!={'assertion','body'} or not isinstance(value['body'],dict):
        raise TimelineRefusal('ASSERTION_INVALID')
    try:
        claims=verify_closed_assertion(value['assertion'],keys=keys,schema=Claims,
            issuer=issuer,audience=audience,now_epoch=int(time.time()) if now is None else now)
        expected_domain = 'dal.timeline-response/1.0' if issuer == 'dal-timeline' else 'dal.timeline-command/1.0'
        if claims['domain'] != expected_domain: raise ValueError
        if expected_domain == 'dal.timeline-command/1.0' and claims['request_body_sha256'] is not None: raise ValueError
        if expected_domain == 'dal.timeline-response/1.0' and claims['request_body_sha256'] is None: raise ValueError
        if claims['exp']-claims['iat']>120 or claims['jti']!=claims['request_id']:
            raise ValueError
        service_event = claims['operation'] in ('events_read','events_ack')
        identity_ok = claims['subject']=='service:pa-timeline' if service_event else (claims['subject'].startswith('device:') and len(claims['subject'])>7)
        if claims['body_sha256']!=digest(value['body']) or not identity_ok:
            raise ValueError
    except (ValueError,TypeError):
        raise TimelineRefusal('ASSERTION_INVALID') from None
    return claims,value['body']


class TimelineEndpoint:
    def __init__(self,requests,*,trusted_keys,signing_key,kid,kill_switch=lambda:False):
        self.requests,self.trusted_keys=requests,trusted_keys
        from personal_agent_dal.timeline.roles import RoleService
        self.roles=RoleService(requests,[])
        self.signing_key,self.kid,self.kill_switch=signing_key,kid,kill_switch

    def dispatch(self,value,*,command):
        claims,body=verify_envelope(value,keys=self.trusted_keys,issuer='pa-timeline',audience='dal-timeline')
        request_digest=digest(body)
        operation=claims['operation']
        if command!=(operation in ('submit','decision')):
            raise TimelineRefusal('SCOPE_REQUIRED')
        decision_body=DecisionCommand.model_validate(body) if operation=='decision' else None
        expected=('dal.request' if decision_body.payload.kind=='project_selection' else 'dal.'+decision_body.payload.kind+'.decide') if decision_body else 'dal.events' if operation in ('events_read','events_ack') else 'dal.request' if command else 'dal.read'
        if claims['scope']!=expected:
            raise TimelineRefusal('SCOPE_REQUIRED')
        if operation in ('events_read','events_ack'):
            from personal_agent_dal.timeline.events import EventStream
            stream=EventStream(self.requests)
            result=stream.read(**EventRead.model_validate(body).model_dump()) if operation=='events_read' else stream.ack(**EventAck.model_validate(body).model_dump())
        elif operation=='decision':
            if decision_body.command_id!=claims['request_id']:raise TimelineRefusal('ASSERTION_INVALID')
            from personal_agent_dal.timeline.decisions import DecisionService
            result=DecisionService(self.requests).process(command_id=decision_body.command_id,
                source_message_ref=decision_body.source_message_ref,subject=claims['subject'],
                decision_id=decision_body.payload.decision_id,binding_digest=decision_body.payload.binding_digest,text=decision_body.payload.text,expected_kind=decision_body.payload.kind)
        elif command:
            if self.kill_switch(): raise TimelineRefusal('DAL_UNAVAILABLE')
            body=Submit.model_validate(body)
            if body.command_id!=claims['request_id']:raise TimelineRefusal('ASSERTION_INVALID')
            result=self.requests.submit(command_id=body.command_id,subject=claims['subject'],
                source_message_ref=body.source_message_ref,body=body.payload.text)
            result=dict(schema_version='dal.timeline/1.0',command_id=body.command_id,
                receipt_id='receipt:'+digest(body.command_id),status='accepted',
                workflow_version=result['version'],request=result)
        elif operation=='artifact_read':
            from personal_agent_dal.timeline.artifacts import ArtifactService
            body=ArtifactRead.model_validate(body)
            result=ArtifactService(self.requests).read(body.artifact_id,offset=body.offset)
        elif operation=='roles_read':
            RolesRead.model_validate(body)
            result=self.roles.resolve()
        elif operation=='request_detail':
            body=Detail.model_validate(body)
            result=self.requests.detail(body.request_id)
        elif operation=='request_list':
            body=ListRequests.model_validate(body)
            result=self.requests.list_tasks(subject=claims['subject'],**body.model_dump())
        else:
            raise TimelineRefusal('INVALID_ARGUMENT')
        return envelope(key=self.signing_key,kid=self.kid,issuer='dal-timeline',audience='pa-timeline',
            operation=operation,request_id=claims['request_id'],subject=claims['subject'],scope=expected,body=result,request_body_sha256=request_digest)


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('DUPLICATE_FIELD')
        result[key]=value
    return result


def mount_routes(app,endpoint):
    async def handle(request,command):
        if endpoint is None:raise HTTPException(503,'DAL_UNAVAILABLE')
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>1024*1024:raise HTTPException(413,'BODY_TOO_LARGE')
        try:
            value=json.loads(body,object_pairs_hook=_unique)
        except (ValueError,UnicodeError,RecursionError):
            raise HTTPException(400,'INVALID_ARGUMENT') from None
        try:
            return await asyncio.to_thread(endpoint.dispatch,value,command=command)
        except TimelineRefusal as exc:
            code=str(exc)
            raise HTTPException(503 if code=='DAL_UNAVAILABLE' else 403,code) from None
        except (ValueError,TypeError):
            raise HTTPException(400,'INVALID_ARGUMENT') from None

    @app.post('/internal/development/commands')
    async def commands(request:Request):
        return await handle(request,True)

    @app.post('/internal/development/query')
    async def query(request:Request):
        return await handle(request,False)

    @app.post('/internal/development/events')
    async def events(request:Request):
        return await handle(request,False)
