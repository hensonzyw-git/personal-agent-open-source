"""Stable notification batches; delivery is a navigation hint, not a decision."""
import json
from datetime import timedelta
from sqlalchemy import select
from personal_agent.storage.models import (
    DalNotification, DalNotificationBatch, DalNotificationMembership,
    DalDecisionState, Device, ConversationEvent,
)
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent.api.notifications import PushSendError

NOTIFY=frozenset({'decision.requested','workflow.blocked','workflow.clarification','workflow.completed'})
IMMEDIATE=frozenset({'workflow.blocked'})


def enqueue(session,*,event_id,kind,now):
    if kind not in NOTIFY:return
    for device in session.scalars(select(Device).where(Device.status=='active',Device.encrypted_push_token.is_not(None))):
        if 'dal.read' not in json.loads(device.scopes):continue
        if session.scalar(select(DalNotification.notification_id).where(DalNotification.device_id==device.device_id,DalNotification.event_id==event_id)):continue
        session.add(DalNotification(notification_id=new_id(),device_id=device.device_id,event_id=event_id,
            status='pending',attempts=0,next_attempt_at=now if kind in IMMEDIATE else now+timedelta(minutes=2)))
        session.flush()
        pending=list(session.scalars(select(DalNotification).where(DalNotification.device_id==device.device_id,
            DalNotification.status=='pending',DalNotification.attempts==0).order_by(DalNotification.next_attempt_at).limit(5)))
        if len(pending)==5:
            for row in pending:row.next_attempt_at=min(row.next_attempt_at,now)


def _valid(session,bridge,event_id):
    from personal_agent.api.events import _entry
    event=session.get(ConversationEvent,event_id)
    if event is None:return False
    content=_entry(bridge.keyring,event).content
    state=session.scalar(select(DalDecisionState).where(DalDecisionState.event_id==event_id))
    if state is None:
        if content.get('kind') not in ('workflow.blocked','workflow.clarification'):return True
        from personal_agent.storage.models import DalEventInbox
        recent=session.scalars(select(DalEventInbox).where(DalEventInbox.workflow_id==content.get('task_id')).order_by(DalEventInbox.seq.desc()).limit(128))
        for inbox in recent:
            projected=session.get(ConversationEvent,inbox.timeline_event_id)
            latest=_entry(bridge.keyring,projected).content if projected else {}
            if latest.get('status') is not None:
                return latest['status']=='blocked' and latest.get('source_version')==content.get('source_version')
        return False
    if state.status!='pending':return False
    # Read the projection's current expiry without relying on any device having
    # opened the message. ConversationEvent stores encrypted content.
    from personal_agent.api.events import _entry
    event=session.get(ConversationEvent,event_id)
    if event is None:return False
    content=_entry(bridge.keyring,event).content
    from datetime import datetime
    decision=content.get('decision')
    return bool(decision and datetime.fromisoformat(decision['expires_at'])>bridge.now())


def notification_context(bridge,auth,batch_id):
    with bridge.sessions() as session:
        bridge._identity(session,auth,'dal.read')
        batch=session.get(DalNotificationBatch,batch_id)
        if batch is None or batch.device_id!=auth.device_id:raise ValueError('NOTIFICATION_NOT_FOUND')
        from personal_agent.api.events import _entry
        ids=json.loads(batch.event_ids)
        items=[]
        for event_id in ids:
            row=session.get(ConversationEvent,event_id)
            content=_entry(bridge.keyring,row).content if row else {}
            items.append(dict(event_id=event_id,text=content.get('text','开发事项'),current=_valid(session,bridge,event_id)))
        return dict(notification_id=batch_id,event_ids=ids,items=items)


def deliver(bridge,sender,*,stop_event=None):
    if sender is None:return
    now=bridge.now()
    with bridge.sessions() as session:
        devices=list(session.scalars(select(DalNotification.device_id).where(
            DalNotification.status.in_(('pending','sending')),DalNotification.next_attempt_at<=now).distinct().limit(100)))
    for device_id in devices:
        if stop_event is not None and stop_event.is_set():return
        def claim(session):
            device=session.get(Device,device_id)
            rows=list(session.scalars(select(DalNotification).where(DalNotification.device_id==device_id,
                DalNotification.status.in_(('pending','sending')),DalNotification.next_attempt_at<=now)
                .order_by(DalNotification.next_attempt_at,DalNotification.notification_id).limit(5)))
            if device is None or device.status!='active' or 'dal.read' not in json.loads(device.scopes) or device.encrypted_push_token is None:
                for row in rows:row.status='undeliverable'
                return None,[]
            eligible=[]
            existing_batch=None
            for row in rows:
                if row.attempts>=5 or not _valid(session,bridge,row.event_id):row.status='undeliverable';continue
                membership=session.get(DalNotificationMembership,row.notification_id)
                if membership:
                    if eligible:break
                    existing_batch=membership.batch_id
                    eligible=list(session.scalars(select(DalNotification).join(DalNotificationMembership).where(
                        DalNotificationMembership.batch_id==existing_batch,DalNotification.status.in_(('pending','sending')),
                        DalNotification.next_attempt_at<=now)))
                    break
                eligible.append(row)
            if not eligible:return None,[]
            batch_id=existing_batch or new_id()
            if existing_batch is None:
                session.add(DalNotificationBatch(batch_id=batch_id,device_id=device_id,
                    event_ids=canonical_json([r.event_id for r in eligible]),created_at=now));session.flush()
                for row in eligible:session.add(DalNotificationMembership(notification_id=row.notification_id,batch_id=batch_id))
            result=[]
            for row in eligible:
                if not _valid(session,bridge,row.event_id) or row.attempts>=5:row.status='undeliverable';continue
                row.attempts+=1;row.status='sending';row.next_attempt_at=now+timedelta(minutes=2)
                result.append((row.notification_id,row.attempts))
            return batch_id,result
        with bridge.sessions() as session:batch_id,batch=run_write_transaction(session,lambda:claim(session))
        if not batch:continue
        failure=None
        try:sender.send_development(device_id,notification_id=batch_id,count=len(batch))
        except PushSendError as exc:failure=exc
        except (OSError,TimeoutError):failure=PushSendError('development push result unknown')
        def finish(session):
            for ident,attempt in batch:
                row=session.get(DalNotification,ident)
                if row is None or row.attempts!=attempt or row.status!='sending':continue
                if failure is None:row.status='provider_accepted'
                elif failure.permanent or attempt>=5:row.status='undeliverable'
                else:row.status='pending';row.next_attempt_at=bridge.now()+timedelta(seconds=min(60*2**attempt,3600))
        with bridge.sessions() as session:run_write_transaction(session,lambda:finish(session))
