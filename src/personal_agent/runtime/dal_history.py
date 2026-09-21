"""Bounded, same-session development messages visible to the current device."""
import json
from sqlalchemy import select
from personal_agent.api import events
from personal_agent.storage.models import ConversationEvent


def development_history(owner):
    bridge=owner.deps.dal_timeline
    if bridge is None or 'dal.read' not in owner.auth.scopes:return []
    with owner.deps.session_factory() as s:
        bridge._identity(s,owner.auth,'dal.read')
        anchor=s.get(ConversationEvent,owner.anchor.event_id)
        if anchor is None:return []
        rows=list(s.scalars(select(ConversationEvent).where(
            ConversationEvent.session_id==anchor.session_id,
            ConversationEvent.conversation_id==anchor.conversation_id,
            ConversationEvent.timeline_sequence<anchor.timeline_sequence,
            ConversationEvent.event_type=='development_update')
            .order_by(ConversationEvent.timeline_sequence.desc()).limit(8)))
        output=[]
        for row in reversed(rows):
            content=events._entry(owner.deps.keyring,row).content
            # Presentation is evidence of what the user saw, never authority or
            # current state. Do not include artifacts, bindings or old approvals.
            value={k:content[k] for k in ('kind','text','status','phase','source_version') if k in content}
            output.append(json.dumps({'development_message':value,'historical':True,
                'event_id':row.event_id},ensure_ascii=False))
        return output
