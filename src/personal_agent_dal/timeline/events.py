"""Authenticated callers consume a contiguous durable stream; ACK never deletes."""
from sqlalchemy import select
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.storage.timeline_models import DevelopmentEvent, DevelopmentEventStream, DevelopmentEventConsumer
from personal_agent_dal.timeline.requests import TimelineRefusal, digest


class EventStream:
    def __init__(self, requests):
        self.requests = requests

    def _verified(self, s, stream, *, after_seq, through_seq):
        if after_seq == 0:
            previous = digest({'domain':'dal.timeline-genesis/1.0', 'stream_id':stream.stream_id})
        else:
            parent = s.scalar(select(DevelopmentEvent).where(DevelopmentEvent.stream_id==stream.stream_id, DevelopmentEvent.seq==after_seq))
            if parent is None: raise TimelineRefusal('EVENT_STREAM_CONFLICT')
            previous = parent.digest
        rows = list(s.scalars(select(DevelopmentEvent).where(DevelopmentEvent.stream_id==stream.stream_id,
            DevelopmentEvent.seq>after_seq, DevelopmentEvent.seq<=through_seq).order_by(DevelopmentEvent.seq)))
        if len(rows) != through_seq-after_seq: raise TimelineRefusal('EVENT_STREAM_CONFLICT')
        items=[]
        for seq,row in enumerate(rows,after_seq+1):
            body=self.requests._open(DevelopmentEvent,row.event_id,'sealed_body',row.sealed_body)
            header=dict(stream_id=stream.stream_id,seq=seq,event_id=row.event_id,
                request_id=row.request_id,version=row.version,kind=row.kind,body_digest=digest(body),prev_digest=previous)
            if row.seq!=seq or row.prev_digest!=previous or row.body_digest!=header['body_digest'] or row.digest!=digest(header):
                raise TimelineRefusal('EVENT_STREAM_CONFLICT')
            items.append(dict(**header,digest=row.digest,body=body))
            previous=row.digest
        if through_seq==stream.next_seq-1 and previous!=stream.tail_digest:
            raise TimelineRefusal('EVENT_STREAM_CONFLICT')
        return items,previous

    def read(self, *, after_seq=0, limit=100, stream_id=None):
        if type(after_seq) is not int or after_seq<0 or type(limit) is not int or not 1<=limit<=100:
            raise TimelineRefusal('INVALID_ARGUMENT')
        with self.requests.sessions() as s:
            stream=s.scalar(select(DevelopmentEventStream))
            if stream is None:
                if after_seq or stream_id: raise TimelineRefusal('EVENT_STREAM_CONFLICT')
                return dict(stream_id=None,items=[],high_water_seq=0,next_seq=0)
            if (stream_id is not None and stream_id!=stream.stream_id) or after_seq>=stream.next_seq:
                raise TimelineRefusal('EVENT_STREAM_CONFLICT')
            through=min(after_seq+limit,stream.next_seq-1)
            items,_=self._verified(s,stream,after_seq=after_seq,through_seq=through)
            return dict(stream_id=stream.stream_id,items=items,high_water_seq=stream.next_seq-1,next_seq=through)

    def ack(self, *, stream_id, through_seq, tail_digest):
        if type(through_seq) is not int or through_seq<0:
            raise TimelineRefusal('INVALID_ARGUMENT')
        def work(s):
            stream=s.scalar(select(DevelopmentEventStream).where(DevelopmentEventStream.stream_id==stream_id))
            if stream is None or through_seq>=stream.next_seq: raise TimelineRefusal('EVENT_STREAM_CONFLICT')
            # Validate the entire claimed prefix, not only its last row. A hole
            # must never be blessed by an ACK to an existing later event.
            _,tail=self._verified(s,stream,after_seq=0,through_seq=through_seq)
            if tail!=tail_digest: raise TimelineRefusal('EVENT_STREAM_CONFLICT')
            row=s.scalar(select(DevelopmentEventConsumer).where(DevelopmentEventConsumer.stream_id==stream_id,
                DevelopmentEventConsumer.consumer_id=='pa-timeline'))
            if row is None:
                row=DevelopmentEventConsumer(stream_id=stream_id,consumer_id='pa-timeline',ack_seq=through_seq,tail_digest=tail,version=1)
                s.add(row)
            elif through_seq>row.ack_seq:
                row.ack_seq,row.tail_digest,row.version=through_seq,tail,row.version+1
            return dict(stream_id=stream_id,ack_seq=row.ack_seq,tail_digest=row.tail_digest)
        with self.requests.sessions() as s:
            return run_write_transaction(s,lambda:work(s))
