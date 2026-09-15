"""PA owns authenticated clicks; queued delivery is not DAL acceptance."""
import json
import logging
import httpx
from datetime import datetime
from typing import Literal
from sqlalchemy import select
from fastapi import HTTPException, Request
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now
from personal_agent.storage.models import Device, DalResumeProposal, DalResumeDecision, DalResumeDelivery
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest, digest
from personal_agent.api.dal_client import sign_decision

logger = logging.getLogger(__name__)
SCOPE = 'dal.resume.approve'


class ProposalRequest(Closed):
    request_id: Id
    feature_id: Id
    selection_id: Id


class DecisionRequest(Closed):
    request_id: Id
    proposal_id: Id
    binding_sha256: Digest
    decision: Literal['approve_once', 'approve_once_accept_duplicate_cost', 'reject']


class ResumeBridge:
    def __init__(self, *, session_factory, transport, key, kid, issuer='pa-resume', audience='dal-resume', now=utc_now):
        self.sessions, self.transport = session_factory, transport
        self.key, self.kid, self.issuer, self.audience, self.now = key, kid, issuer, audience, now

    def _identity(self, s, auth):
        d = s.get(Device, auth.device_id)
        if not d or d.status != 'active': raise ValueError('DEVICE_INACTIVE')
        if SCOPE not in auth.scopes or SCOPE not in json.loads(d.scopes): raise ValueError('SCOPE_REQUIRED')
        if auth.subject_id != 'device:'+d.device_id or auth.key_thumbprint != d.device_key_thumbprint:
            raise ValueError('DEVICE_IDENTITY_MISMATCH')
        return d

    def proposal(self, auth, body):
        sha=digest(dict(device_id=auth.device_id,**body.model_dump()))
        with self.sessions() as s:
            self._identity(s,auth)
            old=s.scalar(select(DalResumeProposal).where(DalResumeProposal.request_id == body.request_id))
            if old:
                if old.request_sha256 != sha: raise ValueError('IDEMPOTENCY_CONFLICT')
                return json.loads(old.body)
        # The read bridge is outside the PA transaction.
        response = self.transport.propose(body.model_dump())
        from personal_agent_dal.machine.resume_authority import ResumeBinding
        from pydantic import TypeAdapter
        from datetime import timedelta
        try:
            if not isinstance(response, dict) or set(response) != {'proposal_id','binding','binding_sha256','expires_at'}:
                raise ValueError
            TypeAdapter(Id).validate_python(response['proposal_id'])
            ResumeBinding.model_validate(response['binding'])
            if digest(response['binding']) != response['binding_sha256']:
                raise ValueError
            expires = datetime.fromisoformat(response['expires_at'])
            if expires.tzinfo is None or expires.utcoffset() != timedelta(0):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError('PROPOSAL_RESPONSE_INVALID') from None
        if response['binding']['feature_id'] != body.feature_id or response['binding']['selection_id'] != body.selection_id:
            raise ValueError('PROPOSAL_TARGET_MISMATCH')
        if expires <= self.now(): raise ValueError('PROPOSAL_EXPIRED')
        sha=digest(dict(device_id=auth.device_id,**body.model_dump()))
        def work(s):
            self._identity(s,auth)
            old=s.scalar(select(DalResumeProposal).where(DalResumeProposal.request_id == body.request_id))
            if old:
                if old.request_sha256 != sha: raise ValueError('IDEMPOTENCY_CONFLICT')
                return json.loads(old.body)
            s.add(DalResumeProposal(proposal_id=response['proposal_id'],request_id=body.request_id,
                device_id=auth.device_id,request_sha256=sha,body=canonical_json(response),expires_at=expires))
            return response
        with self.sessions() as s: return run_write_transaction(s,lambda:work(s))

    def decide(self, auth, body):
        sha=digest(dict(device_id=auth.device_id,**body.model_dump()))
        def work(s):
            d=self._identity(s,auth)
            old=s.scalar(select(DalResumeDecision).where(DalResumeDecision.request_id == body.request_id))
            if old:
                if old.request_sha256 != sha: raise ValueError('IDEMPOTENCY_CONFLICT')
                return self._response(s,old)
            p=s.get(DalResumeProposal,body.proposal_id)
            now=self.now()
            if not p or p.device_id != d.device_id or p.expires_at <= now: raise ValueError('PROPOSAL_INVALID')
            proposal=json.loads(p.body)
            if proposal['binding_sha256'] != body.binding_sha256: raise ValueError('PROPOSAL_DIGEST_MISMATCH')
            if proposal['binding']['isolation_id'] and body.decision == 'approve_once': raise ValueError('DUPLICATE_COST_NOT_ACCEPTED')
            decision_id=new_id()
            claims=dict(iss=self.issuer,aud=self.audience,jti=new_id(),iat=int(now.timestamp()),
                exp=min(int(p.expires_at.timestamp()),int(now.timestamp())+900),decision_id=decision_id,
                device_id=d.device_id,subject_id=auth.subject_id,key_thumbprint=d.device_key_thumbprint,
                decision=body.decision,proposal_id=p.proposal_id,binding_sha256=body.binding_sha256)
            row=DalResumeDecision(decision_id=decision_id,request_id=body.request_id,request_sha256=sha,
                proposal_id=p.proposal_id,device_id=d.device_id,subject_id=auth.subject_id,
                key_thumbprint=d.device_key_thumbprint,decision=body.decision,claims=canonical_json(claims),expires_at=p.expires_at)
            s.add(row)
            s.flush()  # Decision precedes its delivery outbox row.
            s.add(DalResumeDelivery(decision_id=decision_id,status='rejected' if body.decision=='reject' else 'queued',approval_id=None,attempts=0))
            s.flush()
            return self._response(s,row)
        with self.sessions() as s: return run_write_transaction(s,lambda:work(s))

    def _response(self,s,row):
        delivery=s.get(DalResumeDelivery,row.decision_id)
        return dict(decision_id=row.decision_id,delivery_status=delivery.status,expires_at=row.expires_at.isoformat())

    def deliver_pending(self, *, stop_event=None):
        with self.sessions() as s:
            rows=list(s.scalars(select(DalResumeDecision).join(DalResumeDelivery,
                DalResumeDelivery.decision_id==DalResumeDecision.decision_id).where(DalResumeDelivery.status=='queued').limit(50)))
            pending=[(r.decision_id,json.loads(r.claims)) for r in rows]
        for decision_id,claims in pending:
            if stop_event is not None and stop_event.is_set(): return
            if claims['exp'] <= int(self.now().timestamp()):
                status, approval_id='expired',None
            else:
                # Count durable dispatch starts, including refusals and lost replies.
                # Expiration without a transport call is not an attempt.
                with self.sessions() as s:
                    def count_attempt():
                        row=s.get(DalResumeDelivery,decision_id)
                        if row.status != 'queued': return False
                        row.attempts += 1
                        return True
                    if not run_write_transaction(s,count_attempt): continue
                try:
                    result=self.transport.deliver(sign_decision(claims,key=self.key,kid=self.kid))
                    if set(result) != {'decision_id','approval_id','status'} or result['decision_id'] != decision_id or result['status'] != 'accepted' or not result['approval_id']:
                        raise ValueError('DELIVERY_RESPONSE_INVALID')
                    status,approval_id='accepted',result['approval_id']
                except httpx.HTTPStatusError as exc:
                    category = 'transient' if exc.response.status_code >= 500 or exc.response.status_code in (408,429) else 'refused'
                    logger.warning('DAL resume delivery %s; retained queued', category)
                    continue
                except (OSError, httpx.RequestError):
                    logger.warning('DAL resume delivery transient; retained queued')
                    continue
                except ValueError:
                    logger.warning('DAL resume delivery refused; retained queued')
                    continue
            with self.sessions() as s:
                def work():
                    row=s.get(DalResumeDelivery,decision_id)
                    if row.status=='queued':
                        row.status = 'delivery_unknown' if status == 'expired' and row.attempts > 0 else status
                        row.approval_id = approval_id
                run_write_transaction(s,work)



def mount_routes(app, deps, authenticate):
    def auth(request):
        with deps.session_factory() as s:
            return authenticate(request,s)
    def bridge():
        if deps.dal_resume is None: raise HTTPException(503,'DAL_RESUME_UNAVAILABLE')
        return deps.dal_resume
    @app.post('/v1/dal/resume-proposals')
    def proposal(body: ProposalRequest, request: Request):
        try: return bridge().proposal(auth(request),body)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if 400 <= code < 500 and code not in (408,429):
                raise HTTPException(code,'DAL_PROPOSAL_REFUSED') from None
            raise HTTPException(503,'DAL_RESUME_UNAVAILABLE') from None
        except (httpx.RequestError, OSError):
            raise HTTPException(503,'DAL_RESUME_UNAVAILABLE') from None
        except ValueError:
            raise HTTPException(403,'DAL_PROPOSAL_REFUSED') from None
    @app.post('/v1/dal/resume-decisions')
    def decision(body: DecisionRequest, request: Request):
        try: return bridge().decide(auth(request),body)
        except ValueError as exc: raise HTTPException(403,str(exc)) from exc
