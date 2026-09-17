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


def register_authorization(requests, body, *, actor):
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
    @app.post('/operator/development/project-authorizations')
    def authorize(body:Authorization,actor=Depends(_OperatorAuth(service,'control'))):
        ready()
        try:return register_authorization(endpoint.requests,body,actor=actor)
        except ValueError:raise HTTPException(409,'PROJECT_AUTHORIZATION_REFUSED') from None
