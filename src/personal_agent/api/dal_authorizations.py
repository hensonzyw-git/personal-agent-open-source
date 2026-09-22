"""Device-only project approval. Card context never enters the Agent tool set."""
from datetime import datetime, timezone, timedelta
from typing import Literal
from sqlalchemy import select
from pydantic import StrictInt
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.machine.workflow_selection import Closed,Id,Digest
from personal_agent_dal.timeline.requests import digest,valid_id
from personal_agent_dal.timeline.authorization_contracts import Preview,aware_time,validate_proposal
from personal_agent.api.dal_client import sign_decision,verify_closed_assertion
from personal_agent.storage.models import DalAuthorizationContext as Context,DalTimelineCommand as Command


class ContextClaims(Closed):
    iss: Literal['pa-timeline']
    aud: Literal['pa-dal-authorization']
    domain: Literal['dal.authorization-context/1.0']
    jti: Id
    iat: StrictInt
    exp: StrictInt
    subject: Id
    key_thumbprint: str
    context_id: Id
    proposal_id: Id
    binding_digest: Digest


def _identity(bridge,s,auth,write=False):
    if not bridge.project_authorization_enabled:raise ValueError('CAPABILITY_UNAVAILABLE')
    bridge._identity(s,auth,'dal.read')
    if write:
        bridge._identity(s,auth,'dal.project.authorize')
        if getattr(auth,'client_wire_version',0)<7:raise ValueError('CAPABILITY_UNAVAILABLE')


def _check_proposal(p):
    validate_proposal(p)
    if (not isinstance(p,dict) or set(p)!={'proposal_id','revision','binding','binding_digest','scope','expires_at','status'}
        or digest(p['binding'])!=p['binding_digest'] or digest(p['scope'])!=p['binding']['scope_digest']
        or p['proposal_id']!=p['binding']['proposal_id']):raise ValueError('CONTEXT_INVALID')
    aware_time(p['expires_at'])
    return p


def read(bridge,auth,request_id,*,limit=50,cursor=None):
    with bridge.sessions() as s:_identity(bridge,s,auth)
    result=bridge.query(auth,operation='authorization_read',body=dict(request_id=request_id,limit=limit,cursor=cursor))
    if result.get('schema_version')!='dal.project-authorization/1.0' or result.get('request_id')!=request_id:raise ValueError('CONTEXT_INVALID')
    p=result.get('current_proposal')
    if p and p['status']=='pending':
        _check_proposal(p)
        def delivered(s):
            _identity(bridge,s,auth)
            old=s.scalar(select(Context).where(Context.device_id==auth.device_id,Context.key_thumbprint==auth.key_thumbprint,Context.proposal_id==p['proposal_id']))
            if old:
                if old.binding_digest!=p['binding_digest']:raise ValueError('CONTEXT_INVALID')
                return
            ident=new_id()
            s.add(Context(context_id=ident,device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,
                proposal_id=p['proposal_id'],binding_digest=p['binding_digest'],expires_at=aware_time(p['expires_at']),
                sealed_context=bridge.keyring.encrypt(canonical_json(p).encode(),table=Context.__tablename__,column='sealed_context',row_id=ident)))
        with bridge.sessions() as s:run_write_transaction(s,lambda:delivered(s))
    return result


