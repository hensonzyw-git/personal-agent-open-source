"""A document click is context, never an approval."""
from datetime import datetime
from sqlalchemy import select
from personal_agent.storage.models import DalContextBinding, DalDecisionState
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_dal.timeline.requests import digest


def delivered(session,bridge,auth,entries):
    if 'dal.read' not in auth.scopes:return
    bridge._identity(session,auth,'dal.read')
    for event in entries:
        if event.event_type!='development_update' or not isinstance(event.content,dict):continue
        decision=event.content.get('decision')
        if not isinstance(decision,dict) or 'binding' not in decision:continue
        state=session.get(DalDecisionState,decision.get('decision_id'))
        if state is None or state.status!='pending' or state.binding_digest!=decision.get('binding_digest'):
            continue
        binding=decision['binding']
        if digest(binding)!=decision['binding_digest']:raise ValueError('DAL_CONTEXT_INVALID')
        expires=datetime.fromisoformat(decision['expires_at'])
        if expires<=bridge.now():continue
        old=session.scalar(select(DalContextBinding).where(DalContextBinding.device_id==auth.device_id,DalContextBinding.decision_id==decision['decision_id']))
        if old:
            if old.binding_digest!=decision['binding_digest']:raise ValueError('DAL_CONTEXT_INVALID')
            continue
        id=new_id()
        session.add(DalContextBinding(context_id=id,device_id=auth.device_id,decision_id=decision['decision_id'],event_id=event.event_id,
            binding_digest=decision['binding_digest'],sealed_context=bridge.keyring.encrypt(canonical_json(decision).encode(),table='dal_context_bindings',column='sealed_context',row_id=id),expires_at=expires,consumed=0))


def pending(bridge,auth):
    import json
    with bridge.sessions() as s:
        bridge._identity(s,auth,'dal.read')
        rows=list(s.scalars(select(DalContextBinding).where(DalContextBinding.device_id==auth.device_id,DalContextBinding.consumed==0,DalContextBinding.expires_at>bridge.now())))
        result=[]
        for row in rows:
            state=s.get(DalDecisionState,row.decision_id)
            if state is None or state.status!='pending' or state.binding_digest!=row.binding_digest:
                continue
            value=json.loads(bridge.keyring.decrypt(row.sealed_context,table='dal_context_bindings',column='sealed_context',row_id=row.context_id))
            if digest(value['binding'])!=row.binding_digest:raise ValueError('DAL_CONTEXT_INVALID')
            result.append(dict(context_id=row.context_id,event_id=row.event_id,**value))
        return result


from typing import Literal
from pydantic import StrictInt
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest
from personal_agent.api.dal_client import sign_decision, verify_closed_assertion


class ReplyClaims(Closed):
    iss: Literal['pa-timeline']
    aud: Literal['pa-dal-reply']
    domain: Literal['dal.reply-context/1.0']
    jti: Id
    iat: StrictInt
    exp: StrictInt
    subject: Id
    key_thumbprint: str
    context_id: Id
    event_id: Id
    binding_digest: Digest


def mint(bridge, auth, event_id):
    """Only previously delivered, still-pending context can be selected."""
    matches = [c for c in pending(bridge, auth) if c['event_id'] == event_id]
    if len(matches) != 1:
        raise ValueError('DAL_CONTEXT_UNAVAILABLE')
    context = matches[0]
    current=bridge.query(auth,operation='decision_status',body={'decision_id':context['decision_id']})
    if (current.get('valid') is not True or current.get('decision_id')!=context['decision_id']
        or current.get('binding_digest')!=context['binding_digest']):
        raise ValueError('DAL_CONTEXT_UNAVAILABLE')
    now = int(bridge.now().timestamp())
    expiry = min(now + 900, int(datetime.fromisoformat(context['expires_at']).timestamp()))
    claims = dict(iss='pa-timeline', aud='pa-dal-reply', domain='dal.reply-context/1.0',
                  jti=new_id(), iat=now, exp=expiry, subject=auth.subject_id,
                  key_thumbprint=auth.key_thumbprint, context_id=context['context_id'],
                  event_id=event_id, binding_digest=context['binding_digest'])
    return dict(event_id=event_id, token=sign_decision(claims, key=bridge.transport.key, kid=bridge.transport.kid))


def select_reply(bridge, auth, reply, contexts):
    claims = verify_closed_assertion(reply['token'], keys={bridge.transport.kid: bridge.transport.key.public_key()},
                                    schema=ReplyClaims, issuer='pa-timeline', audience='pa-dal-reply',
                                    now_epoch=int(bridge.now().timestamp()))
    if (claims['subject'] != auth.subject_id or claims['key_thumbprint'] != auth.key_thumbprint
        or claims['event_id'] != reply['event_id']):
        raise ValueError('DAL_CONTEXT_INVALID')
    matches = [c for c in contexts if c['context_id'] == claims['context_id']
               and c['event_id'] == claims['event_id'] and c['binding_digest'] == claims['binding_digest']]
    if len(matches) != 1:
        raise ValueError('DAL_CONTEXT_UNAVAILABLE')
    return matches
