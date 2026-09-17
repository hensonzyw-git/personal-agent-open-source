"""Versioned roles; saving configuration never grants runtime admission."""
import json
from typing import Literal, Annotated
from pydantic import Field
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_dal.machine.workflow_selection import Closed, Id, Version
from personal_agent_dal.storage.timeline_models import RoleConfigurationRevision, RoleConfigurationBinding, DevelopmentRoleSnapshot
from personal_agent_dal.timeline.requests import digest


class RoleV3(Closed):
    runtime_ref: Id
    provider_ref: Id
    model: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_./:-]*$')]
    reasoning: Literal['none','low','medium','high','xhigh','max','ultra']
    placement_ref: Id
    permission: Literal['read_only','workspace_write']
    billing: Literal['subscription','api']


class Configuration(Closed):
    contract_version: Literal['dal.role-contract/3.0']
    configuration_id: Id
    revision: Version
    roles: dict[Literal['planner','coder','reviewer'],RoleV3]


class RoleService:
    def __init__(self,requests,registry):
        self.requests=requests
        self.registry=registry

    def validate(self,body):
        value=Configuration.model_validate(body).model_dump()
        roles=value['roles']
        if set(roles)!={'planner','coder','reviewer'}:raise ValueError('ROLE_UNAVAILABLE')
        for name,role in roles.items():
            if role['permission']!=('workspace_write' if name=='coder' else 'read_only'):raise ValueError('ROLE_PERMISSION_INVALID')
            matches=[r for r in self.registry if {k:v for k,v in r.items() if k!='adapter'}==role and r.get('adapter') in ('codex_cli','claude_code')]
            if len(matches)!=1:raise ValueError('ROLE_UNAVAILABLE')
        for name in ('planner','coder'):
            if (roles[name]['provider_ref'],roles[name]['model'])==(roles['reviewer']['provider_ref'],roles['reviewer']['model']):raise ValueError('REVIEW_NOT_INDEPENDENT')
        return value

    def register(self,body):
        body=self.validate(body);sha=digest(body)
        def work(s):
            old=s.scalar(select(RoleConfigurationRevision).where(RoleConfigurationRevision.configuration_id==body['configuration_id'],RoleConfigurationRevision.revision==body['revision']))
            if old:
                if old.digest!=sha:raise ValueError('IDEMPOTENCY_CONFLICT')
                return old.revision_id
            id=new_id();s.add(RoleConfigurationRevision(revision_id=id,configuration_id=body['configuration_id'],revision=body['revision'],body=canonical_json(body),digest=sha));return id
        with self.requests.sessions() as s:return run_write_transaction(s,lambda:work(s))

    def bind(self,*,scope,scope_id,revision_id,expected_version):
        if scope not in ('system','project','task') or (scope=='system' and scope_id!='default'):raise ValueError('INVALID_ARGUMENT')
        def work(s):
            if s.get(RoleConfigurationRevision,revision_id) is None:raise ValueError('ROLE_UNAVAILABLE')
            row=s.scalar(select(RoleConfigurationBinding).where(RoleConfigurationBinding.scope==scope,RoleConfigurationBinding.scope_id==scope_id))
            if (row.version if row else 0)!=expected_version:raise ValueError('STALE_BINDING')
            if row:row.revision_id,row.version=revision_id,row.version+1
            else:s.add(RoleConfigurationBinding(scope=scope,scope_id=scope_id,revision_id=revision_id,version=1))
        with self.requests.sessions() as s:return run_write_transaction(s,lambda:work(s))

    def resolve(self,*,project_id=None,workflow_id=None):
        with self.requests.sessions() as s:
            selected=None;source=None
            for scope,id in [('system','default'),('project',project_id),('task',workflow_id)]:
                if id is None:continue
                binding=s.scalar(select(RoleConfigurationBinding).where(RoleConfigurationBinding.scope==scope,RoleConfigurationBinding.scope_id==id))
                if binding:selected=s.get(RoleConfigurationRevision,binding.revision_id);source=scope
            if selected is None:return dict(roles={},available=False,reason='ROLE_UNAVAILABLE',source=None)
            body=json.loads(selected.body)
            if digest(body)!=selected.digest:raise ValueError('ROLE_CONFIG_INTEGRITY')
            self.validate(body)
            return dict(**body,digest=selected.digest,source=source,available=False,reason='RUNTIME_ADMISSION_REQUIRED')

    def snapshot(self,**scope):
        resolved=self.resolve(**scope)
        if not resolved['roles']:raise ValueError('ROLE_UNAVAILABLE')
        body={k:resolved[k] for k in ('contract_version','configuration_id','revision','roles','digest','source')}
        sha=digest(body)
        def work(s):
            row=s.scalar(select(DevelopmentRoleSnapshot).where(DevelopmentRoleSnapshot.digest==sha))
            if row is None:
                row=DevelopmentRoleSnapshot(snapshot_id=new_id(),digest=sha,body=canonical_json(body));s.add(row)
            return dict(**body,snapshot_id=row.snapshot_id,snapshot_digest=sha)
        with self.requests.sessions() as s:return run_write_transaction(s,lambda:work(s))