def mint(bridge,auth,proposal_id,binding_digest):
    with bridge.sessions() as s:_identity(bridge,s,auth,True)
    current=bridge.query(auth,operation='authorization_status',body=dict(proposal_id=proposal_id))
    if current.get('valid') is not True:raise ValueError('STALE_BINDING')
    p=_check_proposal(current['proposal'])
    if p['binding_digest']!=binding_digest:raise ValueError('STALE_BINDING')
    def work(s):
        _identity(bridge,s,auth,True)
        row=s.scalar(select(Context).where(Context.device_id==auth.device_id,Context.key_thumbprint==auth.key_thumbprint,Context.proposal_id==proposal_id))
        if row is None or row.binding_digest!=binding_digest or row.expires_at<=bridge.now():raise ValueError('CONTEXT_INVALID')
        if row.command_id:return dict(proposal_id=proposal_id,token=None,command_id=row.command_id)
        now=int(bridge.now().timestamp());expiry=min(now+900,int(row.expires_at.timestamp()))
        row.token_jti=new_id();row.token_expires_at=datetime.fromtimestamp(expiry,timezone.utc)
        claims=dict(iss='pa-timeline',aud='pa-dal-authorization',domain='dal.authorization-context/1.0',jti=row.token_jti,
            iat=now,exp=expiry,subject=auth.subject_id,key_thumbprint=auth.key_thumbprint,context_id=row.context_id,
            proposal_id=proposal_id,binding_digest=binding_digest)
        return dict(proposal_id=proposal_id,token=sign_decision(claims,key=bridge.transport.key,kid=bridge.transport.kid),command_id=None)
    with bridge.sessions() as s:return run_write_transaction(s,lambda:work(s))


def queue(bridge,auth,*,kind,target,command_id,body):
    valid_id(command_id);valid_id(target)
    if kind not in ('authorization_preview','authorization_approve'):raise ValueError('INVALID_ARGUMENT')
    if not isinstance(body,dict) or len(canonical_json(body).encode())>32768:raise ValueError('INVALID_ARGUMENT')
    submission=digest(dict(device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,operation=kind,target=target,body=body))
    def work(s):
        _identity(bridge,s,auth,True)
        old=s.scalar(select(Command).where(Command.command_id==command_id))
        if old:
            if old.device_id!=auth.device_id or old.key_thumbprint!=auth.key_thumbprint or old.submission_sha256!=submission:raise ValueError('IDEMPOTENCY_CONFLICT')
            return bridge._response(old)
        context=None
        if kind=='authorization_preview':
            if 'request_id' in body:raise ValueError('INVALID_ARGUMENT')
            # Validate without normalizing the original timestamp on duplicate submissions.
            Preview.model_validate(dict(body,request_id=target))
            payload=dict(body,request_id=target)
        else:
            if set(body)!={'context_token','binding_digest'}:raise ValueError('INVALID_ARGUMENT')
            import jwt
            try:issued=jwt.decode(body['context_token'],options={'verify_signature':False})['iat']
            except (jwt.PyJWTError,KeyError,TypeError):raise ValueError('CONTEXT_INVALID') from None
            if type(issued)is not int or issued>int(bridge.now().timestamp()):raise ValueError('CONTEXT_INVALID')
            claims=verify_closed_assertion(body['context_token'],keys={bridge.transport.kid:bridge.transport.key.public_key()},
                schema=ContextClaims,issuer='pa-timeline',audience='pa-dal-authorization',now_epoch=issued)
            if (claims['subject']!=auth.subject_id or claims['key_thumbprint']!=auth.key_thumbprint
                or claims['proposal_id']!=target or claims['binding_digest']!=body['binding_digest']):raise ValueError('CONTEXT_INVALID')
            context=s.scalar(select(Context).where(Context.context_id==claims['context_id']))
            if (context is None or context.device_id!=auth.device_id or context.key_thumbprint!=auth.key_thumbprint
                or context.proposal_id!=target or context.binding_digest!=body['binding_digest']
                or context.token_jti!=claims['jti'] or context.command_id is not None):raise ValueError('CONTEXT_INVALID')
            if claims['exp']<=int(bridge.now().timestamp()) or context.expires_at<=bridge.now():raise ValueError('CONFIRMATION_EXPIRED_NOT_QUEUED')
            payload=dict(proposal_id=target,binding_digest=body['binding_digest'],source_action_ref=new_id(),
                context_id=context.context_id,context_jti=context.token_jti,key_thumbprint=auth.key_thumbprint,
                confirmed_at=bridge.now().isoformat(),confirmation_expires_at=context.token_expires_at.isoformat())
        envelope=dict(schema_version='dal.timeline/1.0',command_id=command_id,command_kind=kind,payload=payload)
        fingerprint=digest(dict(device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,body=envelope))
        row=Command(command_id=command_id,device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,
            body_sha256=fingerprint,submission_sha256=submission,sealed_body=bridge._seal(command_id,'sealed_body',envelope),
            status='queued',attempts=0,created_at=bridge.now())
        s.add(row);s.flush()
        if context:context.command_id=command_id
        from personal_agent.api import events
        from personal_agent.context.session_manager import SessionManager
        from personal_agent.context.config import default_context_config
        timeline=events.canonical_timeline_id(s,now=bridge.now())
        manager=bridge.projector.manager if bridge.projector else SessionManager(default_context_config())
        events.append_event(s,bridge.keyring,conversation_id=timeline,session_id=manager.system_event_session(s,conversation_id=timeline,now=bridge.now()),
            turn_id='dal-authorization:'+command_id,event_type='development_update',operation_id=None,now=bridge.now(),
            content=dict(schema_version='dal.timeline/1.0',command_id=command_id,kind='authorization.submitting',text='项目授权操作已提交，等待真实回执。'))
        return bridge._response(row)
    with bridge.sessions() as s:return run_write_transaction(s,lambda:work(s))


