"""A committed contiguous stream, durable prefix ACK and safe replays."""
import pytest
from sqlalchemy import text

from tests.dal.test_timeline_requests import world, submit
from personal_agent_dal.timeline.events import EventStream
from personal_agent_dal.timeline.requests import TimelineRefusal


def test_prefix_ack_lost_response_replay_and_reopen(world):
    engine, _, requests = world
    for n in range(3): submit(requests, n)
    events = EventStream(requests)
    page = events.read(after_seq=0, limit=2)
    assert [r['seq'] for r in page['items']] == [1,2]
    assert page['high_water_seq'] == 3
    first = page['items'][0]
    ack = events.ack(stream_id=page['stream_id'], through_seq=1, tail_digest=first['digest'])
    assert ack['ack_seq'] == 1
    assert EventStream(requests).ack(stream_id=page['stream_id'], through_seq=1, tail_digest=first['digest']) == ack
    assert len(events.read(after_seq=0)['items']) == 3  # retained after ACK
    last = events.read(after_seq=2)['items'][0]
    assert last['prev_digest'] == page['items'][1]['digest']
    assert events.ack(stream_id=page['stream_id'], through_seq=3, tail_digest=last['digest'])['ack_seq'] == 3
    assert events.ack(stream_id=page['stream_id'], through_seq=1, tail_digest=first['digest'])['ack_seq'] == 3


def test_wrong_stream_future_or_changed_digest_cannot_advance(world):
    _, _, requests = world
    submit(requests)
    events = EventStream(requests)
    page = events.read(after_seq=0)
    for stream, seq, sha in [('other',1,page['items'][0]['digest']),
                             (page['stream_id'],2,'a'*64), (page['stream_id'],1,'a'*64)]:
        with pytest.raises(TimelineRefusal): events.ack(stream_id=stream,through_seq=seq,tail_digest=sha)


def test_deleted_middle_event_is_not_a_valid_prefix(world):
    engine, _, requests = world
    for n in range(3): submit(requests,n)
    events = EventStream(requests)
    before = events.read(after_seq=0)
    with engine.begin() as c: c.execute(text('DELETE FROM development_events WHERE seq=2'))
    with pytest.raises(TimelineRefusal,match='EVENT_STREAM_CONFLICT'):
        events.read(after_seq=0)
    with pytest.raises(TimelineRefusal,match='EVENT_STREAM_CONFLICT'):
        events.ack(stream_id=before['stream_id'],through_seq=3,tail_digest=before['items'][-1]['digest'])


def test_empty_stream_has_no_fabricated_events(world):
    page = EventStream(world[2]).read(after_seq=0)
    assert page['items'] == [] and page['high_water_seq'] == 0
