"""PA identity checks and encrypted, replayable Timeline command delivery."""
import json
import threading
from types import SimpleNamespace
import httpx
from sqlalchemy import select
from personal_agent.storage.models import Device, DalTimelineCommand
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.timeline.requests import digest, valid_id


class TimelineBridge:
    def __init__(self, *, session_factory, keyring, transport, now=utc_now):
        self.sessions,self.keyring,self.transport,self.now=session_factory,keyring,transport,now
        self._delivery_lock=threading.Lock()
        self.push_sender = None
        self.projector=None

    def _identity(self,s,auth,scope):
        row=s.execute(select(Device.__table__).where(Device.device_id==auth.device_id)).mappings().one_or_none()
        if row is None or row['status']!='active':raise ValueError('DEVICE_INACTIVE')
        if scope not in auth.scopes or scope not in json.loads(row['scopes']):raise ValueError('SCOPE_REQUIRED')
        if auth.subject_id!='device:'+auth.device_id or auth.key_thumbprint!=row['device_key_thumbprint']:
            raise ValueError('DEVICE_IDENTITY_MISMATCH')

    def _seal(self,id,column,value):
        return self.keyring.encrypt(canonical_json(value).encode(),table='dal_timeline_commands',column=column,row_id=id)

    def _open(self,id,column,value):
        from personal_agent_core.crypto import CryptoError
        try:return json.loads(self.keyring.decrypt(value,table='dal_timeline_commands',column=column,row_id=id))
        except (CryptoError,ValueError,TypeError):raise ValueError('INPUT_INTEGRITY_FAILED') from None

    def _response(self,row):
        return dict(command_id=row.command_id,status=row.status,
            receipt=self._open(row.command_id,'sealed_receipt',row.sealed_receipt) if row.sealed_receipt else None)

    def queue_submit(self,auth,*,command_id,source_message_ref,body,_session=None):
        # Called by Host after binding to a persisted user message; no public
        # write endpoint accepts caller-supplied identity or message provenance.
        valid_id(command_id);valid_id(source_message_ref)
        if not isinstance(body,str) or not body.strip() or len(body.encode())>32768:raise ValueError('INVALID_ARGUMENT')
        payload=dict(schema_version='dal.timeline/1.0',command_id=command_id,source_message_ref=source_message_ref,command_kind='submit',payload={'text':body})
        fingerprint=digest(dict(device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,body=payload))
        def work(s):
            self._identity(s,auth,'dal.request')
            row=s.scalar(select(DalTimelineCommand).where(DalTimelineCommand.command_id==command_id))
            if row:
                if row.body_sha256!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
                return self._response(row)
            row=DalTimelineCommand(command_id=command_id,device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,
                body_sha256=fingerprint,sealed_body=self._seal(command_id,'sealed_body',payload),status='queued',attempts=0,created_at=self.now())
            s.add(row)
            return self._response(row)
        if _session is not None:return work(_session)
        with self.sessions() as s:return run_write_transaction(s,lambda:work(s))

    def queue_recovery(self,auth,*,command_id,source_message_ref,payload,_session):
        from personal_agent_dal.timeline.transport import RecoveryPayload
        payload=RecoveryPayload.model_validate(payload).model_dump()
        self._identity(_session,auth,'dal.request')
        body=dict(schema_version='dal.timeline/1.0',command_id=command_id,source_message_ref=source_message_ref,command_kind='recovery',payload=payload)
        fingerprint=digest(dict(device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,body=body))
        old=_session.get(DalTimelineCommand,command_id)
        if old:
            if old.body_sha256!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
            return self._response(old)
        row=DalTimelineCommand(command_id=command_id,device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,body_sha256=fingerprint,
            sealed_body=self._seal(command_id,'sealed_body',body),status='queued',attempts=0,created_at=self.now())
        _session.add(row)
        return self._response(row)

    def queue_decision(self,auth,*,command_id,source_message_ref,context,text,_session):
        from personal_agent.storage.models import DalContextBinding
        from personal_agent_dal.timeline.decisions import parse_decision,parse_project_choice
        kind=context['kind']
        scope='dal.request' if kind=='project_selection' else 'dal.'+kind+'.decide'
        self._identity(_session,auth,scope)
        row=_session.scalar(select(DalContextBinding).where(DalContextBinding.context_id==context['context_id']))
        payload=dict(schema_version='dal.timeline/1.0',command_id=command_id,source_message_ref=source_message_ref,
            command_kind='decision',payload=dict(kind=kind,decision_id=context['decision_id'],binding_digest=context['binding_digest'],text=text))
        fingerprint=digest(dict(device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,body=payload))
        old=_session.scalar(select(DalTimelineCommand).where(DalTimelineCommand.command_id==command_id))
        if old:
            if old.body_sha256!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
            return self._response(old)
        if row is None or row.device_id!=auth.device_id or row.consumed or row.expires_at<=self.now() or row.binding_digest!=context['binding_digest']:raise ValueError('STALE_BINDING')
        parsed=parse_project_choice(text,context['binding']['candidates']) if kind=='project_selection' else parse_decision(text)
        if parsed is None:raise ValueError('AMBIGUOUS_TARGET')
        outbox=DalTimelineCommand(command_id=command_id,device_id=auth.device_id,key_thumbprint=auth.key_thumbprint,
            body_sha256=fingerprint,sealed_body=self._seal(command_id,'sealed_body',payload),status='queued',attempts=0,created_at=self.now())
        _session.add(outbox);row.consumed=1
        return self._response(outbox)

    def command(self,auth,command_id):
        with self.sessions() as s:
            self._identity(s,auth,'dal.read')
            row=s.scalar(select(DalTimelineCommand).where(DalTimelineCommand.command_id==command_id))
            if row is None or row.device_id!=auth.device_id:raise ValueError('COMMAND_NOT_FOUND')
            return self._response(row)

    def query(self,auth,*,operation,body):
        if operation not in ('request_detail','request_list','artifact_read','roles_read','decision_status'):raise ValueError('INVALID_ARGUMENT')
        with self.sessions() as s:self._identity(s,auth,'dal.read')
        result=self.transport.call(operation=operation,request_id=new_id(),subject=auth.subject_id,body=body)
        with self.sessions() as s:self._identity(s,auth,'dal.read')
        return result

    def progress(self,auth,*,deadline_seconds=15):
        import time
        deadline=time.monotonic()+deadline_seconds
        items=[];cursor=None;snapshot=None;total=None;seen=set()
        while True:
            try:
                page=self.query(auth,operation='request_list',body=dict(view='ongoing',limit=50,cursor=cursor))
                if set(page)!={'schema_version','items','total','snapshot','as_of','complete','next_cursor'} or page['schema_version']!='dal.timeline/1.0':raise ValueError('DAL_RESPONSE_INVALID')
                if type(page['total']) is not int or page['total']<0 or type(page['complete']) is not bool or not isinstance(page['items'],list):raise ValueError('DAL_RESPONSE_INVALID')
                if snapshot is not None and (page['snapshot']!=snapshot or page['total']!=total):raise ValueError('DAL_RESPONSE_INVALID')
                snapshot,total=page['snapshot'],page['total']
                page_ids=[item['task_id'] for item in page['items']]
                if len(set(page_ids))!=len(page_ids) or seen.intersection(page_ids):raise ValueError('DAL_RESPONSE_INVALID')
                if len(items)+len(page_ids)>total or (page['complete'] and len(items)+len(page_ids)!=total):raise ValueError('DAL_RESPONSE_INVALID')
                if page['complete']!=(page['next_cursor'] is None) or (not page['complete'] and (not page_ids or page['next_cursor']==cursor)):raise ValueError('DAL_RESPONSE_INVALID')
                seen.update(page_ids);items.extend(page['items']);cursor=page['next_cursor']
                if page['complete']:return dict(items=items,total=total,complete=True,snapshot=snapshot,as_of=page['as_of'])
                if time.monotonic()>=deadline:break
            except (OSError,httpx.RequestError):break
        return dict(items=items,total=total,complete=False,snapshot=snapshot)

    def deliver_pending(self,*,stop_event=None):
        # Only one dispatcher per composed process. Cross-process duplicates
        # retain the same command ID and DAL's atomic idempotency contract.
        if not self._delivery_lock.acquire(blocking=False):return
        try:
            with self.sessions() as s:
                ids=list(s.scalars(select(DalTimelineCommand.command_id).where(
                    DalTimelineCommand.status.in_(['queued','delivery_unknown'])).order_by(
                    DalTimelineCommand.created_at,DalTimelineCommand.command_id).limit(50)))
            for id in ids:
                if stop_event is not None and stop_event.is_set():return
                def prepare(s):
                    row=s.scalar(select(DalTimelineCommand).where(DalTimelineCommand.command_id==id))
                    if row.status not in ('queued','delivery_unknown'):return None
                    body=self._open(id,'sealed_body',row.sealed_body)
                    scope=('dal.request' if body['payload']['kind']=='project_selection' else 'dal.'+body['payload']['kind']+'.decide') if body['command_kind']=='decision' else 'dal.request'
                    auth=SimpleNamespace(device_id=row.device_id,subject_id='device:'+row.device_id,
                        key_thumbprint=row.key_thumbprint,scopes=[scope])
                    try:self._identity(s,auth,scope)
                    except ValueError:
                        if row.attempts==0:row.status='cancelled'
                        return None
                    body=self._open(id,'sealed_body',row.sealed_body)
                    if digest(dict(device_id=row.device_id,key_thumbprint=row.key_thumbprint,body=body))!=row.body_sha256:
                        raise ValueError('INPUT_INTEGRITY_FAILED')
                    row.attempts+=1
                    row.status='delivery_unknown'  # Durable before any transport.
                    return auth.subject_id,body
                with self.sessions() as s:pending=run_write_transaction(s,lambda:prepare(s))
                if pending is None:continue
                try:result=self.transport.call(operation=pending[1]['command_kind'],request_id=id,subject=pending[0],body=pending[1])
                except (ValueError,OSError,httpx.RequestError):continue
                def accept(s):
                    row=s.scalar(select(DalTimelineCommand).where(DalTimelineCommand.command_id==id))
                    if row.status=='delivery_unknown':
                        row.sealed_receipt=self._seal(id,'sealed_receipt',result)
                        row.status=result['status']
                        if result['status']=='refused' and self.projector is not None and pending[1]['command_kind']=='decision':
                            from personal_agent.api import events
                            from personal_agent.storage.models import DalContextBinding
                            binding=s.scalar(select(DalContextBinding).where(DalContextBinding.device_id==row.device_id,
                                DalContextBinding.decision_id==result['decision_id']))
                            if binding is not None:
                                value=json.loads(self.keyring.decrypt(binding.sealed_context,table='dal_context_bindings',column='sealed_context',row_id=binding.context_id))
                                now=self.now();timeline=events.canonical_timeline_id(s,now=now)
                                session_id=self.projector.manager.system_event_session(s,conversation_id=timeline,now=now)
                                events.append_event(s,self.keyring,conversation_id=timeline,session_id=session_id,turn_id='dal-command:'+id,
                                    event_type='development_update',operation_id=None,now=now,content=dict(schema_version='dal.timeline/1.0',
                                    task_id=value['binding']['workflow_id'],kind='command.refused',
                                    text='这次决定未被接纳，待审版本或授权已变化。请打开最新待审消息重新确认。'))
                with self.sessions() as s:run_write_transaction(s,lambda:accept(s))
            if self.projector is not None:self.projector.sync(stop_event=stop_event)
            from personal_agent.api.dal_notifications import deliver
            deliver(self,self.push_sender,stop_event=stop_event)
        finally:self._delivery_lock.release()