def mount_routes(app,deps,authenticate):
    from fastapi import Request,HTTPException
    import httpx
    # Resolve FastAPI annotations in the module rather than local function scope.
    globals()['Request']=Request
    def identity(request):
        if deps.dal_timeline is None:raise HTTPException(503,'DAL_UNAVAILABLE')
        with deps.session_factory() as s:return authenticate(request,s)
    def invoke(fn):
        try:return fn()
        except (ValueError,TypeError,KeyError) as exc:
            code=str(exc)
            if code=='CONFIRMATION_EXPIRED_NOT_QUEUED':raise HTTPException(400,code) from None
            allowed={'SCOPE_REQUIRED','DEVICE_INACTIVE','DEVICE_IDENTITY_MISMATCH','CAPABILITY_UNAVAILABLE','CONTEXT_INVALID',
                'STALE_BINDING','INVALID_ARGUMENT','IDEMPOTENCY_CONFLICT','CATALOG_CHANGED'}
            if code not in allowed:code='CONTEXT_INVALID'
            raise HTTPException(403 if code in ('SCOPE_REQUIRED','DEVICE_INACTIVE','DEVICE_IDENTITY_MISMATCH') else 503 if code=='CAPABILITY_UNAVAILABLE' else 409,code) from None
        except (OSError,httpx.RequestError):raise HTTPException(503,'DAL_UNAVAILABLE') from None
    @app.get('/v1/dal/tasks/{task_id}/authorization')
    def authorization(task_id:str,request:Request,limit:int=50,cursor:str|None=None):
        auth=identity(request)
        return invoke(lambda:read(deps.dal_timeline,auth,task_id,limit=limit,cursor=cursor))
    async def submit(request,target,kind):
        auth=identity(request)
        raw=await request.body()
        if len(raw)>32768:raise HTTPException(413,'INPUT_LIMIT')
        import json
        from personal_agent_dal.timeline.transport import _unique
        try:
            body=json.loads(raw,object_pairs_hook=_unique)
            command_id=body.pop('command_id')
            if request.headers.get('Idempotency-Key')!=command_id:raise ValueError
        except (ValueError,KeyError,AttributeError,TypeError):raise HTTPException(400,'INVALID_ARGUMENT') from None
        from fastapi.responses import JSONResponse
        return JSONResponse(invoke(lambda:queue(deps.dal_timeline,auth,kind=kind,target=target,command_id=command_id,body=body)),status_code=202)
    @app.post('/v1/dal/tasks/{task_id}/authorization/previews')
    async def preview(task_id:str,request:Request):return await submit(request,task_id,'authorization_preview')
    @app.post('/v1/dal/authorization-proposals/{proposal_id}/approve')
    async def approve(proposal_id:str,request:Request):return await submit(request,proposal_id,'authorization_approve')
    @app.post('/v1/dal/authorization-proposals/{proposal_id}/context')
    async def context(proposal_id:str,request:Request):
        auth=identity(request)
        raw=await request.body()
        if len(raw)>32768:raise HTTPException(413,'INPUT_LIMIT')
        import json
        from personal_agent_dal.timeline.transport import _unique
        try:
            body=json.loads(raw,object_pairs_hook=_unique)
            if set(body)!={'binding_digest'}:raise ValueError
        except (ValueError,TypeError):raise HTTPException(400,'INVALID_ARGUMENT') from None
        return invoke(lambda:mint(deps.dal_timeline,auth,proposal_id,body['binding_digest']))


