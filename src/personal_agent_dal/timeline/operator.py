"""Structured operator configuration; never reachable as an Agent tool."""
from typing import Literal
from datetime import datetime
from pathlib import PurePath
from fastapi import Depends, HTTPException
from pydantic import Field, StrictInt
from typing import Annotated
from sqlalchemy import select
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.machine.workflow_selection import Closed, Id
from personal_agent_dal.timeline.roles import Configuration
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.storage.timeline_models import DevelopmentProjectAuthorization as Grant
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_core.ids import new_id


class Bind(Closed):
    scope: Literal['system','project','task']
    scope_id: Id
    revision_id: Id
    expected_version: Annotated[StrictInt, Field(ge=0)]


class Authorization(Closed):
    grant_id: Id
    request_id: Id
    project_id: Id
    subject: Id
    approval_evidence_ref: Id
    root: str
    kind: Literal['existing','local_new']
    display_name: Annotated[str,Field(min_length=1,max_length=128)]
    actions: list[Literal['read','write','create','local_init','remote_issue','push','pr']]
    budget_seconds: Annotated[StrictInt,Field(ge=1,le=86400)]
    expires_at: datetime
    registration_policy: Literal['local_tracker','github_issue']
    remote_repository: str | None = None


def register_authorization(requests, body, *, actor):
    import re
    if (set(body.actions)&{'remote_issue','push','pr'} or body.registration_policy=='github_issue') and (not isinstance(body.remote_repository,str) or re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',body.remote_repository) is None):
        raise ValueError('REMOTE_REPOSITORY_REQUIRED')
    value=body.model_dump(mode='json');sha=digest(value)
    path=PurePath(body.root)
    if (not actor or not path.is_absolute() or '..' in path.parts or body.expires_at.tzinfo is None
        or body.expires_at<=requests.now() or not body.subject.startswith('device:')
        or not body.actions or len(body.actions)!=len(set(body.actions)) or 'read' not in body.actions
        or (body.kind=='local_new' and not {'create','local_init'}<=set(body.actions))):
        raise ValueError('PROJECT_AUTHORIZATION_INVALID')
    def work(s):
        row=s.scalar(select(Grant).where(Grant.grant_id==body.grant_id))
        if row:
            if row.digest!=sha or row.revoked:raise ValueError('IDEMPOTENCY_CONFLICT')
            return dict(grant_id=row.grant_id,version=row.version,digest=row.digest)
        s.add(Grant(grant_id=body.grant_id,version=1,project_id=body.project_id,subject=body.subject,
            sealed_grant=requests._seal(Grant,body.grant_id,'sealed_grant',value),digest=sha,
            expires_at=body.expires_at,revoked=0))
        s.flush()
        from personal_agent_dal.storage.timeline_models import DevelopmentAuthorizationRequest,DevelopmentWorkflow,DevelopmentRequest
        wf=s.scalar(select(DevelopmentWorkflow).where(DevelopmentWorkflow.request_id==body.request_id))
        pending=s.get(DevelopmentAuthorizationRequest,wf.workflow_id) if wf else None
        if pending and pending.status=='pending' and pending.expires_at>requests.now():
            if pending.request_version!=s.get(DevelopmentRequest,body.request_id).version:raise ValueError('STALE_AUTHORIZATION_REQUEST')
            pending.status='granted';pending.grant_id=body.grant_id
            requests._append_event(s,s.get(DevelopmentRequest,body.request_id),'workflow.authorization_granted',dict(summary='项目授权已记录，等待重新校验与项目选择。'))
        append_audit_event(s,event_id=new_id(),trace_id=body.request_id,event_type='development.project.authorized',
            redacted_summary='structured authorization registered',now=requests.now())
        return dict(grant_id=body.grant_id,version=1,digest=sha)
    with requests.sessions() as s:return run_write_transaction(s,lambda:work(s))


def mount_routes(app, endpoint, service):
    from personal_agent_dal.service.app import _OperatorAuth
    def ready():
        if endpoint is None:raise HTTPException(503,'DAL_TIMELINE_UNAVAILABLE')
    @app.post('/operator/development/role-configurations')
    def register(body:Configuration,actor=Depends(_OperatorAuth(service,'control'))):
        ready()
        try:return dict(revision_id=endpoint.roles.register(body.model_dump()))
        except ValueError:raise HTTPException(409,'ROLE_CONFIGURATION_REFUSED') from None
    @app.post('/operator/development/role-bindings')
    def bind(body:Bind,actor=Depends(_OperatorAuth(service,'control'))):
        ready()
        try:endpoint.roles.bind(**body.model_dump())
        except ValueError:raise HTTPException(409,'ROLE_BINDING_REFUSED') from None
        return dict(status='bound',version=body.expected_version+1)
    class Renewal(Closed):
        authorization: Authorization
        expected_version: Annotated[StrictInt,Field(ge=1)]
    @app.post('/operator/development/project-authorizations/renew')
    def renew(body:Renewal,actor=Depends(_OperatorAuth(service,'control'))):
        ready()
        try:return renew_authorization(endpoint.requests,body.authorization,expected_version=body.expected_version,actor=actor)
        except ValueError:raise HTTPException(409,'PROJECT_AUTHORIZATION_RENEWAL_REFUSED') from None
    @app.post('/operator/development/project-authorizations')
    def authorize(body:Authorization,actor=Depends(_OperatorAuth(service,'control'))):
        ready()
        try:return register_authorization(endpoint.requests,body,actor=actor)
        except ValueError:raise HTTPException(409,'PROJECT_AUTHORIZATION_REFUSED') from None


def renew_authorization(requests,body,*,expected_version,actor):
    """Explicit new approval extends only time/budget, never project or actions."""
    from personal_agent_dal.storage.timeline_models import (
        DevelopmentProjectBinding as Binding,DevelopmentWorkflow as Workflow,
        DevelopmentDriverStep as Step,DevelopmentExecution as Execution,DevelopmentRemoteEffect as Effect,
    )
    value=body.model_dump(mode='json');sha=digest(value)
    if not actor or body.expires_at<=requests.now():raise ValueError('PROJECT_AUTHORIZATION_INVALID')
    def work(s):
        row=s.get(Grant,body.grant_id)
        if row is None or row.revoked:raise ValueError('PROJECT_AUTHORIZATION_INVALID')
        if row.digest==sha and row.version==expected_version+1:return dict(grant_id=row.grant_id,version=row.version,digest=row.digest)
        if row.version!=expected_version:raise ValueError('STALE_BINDING')
        old=requests._open(Grant,row.grant_id,'sealed_grant',row.sealed_grant)
        mutable={'expires_at','budget_seconds','approval_evidence_ref'}
        if ({k:v for k,v in old.items() if k not in mutable}!={k:v for k,v in value.items() if k not in mutable}
            or old['approval_evidence_ref']==value['approval_evidence_ref'] or body.budget_seconds<old['budget_seconds']
            or body.expires_at<row.expires_at):raise ValueError('RENEWAL_SCOPE_CHANGED')
        binding=s.scalar(select(Binding).where(Binding.grant_id==row.grant_id))
        if binding:
            wf=s.get(Workflow,binding.workflow_id)
            if wf.status in ('cancelled','completed'):raise ValueError('STALE_BINDING')
            if s.scalar(select(Step.step_id).where(Step.workflow_id==wf.workflow_id,Step.status.in_(('dispatch_started','result_unknown'))).limit(1)):
                raise ValueError('RECONCILIATION_REQUIRED')
            if s.scalar(select(Effect.step_id).join(Step).where(Step.workflow_id==wf.workflow_id,Effect.status.in_(('started','unknown'))).limit(1)):
                raise ValueError('RECONCILIATION_REQUIRED')
            for step in s.scalars(select(Step).where(Step.workflow_id==wf.workflow_id,Step.status=='prepared')):
                step.status='retired'
                execution=s.scalar(select(Execution).where(Execution.step_id==step.step_id))
                if execution:execution.charged_seconds=0
            binding.grant_version=row.version+1
            bound=requests._open(Binding,wf.workflow_id,'sealed_binding',binding.sealed_binding)
            bound['grant_digest']=sha
            binding.sealed_binding=requests._seal(Binding,wf.workflow_id,'sealed_binding',bound)
            if wf.status=='blocked' and wf.blocker_reason in ('EXECUTION_BUDGET_EXHAUSTED','PROJECT_AUTHORIZATION_REQUIRED'):
                wf.status='active';wf.blocker_reason=None;wf.version+=1
        row.version+=1;row.digest=sha;row.expires_at=body.expires_at
        row.sealed_grant=requests._seal(Grant,row.grant_id,'sealed_grant',value)
        append_audit_event(s,event_id=new_id(),trace_id=body.request_id,event_type='development.project.authorization_renewed',
            redacted_summary='time and total budget renewed by explicit approval',now=requests.now())
        return dict(grant_id=row.grant_id,version=row.version,digest=row.digest)
    with requests.sessions() as s:return run_write_transaction(s,lambda:work(s))