def mount_routes(app,deps,authenticate):
    from fastapi import HTTPException, Request
    # Request annotations are resolved from module globals by FastAPI.
    def query(request,operation,body):
        if deps.dal_timeline is None:raise HTTPException(503,'DAL_UNAVAILABLE')
        with deps.session_factory() as s:auth=authenticate(request,s)
        try:return deps.dal_timeline.query(auth,operation=operation,body=body)
        except (OSError,httpx.RequestError):raise HTTPException(503,'DAL_UNAVAILABLE') from None
        except ValueError as exc:
            code=str(exc)
            if code not in ('DEVICE_INACTIVE','SCOPE_REQUIRED','DEVICE_IDENTITY_MISMATCH'):
                raise HTTPException(503,'DAL_UNAVAILABLE') from None
            raise HTTPException(403,code) from None
    @app.get('/v1/dal/tasks')
    def tasks(request:Request,filter:str='ongoing',limit:int=50,cursor:str|None=None):
        if filter not in ('ongoing','all','waiting') or not 1<=limit<=50:raise HTTPException(400,'INVALID_ARGUMENT')
        return query(request,'request_list',dict(view=filter,limit=limit,cursor=cursor))
    @app.get('/v1/dal/tasks/{task_id}')
    def detail(task_id:str,request:Request):
        try:valid_id(task_id)
        except ValueError:raise HTTPException(400,'INVALID_ARGUMENT') from None
        result=query(request,'request_detail',dict(request_id=task_id))
        from personal_agent.storage.models import DalDecisionState
        with deps.session_factory() as session:
            for decision in result.get('pending_decisions',[]):
                state=session.get(DalDecisionState,decision['decision_id'])
                decision['event_id']=state.event_id if state and state.status=='pending' else None
        return result

    @app.get('/v1/dal/artifacts/{artifact_id}')
    def artifact(artifact_id:str,request:Request,offset:int=0):
        try:valid_id(artifact_id)
        except ValueError:raise HTTPException(400,'INVALID_ARGUMENT') from None
        if offset<0:raise HTTPException(400,'INVALID_ARGUMENT')
        return query(request,'artifact_read',dict(artifact_id=artifact_id,offset=offset))
    @app.get('/v1/dal/events/{event_id}/context')
    def reply_context(event_id: str, request: Request):
        if deps.dal_timeline is None:
            raise HTTPException(503, 'DAL_UNAVAILABLE')
        with deps.session_factory() as session:
            auth = authenticate(request, session)
        from personal_agent.api.dal_contexts import mint
        try:
            return mint(deps.dal_timeline, auth, event_id)
        except ValueError:
            raise HTTPException(409, 'DAL_CONTEXT_UNAVAILABLE') from None

    @app.get('/v1/dal/notifications/{notification_id}')
    def notification(notification_id:str,request:Request):
        if deps.dal_timeline is None:raise HTTPException(503,'DAL_UNAVAILABLE')
        with deps.session_factory() as session:auth=authenticate(request,session)
        from personal_agent.api.dal_notifications import notification_context
        try:return notification_context(deps.dal_timeline,auth,notification_id)
        except ValueError:raise HTTPException(404,'NOTIFICATION_NOT_FOUND') from None

    @app.get('/v1/dal/roles')
    def roles(request:Request,workflow_id:str|None=None):
        if workflow_id is not None:
            try:valid_id(workflow_id)
            except ValueError:raise HTTPException(400,'INVALID_ARGUMENT') from None
        return query(request,'roles_read',{'workflow_id':workflow_id})
