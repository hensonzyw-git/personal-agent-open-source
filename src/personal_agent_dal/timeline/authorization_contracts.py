"""Closed phone authorization contracts; never exposed as model tools."""
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Literal
import re
from pydantic import BeforeValidator, Field, StrictInt, model_validator
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest


def aware_time(value):
    if isinstance(value,str):
        if 'T' not in value:raise ValueError('INVALID_ARGUMENT')
        value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if not isinstance(value,datetime) or value.tzinfo is None:raise ValueError('INVALID_ARGUMENT')
    return value.astimezone(timezone.utc)

Time=Annotated[datetime,BeforeValidator(aware_time)]
Positive=Annotated[StrictInt,Field(ge=1)]
Action=Literal['read','write','create','local_init','remote_issue','push','pr']
Actions=Annotated[list[Action],Field(min_length=1,max_length=7)]


# Fixed empty Git commit: the phone approves the same base before bootstrap.
import hashlib
LOCAL_BOOTSTRAP_BODY = (b'tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n'
    b'author DAL <dal@localhost> 946684800 +0000\n'
    b'committer DAL <dal@localhost> 946684800 +0000\n\nDAL local project bootstrap\n')
LOCAL_BOOTSTRAP_SHA = hashlib.sha1(b'commit '+str(len(LOCAL_BOOTSTRAP_BODY)).encode()+b'\0'+LOCAL_BOOTSTRAP_BODY).hexdigest()


class ProjectTemplate(Closed):
    project_id: Id
    revision: Positive
    display_name: Annotated[str,Field(strict=True,min_length=1,max_length=128)]
    kind: Literal['existing','local_new']
    root: Annotated[str,Field(strict=True,min_length=2,max_length=4096)]
    remote_repository: str|None
    allowed_actions: Actions
    registration_policies: Annotated[list[Literal['local_tracker','github_issue']],Field(min_length=1,max_length=2)]
    max_budget_seconds: Annotated[StrictInt,Field(ge=1,le=86400)]
    max_validity_seconds: Annotated[StrictInt,Field(ge=1,le=31536000)]
    allow_subjects: Annotated[list[Id],Field(min_length=1,max_length=128)]
    worker_id: Id
    worker_configuration_digest: Digest
    directory_identity_digest: Digest
    base_sha: Annotated[str,Field(strict=True,pattern=r'^[0-9a-f]{40}$')]
    base_branch: Annotated[str,Field(strict=True,min_length=1,max_length=128)]
    budget_policy_ref: Id

    @model_validator(mode='after')
    def valid(self):
        path=PurePosixPath(self.root)
        if not path.is_absolute() or '..' in path.parts or str(path)!=self.root or self.root=='/':raise ValueError('INVALID_ARGUMENT')
        for values in (self.allowed_actions,self.registration_policies,self.allow_subjects):
            if len(values)!=len(set(values)):raise ValueError('INVALID_ARGUMENT')
        if 'read' not in self.allowed_actions or not all(s.startswith('device:') for s in self.allow_subjects):raise ValueError('INVALID_ARGUMENT')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*',self.base_branch) or '..' in self.base_branch
            or self.base_branch.endswith(('/', '.', '.lock')) or '//' in self.base_branch):raise ValueError('INVALID_ARGUMENT')
        if self.remote_repository is not None and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',self.remote_repository):raise ValueError('INVALID_ARGUMENT')
        if (set(self.allowed_actions)&{'remote_issue','push','pr'} or 'github_issue' in self.registration_policies) and not self.remote_repository:raise ValueError('INVALID_ARGUMENT')
        if 'local_tracker' not in self.registration_policies:raise ValueError('INVALID_ARGUMENT')
        if ('push' in self.allowed_actions)!=('pr' in self.allowed_actions):raise ValueError('INVALID_ARGUMENT')
        if self.kind=='local_new' and (not {'create','local_init'}<=set(self.allowed_actions) or self.base_sha!=LOCAL_BOOTSTRAP_SHA):raise ValueError('INVALID_ARGUMENT')
        return self


class Expected(Closed):
    request_version: Positive
    workflow_version: Positive
    gate_version: Positive
    gate_epoch: Positive
    generation: Annotated[StrictInt,Field(ge=0)]


class ExpectedGrant(Closed):
    id: Id
    version: Positive
    digest: Digest


class Preview(Closed):
    request_id: Id
    operation: Literal['create','renew','amend']
    project_id: Id
    template_revision: Positive
    template_digest: Digest
    requested_actions: Actions
    budget_seconds: Annotated[StrictInt,Field(ge=1,le=86400)]
    grant_expires_at: Time
    expected: Expected
    expected_grant: ExpectedGrant|None

    @model_validator(mode='after')
    def valid(self):
        if len(self.requested_actions)!=len(set(self.requested_actions)) or 'read' not in self.requested_actions:raise ValueError('INVALID_ARGUMENT')
        if ('push' in self.requested_actions)!=('pr' in self.requested_actions):raise ValueError('INVALID_ARGUMENT')
        if (self.operation=='create')!=(self.expected_grant is None):raise ValueError('INVALID_ARGUMENT')
        return self


class Approval(Closed):
    proposal_id: Id
    binding_digest: Digest
    source_action_ref: Id
    context_id: Id
    context_jti: Id
    confirmed_at: Time
    confirmation_expires_at: Time
    key_thumbprint: Annotated[str,Field(strict=True,min_length=1,max_length=128)]


REFUSALS=frozenset({'INVALID_ARGUMENT','SCOPE_REQUIRED','DEVICE_INACTIVE','DEVICE_IDENTITY_MISMATCH',
    'PROJECT_UNAVAILABLE','CATALOG_CHANGED','STALE_BINDING','PROPOSAL_EXPIRED','CONTEXT_INVALID',
    'IDEMPOTENCY_CONFLICT','BUDGET_LIMIT_EXCEEDED','RECONCILIATION_REQUIRED','CAPABILITY_UNAVAILABLE'})