def reconcile(bridge):
    """Observe existing effects even after device revocation or feature shutdown."""
    from personal_agent_dal.timeline.authorization_contracts import validate_receipt
    import httpx
    with bridge.sessions() as s:
        rows=list(s.scalars(select(Command).where(Command.status=='delivery_unknown').order_by(Command.created_at).limit(50)))
        pending=[]
        for row in rows:
            try:body=bridge._open(row.command_id,'sealed_body',row.sealed_body)
            except ValueError:continue
            if body.get('command_kind') in ('authorization_preview','authorization_approve'):
                pending.append((row.command_id,'device:'+row.device_id,body))
    for command_id,subject,body in pending:
        try:
            response=bridge.transport.call(operation='authorization_receipt',request_id=new_id(),subject=subject,body={'command_id':command_id})
            receipt=response['receipt']
            if receipt is not None:validate_receipt(receipt,kind=body['command_kind'],command_id=command_id,subject=subject,payload=body['payload'])
        except (ValueError,KeyError,TypeError,OSError,httpx.RequestError):continue
        def apply(s):
            row=s.scalar(select(Command).where(Command.command_id==command_id))
            if row is None or row.status!='delivery_unknown':return
            if receipt is not None:
                row.status=receipt['status'];row.sealed_receipt=bridge._seal(command_id,'sealed_receipt',receipt)
                row.delivery_error=None;row.next_attempt_at=None
                project_receipt(bridge,s,row,body,receipt)
            else:
                expiry=body['payload'].get('confirmation_expires_at') or body['payload'].get('grant_expires_at') or (row.created_at+__import__('datetime').timedelta(hours=24)).isoformat()
                if expiry is not None and aware_time(expiry)<=bridge.now():bridge._halt_delivery(s,row,'RETRY_EXHAUSTED')
        with bridge.sessions() as s:run_write_transaction(s,lambda:apply(s))


def project_receipt(bridge,s,row,body,receipt):
    """One durable human-facing outcome, shared by delivery and reconciliation."""
    from personal_agent.api import events
    from personal_agent.api.dal_notifications import enqueue
    from personal_agent.context.session_manager import SessionManager
    from personal_agent.context.config import default_context_config
    kind=body['command_kind']
    if kind=='authorization_preview' and receipt['status']=='accepted':return
    request_id=body['payload'].get('request_id')
    if kind=='authorization_approve':
        context=s.scalar(select(Context).where(Context.command_id==row.command_id))
        if context:
            import json
            proposal=json.loads(bridge.keyring.decrypt(context.sealed_context,table=Context.__tablename__,column='sealed_context',row_id=context.context_id))
            request_id=proposal['binding']['request_id']
    text=('项目授权已记录。后续执行仍需通过当前任务的审批与运行条件。' if receipt['status']=='accepted'
        else '这次项目授权未被接纳，请打开任务的授权卡片核对最新范围。')
    now=bridge.now();timeline=events.canonical_timeline_id(s,now=now)
    manager=bridge.projector.manager if bridge.projector else SessionManager(default_context_config())
    event_id=events.append_event(s,bridge.keyring,conversation_id=timeline,
        session_id=manager.system_event_session(s,conversation_id=timeline,now=now),
        turn_id='dal-authorization-result:'+row.command_id,event_type='development_update',operation_id=None,now=now,
        content=dict(schema_version='dal.timeline/1.0',command_id=row.command_id,task_id=request_id,
            kind='authorization.resolved',text=text,authorization={'schema_version':'dal.authorization-entry/1.0'}))
    enqueue(s,event_id=event_id,kind='authorization.resolved',now=now)
