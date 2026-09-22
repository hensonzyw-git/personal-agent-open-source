from personal_agent_dal.timeline.authorization_limits import expiry_projection, within_limit, extends_limit, valid_expiry
"""Operator templates and atomic phone decisions. No network or model calls."""
from datetime import timedelta
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.timeline.requests import digest, valid_id
from personal_agent_dal.timeline.authorization_contracts import ProjectTemplate,Preview,Approval,REFUSALS,aware_time
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.timeline_models import (
    DevelopmentProjectTemplate as Template, DevelopmentAuthorizationProposal as Proposal,
    DevelopmentAuthorizationPolicy as Policy, DevelopmentAuthorizationRequest as Pending,
    DevelopmentProjectAuthorization as Grant,DevelopmentProjectBinding as Binding,
    DevelopmentRequest as Request,DevelopmentWorkflow as Workflow,DevelopmentGate as Gate,
    DevelopmentCommand as Command,DevelopmentDriverStep as Step,DevelopmentRemoteEffect as Effect,
    DevelopmentExecution as Execution,DevelopmentDecisionRequest as Decision,
)


class ProjectAuthorizationService:
    def __init__(self,requests):self.r=requests

    def _write(self,fn):
        with self.r.sessions() as s:return run_write_transaction(s,lambda:fn(s))

    def _audit(self,s,event,trace,actor):
        append_audit_event(s,event_id=new_id(),trace_id=trace,event_type=event,
            redacted_summary='explicit project authorization; actor='+actor,now=self.r.now())

    def register_template(self,template,*,actor,observed_at,expires_at,evidence_digest):
        template=ProjectTemplate.model_validate(template)
        valid_id(actor)
        unbounded=expires_at is None
        observed_at,expires_at=aware_time(observed_at),expiry_projection(expires_at)
        if not unbounded:expires_at=aware_time(expires_at)
        from personal_agent_dal.machine.workflow_selection import Digest
        from pydantic import TypeAdapter
        TypeAdapter(Digest).validate_python(evidence_digest)
        if not observed_at<=self.r.now()<expires_at or (not unbounded and expires_at>observed_at+timedelta(hours=24)):raise ValueError('INVALID_ARGUMENT')
        value=template.model_dump(mode='json');sha=digest(value)
        def work(s):
            existing=s.scalar(select(Template).where(Template.project_id==template.project_id,Template.revision==template.revision))
            if existing:
                if existing.digest!=sha or not existing.active:raise ValueError('IDEMPOTENCY_CONFLICT')
                if observed_at<existing.observed_at:raise ValueError('STALE_BINDING')
                existing.observed_at=observed_at;existing.expires_at=expires_at;existing.evidence_digest=evidence_digest;existing.actor=actor
                self._audit(s,'development.project.template_revalidated',template.project_id,actor)
                return dict(template_id=existing.template_id,template_digest=sha)
            old=list(s.scalars(select(Template).where(Template.project_id==template.project_id)))
            if old and template.revision<=max(t.revision for t in old):raise ValueError('STALE_BINDING')
            if not old and len(list(s.scalars(select(Template.project_id).distinct())))>=128:raise ValueError('INVALID_ARGUMENT')
            for row in old:row.active=0
            s.flush()
            ident=new_id();s.add(Template(template_id=ident,project_id=template.project_id,revision=template.revision,active=1,
                digest=sha,sealed_template=self.r._seal(Template,ident,'sealed_template',value),
                observed_at=observed_at,expires_at=expires_at,evidence_digest=evidence_digest,actor=actor))
            self._audit(s,'development.project.template_registered',template.project_id,actor)
            return dict(template_id=ident,template_digest=sha)
        return self._write(work)

    def disable_template(self,project_id,*,expected_revision,actor):
        valid_id(actor);valid_id(project_id)
        def work(s):
            row=s.scalar(select(Template).where(Template.project_id==project_id,Template.active==1))
            if row is None or row.revision!=expected_revision:raise ValueError('STALE_BINDING')
            row.active=0;self._audit(s,'development.project.template_disabled',project_id,actor)
        return self._write(work)

    def _template(self,s,row,subject=None):
        if row is None or not row.active or not row.observed_at<=self.r.now()<row.expires_at:raise ValueError('PROJECT_UNAVAILABLE')
        value=self.r._open(Template,row.template_id,'sealed_template',row.sealed_template)
        if digest(value)!=row.digest:raise ValueError('PROJECT_UNAVAILABLE')
        template=ProjectTemplate.model_validate(value)
        if subject is not None and subject not in template.allow_subjects:raise ValueError('PROJECT_UNAVAILABLE')
        return template

    def _state(self,s,request_id):
        request=s.scalar(select(Request).where(Request.request_id==request_id))
        wf=s.scalar(select(Workflow).where(Workflow.request_id==request_id))
        if request is None or wf is None:raise ValueError('STALE_BINDING')
        gate=s.scalar(select(Gate).where(Gate.workflow_id==wf.workflow_id))
        if gate is None:raise ValueError('STALE_BINDING')
        pending=s.scalar(select(Pending).where(Pending.workflow_id==wf.workflow_id))
        expected=dict(request_version=request.version,workflow_version=wf.version,gate_version=gate.version,
            gate_epoch=gate.epoch,generation=pending.generation if pending else 0)
        return request,wf,gate,pending,expected

    def _proposal(self,row):
        value=self.r._open(Proposal,row.proposal_id,'sealed_proposal',row.sealed_proposal)
        if digest(value['binding'])!=row.binding_digest or digest(value['scope'])!=value['binding']['scope_digest']:
            raise ValueError('STALE_BINDING')
        return dict(value,status='expired' if row.status=='pending' and row.expires_at<=self.r.now() else row.status)

    def read(self,request_id,*,subject,limit=50,cursor=None):
        valid_id(request_id);valid_id(subject)
        if type(limit)is not int or not 1<=limit<=50:raise ValueError('INVALID_ARGUMENT')
        import base64,hmac,json
        with self.r.sessions() as s:
            request,wf,gate,pending,expected=self._state(s,request_id)
            candidates=[]
            for row in s.scalars(select(Template).where(Template.active==1).order_by(Template.project_id)):
                try:t=self._template(s,row,subject)
                except ValueError:continue
                # Return only the current device's allowlist membership, not other subjects.
                value=t.model_dump(mode='json');value.pop('allow_subjects')
                candidates.append(dict(value,template_digest=row.digest))
            snapshot=digest(dict(subject=subject,candidates=candidates));offset=0
            if cursor:
                try:
                    raw,signature=cursor.split('.')
                    if not hmac.compare_digest(hmac.new(self.r.cursor_key,raw.encode(),'sha256').hexdigest(),signature):raise ValueError
                    c=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
                    if c['snapshot']!=snapshot or c['request_id']!=request_id:raise ValueError
                    offset=c['offset']
                    if type(offset)is not int or not 0<=offset<=len(candidates):raise ValueError
                except (ValueError,KeyError,TypeError):raise ValueError('CATALOG_CHANGED') from None
            next_cursor=None
            if offset+limit<len(candidates):
                raw=base64.urlsafe_b64encode(json.dumps(dict(snapshot=snapshot,request_id=request_id,offset=offset+limit)).encode()).decode().rstrip('=')
                next_cursor=raw+'.'+hmac.new(self.r.cursor_key,raw.encode(),'sha256').hexdigest()
            proposal=None
            if pending and pending.current_proposal_id:
                row=s.get(Proposal,pending.current_proposal_id)
                if row:
                    try:
                        self._template(s,s.get(Template,row.template_id),subject)
                        proposal=self._proposal(row)
                    except ValueError:pass
            grants=[]
            for row in s.scalars(select(Grant)):
                value=self.r._open(Grant,row.grant_id,'sealed_grant',row.sealed_grant)
                if value.get('request_id')!=request_id:continue
                policy=s.get(Policy,row.grant_id)
                if policy:
                    visible={candidate['project_id'] for candidate in candidates}
                    if row.project_id not in visible and value.get('subject')!=subject:continue
                grants.append(dict(grant_id=row.grant_id,version=row.version,digest=row.digest,
                    expires_at=value['expires_at'],revoked=bool(row.revoked),source='phone' if policy else 'operator',scope={k:value[k] for k in ('project_id','actions','budget_seconds','expires_at')}))
            return dict(schema_version='dal.project-authorization/1.0',request_id=request_id,workflow_id=wf.workflow_id,
                expected=expected,current_proposal=proposal,grants=grants,candidates=candidates[offset:offset+limit],
                next_cursor=next_cursor,complete=next_cursor is None,total=len(candidates),snapshot=snapshot,
                status=wf.status,phase=wf.phase,reason=wf.blocker_reason)

    def _command(self,kind,command_id,subject,payload,handler):
        valid_id(command_id);valid_id(subject)
        fingerprint=digest(dict(command_kind=kind,subject=subject,payload=payload))
        def work(s):
            old=s.scalar(select(Command).where(Command.command_id==command_id))
            if old:
                if old.body_sha256!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
                return self.r._open(Command,command_id,'sealed_result',old.sealed_result)
            try:
                with s.begin_nested():result=handler(s)
            except ValueError as exc:
                code=str(exc);reason=code if code in REFUSALS else 'INVALID_ARGUMENT'
                result=dict(status='refused',reason=reason,proposal_id=payload.get('proposal_id') if isinstance(payload,dict) else None)
            result=dict(schema_version='dal.project-authorization-receipt/1.0',command_id=command_id,
                command_kind=kind,request_body_sha256=fingerprint,subject=subject,**result)
            s.add(Command(command_id=command_id,body_sha256=fingerprint,
                sealed_result=self.r._seal(Command,command_id,'sealed_result',result)))
            self._audit(s,'development.project.'+kind+'.'+result['status'],command_id,subject)
            return result
        return self._write(work)

    def _quiescent(self,s,wf,gate,operation):
        if wf.status in ('cancelled','completed') or gate.mode in ('cancelled','delivered'):raise ValueError('STALE_BINDING')
        if operation=='create':
            if wf.phase!='project_routing' or wf.status!='blocked' or wf.blocker_reason!='PROJECT_AUTHORIZATION_REQUIRED' or gate.mode!='open':raise ValueError('STALE_BINDING')
        elif not (gate.mode=='open' and wf.status=='active' or gate.mode=='paused' or wf.status=='paused' or wf.status=='blocked' and wf.blocker_reason in ('PROJECT_AUTHORIZATION_REQUIRED','EXECUTION_BUDGET_EXHAUSTED')):
            raise ValueError('RECONCILIATION_REQUIRED')
        if s.scalar(select(Step.step_id).where(Step.workflow_id==wf.workflow_id,Step.status.in_(('dispatch_started','result_unknown'))).limit(1)):
            raise ValueError('RECONCILIATION_REQUIRED')
        if s.scalar(select(Effect.step_id).join(Step).where(Step.workflow_id==wf.workflow_id,Effect.status.in_(('started','unknown'))).limit(1)):
            raise ValueError('RECONCILIATION_REQUIRED')

    def _existing(self,s,request_id):
        result=[]
        for grant in s.scalars(select(Grant)):
            value=self.r._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)
            if digest(value)!=grant.digest:raise ValueError('RECONCILIATION_REQUIRED')
            if value.get('request_id')==request_id:result.append((grant,value))
        return result

    def preview(self,*,command_id,subject,payload):
        def work(s):
            body=Preview.model_validate(payload)
            request,wf,gate,pending,expected=self._state(s,body.request_id)
            if expected!=body.expected.model_dump():raise ValueError('STALE_BINDING')
            self._quiescent(s,wf,gate,body.operation)
            row=s.scalar(select(Template).where(Template.project_id==body.project_id,Template.revision==body.template_revision))
            template=self._template(s,row,subject)
            if row.digest!=body.template_digest:raise ValueError('STALE_BINDING')
            if not set(body.requested_actions)<=set(template.allowed_actions):raise ValueError('INVALID_ARGUMENT')
            if template.kind=='local_new' and not {'create','local_init'}<=set(body.requested_actions):raise ValueError('INVALID_ARGUMENT')
            if not within_limit(body.budget_seconds,template.max_budget_seconds) or not valid_expiry(body.grant_expires_at,template.max_validity_seconds,self.r.now()):
                raise ValueError('BUDGET_LIMIT_EXCEEDED')
            policy='github_issue' if 'remote_issue' in body.requested_actions else 'local_tracker'
            if policy not in template.registration_policies:raise ValueError('INVALID_ARGUMENT')
            existing=self._existing(s,body.request_id)
            grant_subject=subject
            if body.operation=='create':
                if existing or s.get(Binding,wf.workflow_id):raise ValueError('RECONCILIATION_REQUIRED')
            else:
                if len(existing)!=1:raise ValueError('RECONCILIATION_REQUIRED')
                grant,old=existing[0]
                if grant.revoked or (grant.grant_id,grant.version,grant.digest)!=(body.expected_grant.id,body.expected_grant.version,body.expected_grant.digest):raise ValueError('STALE_BINDING')
                grant_subject=old['subject']
                if any(old[k]!=v for k,v in dict(project_id=template.project_id,root=template.root,kind=template.kind,
                    registration_policy=policy).items()):raise ValueError('INVALID_ARGUMENT')
                if old['remote_repository']!=template.remote_repository:
                    if not (body.operation=='amend' and old['remote_repository'] is None
                        and template.remote_repository is not None and wf.phase=='project_routing'
                        and s.get(Binding,wf.workflow_id) is None):raise ValueError('INVALID_ARGUMENT')
                if not set(old['actions'])<=set(body.requested_actions) or body.operation=='renew' and set(old['actions'])!=set(body.requested_actions):raise ValueError('INVALID_ARGUMENT')
                if not extends_limit(body.budget_seconds,old['budget_seconds']) or expiry_projection(body.grant_expires_at)<grant.expires_at:raise ValueError('BUDGET_LIMIT_EXCEEDED')
                association=s.get(Policy,grant.grant_id)
                if association is None:raise ValueError('RECONCILIATION_REQUIRED')
                prior=self.r._open(Template,association.template_id,'sealed_template',s.get(Template,association.template_id).sealed_template)
                if any(prior[k]!=getattr(template,k) for k in ('base_sha','base_branch','directory_identity_digest')):raise ValueError('INVALID_ARGUMENT')
            scope=dict(project_id=template.project_id,subject=grant_subject,root=template.root,kind=template.kind,
                display_name=template.display_name,actions=sorted(body.requested_actions),budget_seconds=body.budget_seconds,
                expires_at=body.grant_expires_at.isoformat() if body.grant_expires_at is not None else None,registration_policy=policy,remote_repository=template.remote_repository,
                base_sha=template.base_sha,base_branch=template.base_branch,branch='refs/heads/codex/dal-'+wf.workflow_id,
                template_digest=row.digest,budget_policy_ref=template.budget_policy_ref)
            generation=expected['generation']+1;ident=new_id();expires=min(self.r.now()+timedelta(hours=24),expiry_projection(body.grant_expires_at))
            binding=dict(schema='dal.project-authorization-binding/1.0',request_id=request.request_id,workflow_id=wf.workflow_id,
                request_version=request.version,workflow_version=wf.version,gate_version=gate.version,gate_epoch=gate.epoch,
                authorization_generation=generation,operation=body.operation,expected_grant=body.expected_grant.model_dump() if body.expected_grant else None,
                proposal_id=ident,revision=generation,project_id=template.project_id,template_revision=template.revision,
                template_digest=row.digest,scope_digest=digest(scope),worker_configuration_digest=template.worker_configuration_digest,
                policy_digest=row.digest,expires_at=expires.isoformat())
            value=dict(proposal_id=ident,revision=generation,binding=binding,binding_digest=digest(binding),scope=scope,expires_at=expires.isoformat())
            if pending and pending.current_proposal_id:
                old=s.get(Proposal,pending.current_proposal_id)
                if old and old.status=='pending':old.status='superseded'
            if pending is None:
                pending=Pending(workflow_id=wf.workflow_id);s.add(pending)
            pending.request_version=request.version;pending.status='pending';pending.generation=generation
            pending.current_proposal_id=ident;pending.expires_at=expires
            pending.sealed_scope=self.r._seal(Pending,wf.workflow_id,'sealed_scope',scope)
            s.add(Proposal(proposal_id=ident,workflow_id=wf.workflow_id,template_id=row.template_id,generation=generation,
                status='pending',binding_digest=digest(binding),sealed_proposal=self.r._seal(Proposal,ident,'sealed_proposal',value),expires_at=expires))
            self.r._append_event(s,request,'workflow.authorization_proposed',dict(summary='请在手机核对并授权开发项目。',authorization=value))
            return dict(status='accepted',reason=None,proposal_id=ident,binding_digest=digest(binding),scope_digest=digest(scope),proposal=dict(value,status='pending'))
        return self._command('authorization_preview',command_id,subject,payload,work)

    def approve(self,*,command_id,subject,payload):
        def work(s):
            body=Approval.model_validate(payload);now=self.r.now()
            if not body.confirmed_at<=now<body.confirmation_expires_at<=body.confirmed_at+timedelta(minutes=15):raise ValueError('PROPOSAL_EXPIRED')
            row=s.scalar(select(Proposal).where(Proposal.proposal_id==body.proposal_id))
            if row is None or row.status!='pending' or row.binding_digest!=body.binding_digest:raise ValueError('STALE_BINDING')
            if row.expires_at<=now:raise ValueError('PROPOSAL_EXPIRED')
            value=self._proposal(row);bound=value['binding'];scope=value['scope']
            request,wf,gate,pending,expected=self._state(s,bound['request_id'])
            want=dict(request_version=bound['request_version'],workflow_version=bound['workflow_version'],gate_version=bound['gate_version'],gate_epoch=bound['gate_epoch'],generation=bound['authorization_generation'])
            if expected!=want or pending.current_proposal_id!=row.proposal_id:raise ValueError('STALE_BINDING')
            self._quiescent(s,wf,gate,bound['operation'])
            template_row=s.get(Template,row.template_id);template=self._template(s,template_row,subject)
            if template_row.digest!=bound['template_digest'] or not within_limit(scope['budget_seconds'],template.max_budget_seconds):raise ValueError('STALE_BINDING')
            existing=self._existing(s,request.request_id)
            if bound['operation']=='create':
                if existing or s.get(Binding,wf.workflow_id):raise ValueError('RECONCILIATION_REQUIRED')
                grant=Grant(grant_id=new_id(),version=1,project_id=scope['project_id'],subject=scope['subject'],revoked=0)
                s.add(grant)
            else:
                if len(existing)!=1:raise ValueError('RECONCILIATION_REQUIRED')
                grant,_=existing[0];old=bound['expected_grant']
                if grant.revoked or (grant.grant_id,grant.version,grant.digest)!=(old['id'],old['version'],old['digest']):raise ValueError('STALE_BINDING')
                bindings=list(s.scalars(select(Binding).where(Binding.grant_id==grant.grant_id)))
                if len(bindings)>1 or any(b.workflow_id!=wf.workflow_id for b in bindings):raise ValueError('RECONCILIATION_REQUIRED')
                grant.version+=1
            from personal_agent_dal.timeline.operator import Authorization
            grant_value=Authorization(grant_id=grant.grant_id,request_id=request.request_id,approval_evidence_ref=command_id,
                **{k:scope[k] for k in ('project_id','subject','root','kind','display_name','actions','budget_seconds','expires_at','registration_policy','remote_repository')}).model_dump(mode='json')
            grant.digest=digest(grant_value);grant.expires_at=expiry_projection(scope['expires_at'])
            grant.sealed_grant=self.r._seal(Grant,grant.grant_id,'sealed_grant',grant_value)
            s.flush()
            association=s.get(Policy,grant.grant_id)
            if association is None:
                association=Policy(grant_id=grant.grant_id,workflow_id=wf.workflow_id);s.add(association)
            association.template_id=row.template_id;association.template_digest=template_row.digest;association.proposal_id=row.proposal_id
            binding=s.get(Binding,wf.workflow_id)
            if binding:
                binding.grant_version=grant.version
                data=self.r._open(Binding,wf.workflow_id,'sealed_binding',binding.sealed_binding);data['grant_digest']=grant.digest
                binding.sealed_binding=self.r._seal(Binding,wf.workflow_id,'sealed_binding',data)
            if bound['operation']!='create':
                for step in s.scalars(select(Step).where(Step.workflow_id==wf.workflow_id,Step.status=='prepared')):
                    step.status='retired'
                    execution=s.scalar(select(Execution).where(Execution.step_id==step.step_id))
                    if execution and execution.started_at is None:execution.charged_seconds=0
                old_decisions=list(s.scalars(select(Decision).where(Decision.workflow_id==wf.workflow_id,Decision.status=='pending')))
                for decision in old_decisions:decision.status='superseded'
                wf.version+=1
                # Rebind pending documents without changing any pause or gate. A
                # paused workflow still requires explicit recovery before consumption.
                for decision in old_decisions:
                    decision_binding=self.r._open(Decision,decision.decision_id,'sealed_binding',decision.sealed_binding)
                    replacement_id=new_id();expires=now+timedelta(hours=24)
                    decision_binding.update(decision_id=replacement_id,workflow_version=wf.version,
                        decision_version=1,gate_version=gate.version,gate_epoch=gate.epoch,expires_at=expires.isoformat())
                    replacement=Decision(decision_id=replacement_id,workflow_id=wf.workflow_id,kind=decision.kind,
                        version=1,binding_digest=digest(decision_binding),status='pending',expires_at=expires,
                        sealed_binding=self.r._seal(Decision,replacement_id,'sealed_binding',decision_binding))
                    s.add(replacement)
                    from personal_agent_dal.timeline.decisions import DecisionService
                    self.r._append_event(s,request,'decision.requested',dict(
                        summary='授权范围已更新，请重新核对待审批文档。',status=wf.status,phase=wf.phase,
                        artifact=dict(artifact_id=decision_binding['artifact_id'],revision=decision_binding['artifact_revision'],
                            kind={'prd':'prd','project_selection':'project_route','delivery':'delivery'}[decision.kind]),
                        decision=DecisionService._projection(replacement,decision_binding),
                        invalidated_decision_ids=[decision.decision_id]))
                # Resume only the authority blocker; preserve human pause and other gates.
                if gate.mode=='open' and wf.status=='blocked' and wf.blocker_reason in ('PROJECT_AUTHORIZATION_REQUIRED','EXECUTION_BUDGET_EXHAUSTED'):
                    wf.status='active';wf.blocker_reason=None
            row.status='granted';pending.status='granted';pending.grant_id=grant.grant_id
            self.r._append_event(s,request,'workflow.authorization_granted',dict(summary='项目授权已记录，正在重新校验执行条件。',proposal_id=row.proposal_id,grant_id=grant.grant_id))
            return dict(status='accepted',reason=None,proposal_id=row.proposal_id,binding_digest=body.binding_digest,
                scope_digest=bound['scope_digest'],grant_id=grant.grant_id,grant_version=grant.version,grant_digest=grant.digest,
                operation=bound['operation'],authorization_applied=True,workflow_resumed=wf.status=='active',accepted_at=now.isoformat())
        return self._command('authorization_approve',command_id,subject,payload,work)

    def proposal_status(self,proposal_id,*,subject):
        valid_id(proposal_id);valid_id(subject)
        unavailable=dict(proposal_id=proposal_id,valid=False,proposal=None)
        with self.r.sessions() as s:
            row=s.scalar(select(Proposal).where(Proposal.proposal_id==proposal_id))
            if row is None:return unavailable
            try:
                self._template(s,s.get(Template,row.template_id),subject)
                proposal=self._proposal(row);b=proposal['binding']
                _,wf,gate,pending,e=self._state(s,b['request_id'])
                valid=(row.status=='pending' and row.expires_at>self.r.now() and pending.current_proposal_id==row.proposal_id
                    and e==dict(request_version=b['request_version'],workflow_version=b['workflow_version'],gate_version=b['gate_version'],gate_epoch=b['gate_epoch'],generation=b['authorization_generation'])
                    and wf.status not in ('cancelled','completed') and gate.mode not in ('cancelled','delivered'))
                return dict(proposal_id=proposal_id,valid=valid,proposal=proposal)
            except ValueError:return unavailable

    def receipt(self,command_id,*,subject):
        valid_id(command_id);valid_id(subject)
        with self.r.sessions() as s:
            row=s.scalar(select(Command).where(Command.command_id==command_id))
            if row is None:return dict(command_id=command_id,receipt=None)
            value=self.r._open(Command,row.command_id,'sealed_result',row.sealed_result)
            if value.get('subject')!=subject or value.get('command_kind') not in ('authorization_preview','authorization_approve'):
                return dict(command_id=command_id,receipt=None)
            return dict(command_id=command_id,receipt=value)


