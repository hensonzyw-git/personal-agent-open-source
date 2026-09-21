"""Continuous inbox/Timeline/cursor commit, followed by network ACK."""
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent.storage.models import DalConsumerCursor, DalEventInbox, ContextSession
from personal_agent.api import events
from personal_agent_dal.timeline.requests import digest


def clarification_text(body):
    questions=body.get('questions')
    summary=body.get('summary')
    if (not isinstance(summary,str) or not summary.strip() or not isinstance(questions,list)
        or not questions or any(not isinstance(q,str) or not q.strip() for q in questions)):
        raise ValueError('EVENT_STREAM_CONFLICT')
    return summary+'\n\n需要你确认：\n'+'\n'.join(f'{i}. {q}' for i,q in enumerate(questions,1))


class TimelineProjector:
    def __init__(self,bridge,session_manager):
        self.bridge,self.manager=bridge,session_manager

    def cursor(self):
        with self.bridge.sessions() as s:
            row=s.execute(select(DalConsumerCursor.__table__)).mappings().one_or_none()
            return dict(row) if row else None

    def _call(self,operation,body):
        return self.bridge.transport.call(operation=operation,request_id=new_id(),subject='service:pa-timeline',body=body)

    def sync(self,*,stop_event=None):
        cursor=self.cursor()
        page=self._call('events_read',dict(after_seq=cursor['received_seq'] if cursor else 0,
            stream_id=cursor['stream_id'] if cursor else None,limit=100))
        if not isinstance(page,dict) or set(page)!={'stream_id','items','high_water_seq','next_seq'}:raise ValueError('EVENT_STREAM_CONFLICT')
        if cursor and page['stream_id']!=cursor['stream_id']:raise ValueError('EVENT_STREAM_CONFLICT')
        if not isinstance(page['items'],list) or len(page['items'])>100:raise ValueError('EVENT_STREAM_CONFLICT')
        after=cursor['received_seq'] if cursor else 0
        if (type(page['high_water_seq']) is not int or type(page['next_seq']) is not int
            or page['high_water_seq']<after or page['next_seq']!=after+len(page['items'])
            or page['next_seq']>page['high_water_seq']
            or (not page['items'] and page['high_water_seq']>after)):
            raise ValueError('EVENT_STREAM_CONFLICT')
        for item in page['items']:
            if stop_event is not None and stop_event.is_set():return
            with self.bridge.sessions() as s:run_write_transaction(s,lambda:self._project(s,page['stream_id'],item))
        cursor=self.cursor()
        if cursor is None:return
        # Always replay the exact locally committed prefix; an old DAL ACK may
        # be ahead following a PA backup restore, but never advances local reads.
        ack=self._call('events_ack',dict(stream_id=cursor['stream_id'],through_seq=cursor['received_seq'],tail_digest=cursor['tail_digest']))
        if not isinstance(ack,dict) or ack.get('stream_id')!=cursor['stream_id'] or type(ack.get('ack_seq')) is not int or ack['ack_seq']<cursor['received_seq']:
            raise ValueError('EVENT_STREAM_CONFLICT')
        if ack['ack_seq']==cursor['received_seq'] and ack.get('tail_digest')!=cursor['tail_digest']:raise ValueError('EVENT_STREAM_CONFLICT')
        with self.bridge.sessions() as s:
            def update():
                row=s.scalar(select(DalConsumerCursor))
                if row.stream_id!=cursor['stream_id']:raise ValueError('EVENT_STREAM_CONFLICT')
                row.acked_seq=max(row.acked_seq,cursor['received_seq'])
                row.version+=1
            run_write_transaction(s,update)

    def _project(self,s,stream_id,item):
        required={'stream_id','seq','event_id','request_id','version','kind','body_digest','prev_digest','digest','body'}
        if not isinstance(item,dict) or set(item)!=required or item['stream_id']!=stream_id:raise ValueError('EVENT_STREAM_CONFLICT')
        if not isinstance(item['body'],dict) or type(item['seq']) is not int or type(item['version']) is not int:raise ValueError('EVENT_STREAM_CONFLICT')
        header={k:v for k,v in item.items() if k not in ('body','digest')}
        if digest(item['body'])!=item['body_digest'] or digest(header)!=item['digest']:raise ValueError('EVENT_STREAM_CONFLICT')
        existing=s.scalar(select(DalEventInbox).where(DalEventInbox.event_id==item['event_id']))
        if existing:
            if (existing.stream_id,existing.seq,existing.digest)!=(stream_id,item['seq'],item['digest']):raise ValueError('EVENT_STREAM_CONFLICT')
            return
        cursor=s.scalar(select(DalConsumerCursor))
        expected=cursor.received_seq+1 if cursor else 1
        previous=cursor.tail_digest if cursor else digest({'domain':'dal.timeline-genesis/1.0','stream_id':stream_id})
        if item['seq']!=expected or item['prev_digest']!=previous or (cursor and cursor.stream_id!=stream_id):raise ValueError('EVENT_STREAM_CONFLICT')
        now=self.bridge.now()
        timeline=events.canonical_timeline_id(s,now=now)
        session_id=self.manager.system_event_session(s,conversation_id=timeline,now=now)
        current=s.execute(select(ContextSession.__table__).where(ContextSession.session_id==session_id)).mappings().one()
        if current['status']!='open' or current['conversation_id']!=timeline:raise ValueError('SESSION_UNAVAILABLE')
        body=item['body']
        content=dict(schema_version='dal.timeline/1.0',dal_event_id=item['event_id'],task_id=item['request_id'],
            kind=item['kind'],source_version=item['version'],observed_at=now.isoformat(),
            text='DAL 已接收开发需求，等待项目确认；开发尚未开始。' if item['kind']=='request.accepted' else clarification_text(body) if item['kind']=='workflow.clarification' else body.get('summary','开发任务状态已更新。'),
            artifact=body.get('artifact'),decision=body.get('decision'),status=body.get('status'),phase=body.get('phase'))
        event_id=events.append_event(s,self.bridge.keyring,conversation_id=timeline,session_id=session_id,
            turn_id='dal:'+item['event_id'],event_type='development_update',content=content,operation_id=None,now=now)
        from personal_agent.storage.models import DalDecisionState, DalContextBinding
        from sqlalchemy import update
        invalidated=body.get('invalidated_decision_ids',[])
        if not isinstance(invalidated,list) or any(not isinstance(ref,str) for ref in invalidated):
            raise ValueError('EVENT_STREAM_CONFLICT')
        invalidated=list(invalidated)
        if item['kind']=='decision.accepted':
            if not isinstance(body.get('decision_id'),str):raise ValueError('EVENT_STREAM_CONFLICT')
            invalidated.append(body['decision_id'])
        # New proposals supersede earlier pending proposals even when replaying
        # events from versions that did not publish explicit invalidation refs.
        decision=body.get('decision')
        if item['kind']=='decision.requested':
            if not isinstance(decision,dict) or digest(decision.get('binding'))!=decision.get('binding_digest'):
                raise ValueError('EVENT_STREAM_CONFLICT')
            invalidated.extend(s.scalars(select(DalDecisionState.decision_id).where(
                DalDecisionState.request_id==item['request_id'],DalDecisionState.status=='pending',
                DalDecisionState.decision_id!=decision['decision_id'])))
        for ref in set(invalidated):
            state=s.get(DalDecisionState,ref)
            status='consumed' if ref==body.get('decision_id') and item['kind']=='decision.accepted' else 'superseded'
            if state is None:
                s.add(DalDecisionState(decision_id=ref,request_id=item['request_id'],status=status,source_seq=item['seq']))
            else:
                if state.request_id!=item['request_id']:raise ValueError('EVENT_STREAM_CONFLICT')
                state.status=status;state.source_seq=item['seq']
            s.execute(update(DalContextBinding).where(DalContextBinding.decision_id==ref).values(consumed=1))
        if item['kind']=='decision.requested':
            state=s.get(DalDecisionState,decision['decision_id'])
            if state is not None:raise ValueError('EVENT_STREAM_CONFLICT')
            s.add(DalDecisionState(decision_id=decision['decision_id'],request_id=item['request_id'],
                binding_digest=decision['binding_digest'],event_id=event_id,status='pending',source_seq=item['seq']))
        from personal_agent.api.dal_notifications import enqueue
        enqueue(s,event_id=event_id,kind=item['kind'],now=now)
        s.add(DalEventInbox(event_id=item['event_id'],stream_id=stream_id,seq=item['seq'],digest=item['digest'],
            workflow_id=item['request_id'],timeline_event_id=event_id))
        if cursor is None:
            s.add(DalConsumerCursor(consumer_id='pa-timeline',stream_id=stream_id,received_seq=item['seq'],acked_seq=0,tail_digest=item['digest'],version=1))
        else:
            cursor.received_seq,cursor.tail_digest,cursor.version=item['seq'],item['digest'],cursor.version+1

    def repair_clarification(self, item):
        """Append missing questions from a verified inbox event; never rewind ACK."""
        from personal_agent.storage.models import ConversationEvent
        required={'stream_id','seq','event_id','request_id','version','kind','body_digest','prev_digest','digest','body'}
        if not isinstance(item,dict) or set(item)!=required or item['kind']!='workflow.clarification':
            raise ValueError('EVENT_STREAM_CONFLICT')
        header={k:v for k,v in item.items() if k not in ('body','digest')}
        if digest(item['body'])!=item['body_digest'] or digest(header)!=item['digest']:
            raise ValueError('EVENT_STREAM_CONFLICT')
        text=clarification_text(item['body'])
        def work(s):
            inbox=s.scalar(select(DalEventInbox).where(DalEventInbox.event_id==item['event_id']))
            if inbox is None or (inbox.stream_id,inbox.seq,inbox.digest,inbox.workflow_id)!=(item['stream_id'],item['seq'],item['digest'],item['request_id']):
                raise ValueError('EVENT_STREAM_CONFLICT')
            original=s.get(ConversationEvent,inbox.timeline_event_id)
            content=events._entry(self.bridge.keyring,original).content
            if content.get('text')==text:return original.event_id
            if content.get('text')!=item['body']['summary']:raise ValueError('EVENT_STREAM_CONFLICT')
            turn='dal-clarification-display-repair:'+item['event_id']
            existing=s.scalar(select(ConversationEvent).where(ConversationEvent.turn_id==turn))
            if existing:return existing.event_id
            now=self.bridge.now()
            timeline=events.canonical_timeline_id(s,now=now)
            sid=self.manager.system_event_session(s,conversation_id=timeline,now=now)
            return events.append_event(s,self.bridge.keyring,conversation_id=timeline,session_id=sid,
                turn_id=turn,event_type='development_update',operation_id=None,now=now,
                content=dict(content,text='补充显示此前遗漏的澄清问题：\n'+text,
                    observed_at=now.isoformat(),projection_repair_of=original.event_id))
        with self.bridge.sessions() as s:return run_write_transaction(s,lambda:work(s))