class AuthorizationCommand(Closed):
    schema_version: Literal['dal.timeline/1.0']
    command_id: Id
    command_kind: Literal['authorization_preview','authorization_approve']
    payload: dict


class AuthorizationRead(Closed):
    request_id: Id
    limit: Annotated[StrictInt,Field(ge=1,le=50)]=50
    cursor: Annotated[str,Field(max_length=2048)]|None=None


class AuthorizationStatus(Closed):
    proposal_id: Id


class AuthorizationReceiptRead(Closed):
    command_id: Id


def validate_receipt(value,*,kind,command_id,subject,payload):
    """Validate the union before PA persists a success from a signed peer."""
    from personal_agent_dal.timeline.requests import digest,valid_id
    common={'schema_version','command_id','command_kind','request_body_sha256','subject','status','reason','proposal_id'}
    if not isinstance(value,dict) or not common<=set(value):raise ValueError('RESPONSE_INVALID')
    if (value['schema_version']!='dal.project-authorization-receipt/1.0' or value['command_id']!=command_id
        or value['command_kind']!=kind or value['subject']!=subject
        or value['request_body_sha256']!=digest(dict(command_kind=kind,subject=subject,payload=payload))):raise ValueError('RESPONSE_INVALID')
    if value['status']=='refused':
        if set(value)!=common or value['reason'] not in REFUSALS:raise ValueError('RESPONSE_INVALID')
        if value['proposal_id'] is not None:valid_id(value['proposal_id'])
        return value
    if value['status']!='accepted' or value['reason'] is not None:raise ValueError('RESPONSE_INVALID')
    valid_id(value['proposal_id'])
    from pydantic import TypeAdapter
    for k in ('binding_digest','scope_digest'):TypeAdapter(Digest).validate_python(value.get(k))
    if kind=='authorization_preview':
        if set(value)!=common|{'binding_digest','scope_digest','proposal'}:raise ValueError('RESPONSE_INVALID')
        p=validate_proposal(value['proposal'])
        if (not isinstance(p,dict) or set(p)!={'proposal_id','revision','binding','binding_digest','scope','expires_at','status'}
            or p['proposal_id']!=value['proposal_id'] or p['binding_digest']!=value['binding_digest']
            or digest(p['binding'])!=value['binding_digest'] or digest(p['scope'])!=value['scope_digest']
            or p['binding']['scope_digest']!=value['scope_digest'] or p['status']!='pending'):raise ValueError('RESPONSE_INVALID')
    else:
        extra={'binding_digest','scope_digest','grant_id','grant_version','grant_digest','operation','authorization_applied','workflow_resumed','accepted_at'}
        if (set(value)!=common|extra or value['proposal_id']!=payload['proposal_id'] or value['binding_digest']!=payload['binding_digest']
            or value['authorization_applied'] is not True or type(value['workflow_resumed']) is not bool
            or value['operation'] not in ('create','renew','amend')):raise ValueError('RESPONSE_INVALID')
        valid_id(value['grant_id']);TypeAdapter(Positive).validate_python(value['grant_version'])
        TypeAdapter(Digest).validate_python(value['grant_digest']);aware_time(value['accepted_at'])
    return value


class FrozenScope(Closed):
    project_id: Id
    subject: Id
    root: str
    kind: Literal['existing','local_new']
    display_name: str
    actions: Actions
    budget_seconds: Annotated[StrictInt,Field(ge=1,le=86400)]
    expires_at: Time
    registration_policy: Literal['local_tracker','github_issue']
    remote_repository: str|None
    base_sha: Annotated[str,Field(pattern=r'^[0-9a-f]{40}$')]
    base_branch: str
    branch: str
    template_digest: Digest
    budget_policy_ref: Id


class ProposalBinding(Closed):
    schema_version: Literal['dal.project-authorization-binding/1.0']=Field(alias='schema')
    request_id: Id
    workflow_id: Id
    request_version: Positive
    workflow_version: Positive
    gate_version: Positive
    gate_epoch: Positive
    authorization_generation: Positive
    operation: Literal['create','renew','amend']
    expected_grant: ExpectedGrant|None
    proposal_id: Id
    revision: Positive
    project_id: Id
    template_revision: Positive
    template_digest: Digest
    scope_digest: Digest
    worker_configuration_digest: Digest
    policy_digest: Digest
    expires_at: Time


class FrozenProposal(Closed):
    proposal_id: Id
    revision: Positive
    binding: ProposalBinding
    binding_digest: Digest
    scope: FrozenScope
    expires_at: Time
    status: Literal['pending','granted','superseded','expired']


def validate_proposal(value):
    from personal_agent_dal.timeline.requests import digest
    proposal=FrozenProposal.model_validate(value)
    b=proposal.binding;s=proposal.scope
    if (digest(value['binding'])!=proposal.binding_digest or digest(value['scope'])!=b.scope_digest
        or proposal.proposal_id!=b.proposal_id or proposal.revision!=b.revision or proposal.expires_at!=b.expires_at
        or b.project_id!=s.project_id or b.template_digest!=s.template_digest
        or s.branch!='refs/heads/codex/dal-'+b.workflow_id
        or (b.expected_grant is None)!=(b.operation=='create')
        or len(s.actions)!=len(set(s.actions)) or 'read' not in s.actions
        or ('push' in s.actions)!=('pr' in s.actions)
        or (s.registration_policy=='github_issue')!=('remote_issue' in s.actions)):
        raise ValueError('RESPONSE_INVALID')
    return value