def execution_policy(requests,session,grant):
    """Current template authority is re-read at every dispatch/publication fence."""
    association=session.scalar(select(Policy).where(Policy.grant_id==grant.grant_id))
    if association is None:return None  # Legacy operator evidence is not a phone approval.
    row=session.scalar(select(Template).where(Template.template_id==association.template_id))
    try:template=ProjectAuthorizationService(requests)._template(session,row)
    except ValueError:raise ValueError('PROJECT_AUTHORIZATION_REQUIRED') from None
    if row.digest!=association.template_digest:raise ValueError('PROJECT_AUTHORIZATION_REQUIRED')
    scope=requests._open(Grant,grant.grant_id,'sealed_grant',grant.sealed_grant)
    if (digest(scope)!=grant.digest or grant.revoked or grant.expires_at<=requests.now()
        or scope['subject']!=grant.subject or grant.project_id!=template.project_id
        or any(scope[key]!=getattr(template,key) for key in ('project_id','root','kind','remote_repository'))
        or not set(scope['actions']).issubset(template.allowed_actions)
        or not within_limit(scope['budget_seconds'],template.max_budget_seconds)
        or scope['registration_policy'] not in template.registration_policies):
        raise ValueError('PROJECT_AUTHORIZATION_REQUIRED')
    return dict(template_digest=row.digest,project_id=template.project_id,worker_id=template.worker_id,
        worker_configuration_digest=template.worker_configuration_digest,directory_identity_digest=template.directory_identity_digest,
        root=template.root,kind=template.kind,base_sha=template.base_sha,base_branch=template.base_branch,
        branch='refs/heads/codex/dal-'+association.workflow_id)
