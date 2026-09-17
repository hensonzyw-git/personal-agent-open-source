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

    def command(self,auth,command_id):
        with self.sessions() as s:
            self._identity(s,auth,'dal.read')
            row=s.scalar(select(DalTimelineCommand).where(DalTimelineCommand.command_id==command_id))
            if row is None or row.device_id!=auth.device_id:raise ValueError('COMMAND_NOT_FOUND')
            return self._response(row)

    def query(self,auth,*,operation,body):
        if operation not in ('request_detail','request_list'):raise ValueError('INVALID_ARGUMENT')
        with self.sessions() as s:self._identity(s,auth,'dal.read')
        result=self.transport.call(operation=operation,request_id=new_id(),subject=auth.subject_id,body=body)
        with self.sessions() as s:self._identity(s,auth,'dal.read')
        return result

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
                    auth=SimpleNamespace(device_id=row.device_id,subject_id='device:'+row.device_id,
                        key_thumbprint=row.key_thumbprint,scopes=['dal.request'])
                    try:self._identity(s,auth,'dal.request')
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
                try:result=self.transport.call(operation='submit',request_id=id,subject=pending[0],body=pending[1])
                except (ValueError,OSError,httpx.RequestError):continue
                def accept(s):
                    row=s.scalar(select(DalTimelineCommand).where(DalTimelineCommand.command_id==id))
                    if row.status=='delivery_unknown':
                        row.sealed_receipt=self._seal(id,'sealed_receipt',result)
                        row.status='accepted'
                with self.sessions() as s:run_write_transaction(s,lambda:accept(s))
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
        return query(request,'request_detail',dict(request_id=task_id))
