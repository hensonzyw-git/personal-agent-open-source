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


def explanation_only(text):
    """Narrow fail-closed guard for questions about a development blocker.

    This is not a general intent classifier: ambiguous mixed correction/question
    messages in this class remain conversational and cannot mutate DAL.
    """
    import re
    return bool(re.search(r'卡点|卡住|开发|任务|目录|directory|source|检查',text,re.I)
        and re.search(r'为什么|为何|什么意思|怎么回事|检查.{0,16}(?:什么|哪)|什么.{0,16}(?:目录|directory)|\bwhy\b|\bwhat\b',text,re.I))


def restrict_development_tools(specs, text):
    return [spec for spec in specs if not spec.business_name.startswith('dal.')] if explanation_only(text) else specs
