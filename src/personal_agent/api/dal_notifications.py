"""Durable, coalesced development notifications with opaque Timeline routing."""
import json
from datetime import timedelta
from sqlalchemy import select
from personal_agent.storage.models import DalNotification, Device
from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent.api.notifications import PushSendError

NOTIFY = frozenset({'decision.requested','workflow.blocked','workflow.clarification','workflow.completed'})


def enqueue(session, *, event_id, kind, now):
    if kind not in NOTIFY:return
    for device in session.scalars(select(Device).where(Device.status=='active',Device.encrypted_push_token.is_not(None))):
        if 'dal.read' not in json.loads(device.scopes):continue
        old=session.scalar(select(DalNotification.notification_id).where(DalNotification.device_id==device.device_id,DalNotification.event_id==event_id))
        if old:continue
        session.add(DalNotification(notification_id=new_id(),device_id=device.device_id,event_id=event_id,
            status='pending',attempts=0,next_attempt_at=now+timedelta(seconds=10)))


def deliver(bridge, sender, *, stop_event=None):
    if sender is None:return
    now=bridge.now()
    with bridge.sessions() as s:
        devices=list(s.scalars(select(DalNotification.device_id).where(DalNotification.status.in_(('pending','sending')),
            DalNotification.next_attempt_at<=now).distinct().limit(100)))
    for device_id in devices:
        if stop_event is not None and stop_event.is_set():return
        def claim(s):
            rows=list(s.scalars(select(DalNotification).where(DalNotification.device_id==device_id,
                DalNotification.status.in_(('pending','sending')),DalNotification.next_attempt_at<=now)
                .order_by(DalNotification.next_attempt_at,DalNotification.notification_id).limit(100)))
            device=s.scalar(select(Device).where(Device.device_id==device_id))
            if device is None or device.status!='active' or 'dal.read' not in json.loads(device.scopes) or device.encrypted_push_token is None:
                for row in rows:row.status='undeliverable'
                return []
            result=[]
            for row in rows:
                if row.attempts>=5:row.status='undeliverable';continue
                row.attempts+=1;row.status='sending';row.next_attempt_at=now+timedelta(minutes=2)
                result.append((row.notification_id,row.event_id,row.attempts))
            return result
        with bridge.sessions() as s:batch=run_write_transaction(s,lambda:claim(s))
        if not batch:continue
        failure=None
        try:sender.send_development(device_id,event_id=batch[-1][1],count=len(batch))
        except PushSendError as exc:failure=exc
        except (OSError,TimeoutError):failure=PushSendError('development push result unknown')
        def finish(s):
            for id,_,attempt in batch:
                row=s.scalar(select(DalNotification).where(DalNotification.notification_id==id))
                if row is None or row.attempts!=attempt or row.status!='sending':continue
                if failure is None:row.status='provider_accepted'
                elif failure.permanent or attempt>=5:row.status='undeliverable'
                else:row.status='pending';row.next_attempt_at=bridge.now()+timedelta(seconds=min(60*2**attempt,3600))
        with bridge.sessions() as s:run_write_transaction(s,lambda:finish(s))
