"""Server-owned immutable revisions. Manual selection never schedules work."""
import hashlib
import json
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_serializer, model_validator
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import _transaction
from personal_agent_dal.storage.models import Feature
from personal_agent_dal.storage.machine_models import (
    WorkflowProfileRevision, ExecutionSnapshot, WorkflowSelection, ExecutionGate, WorkflowAction,
)

Id = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')]
Digest = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
Version = Annotated[StrictInt, Field(ge=1)]


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Role(Closed):
    runtime: Literal['codex_cli', 'claude_code']
    provider: Id
    model: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]
    reasoning: Literal['low', 'medium', 'high', 'xhigh', 'none']
    placement: Literal['home_mac']
    permission: Literal['read_only', 'workspace_write']
    billing: Literal['subscription', 'api']


class SelectionRequest(Closed):
    request_id: Id
    profile_revision_id: Id
    expected_feature_version: Version
    expected_gate_version: Version


def digest(body):
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


ROLE_CONTRACT_V2 = 'dal.role-contract/2.0'


class VersionedRoleContract(Closed):
    # Absence means the historical contract, never an implicit upgrade.
    contract_version: Literal['dal.role-contract/2.0'] | None = None

    @model_validator(mode='before')
    @classmethod
    def explicit_version(cls, value):
        if isinstance(value, dict) and 'contract_version' in value and value['contract_version'] is None:
            raise ValueError('ROLE_CONTRACT_VERSION_INVALID')
        return value

    @model_serializer(mode='wrap')
    def preserve_legacy_shape(self, handler):
        body = handler(self)
        if self.contract_version is None:
            body.pop('contract_version', None)
        return body


def validate_profile_roles(profile, roles, contract_version=None):
    """One exact role policy for registration, configuration and Worker reads.

    Billing is an explicitly supplied classification, not evidence of gateway
    billing or authentication. This function grants no runtime readiness.
    """
    if contract_version not in (None, ROLE_CONTRACT_V2):
        raise ValueError('ROLE_CONTRACT_VERSION_INVALID')
    if profile not in ('A', 'B') or set(roles) != {'planner', 'coder', 'reviewer'}:
        raise ValueError('PROFILE_INVALID')
    normalized = {name: Role.model_validate(role).model_dump() for name, role in roles.items()}
    if any(role['permission'] != ('workspace_write' if name == 'coder' else 'read_only')
           for name, role in normalized.items()):
        raise ValueError('PROFILE_PERMISSION_INVALID')
    if contract_version == ROLE_CONTRACT_V2:
        if profile != 'B':
            raise ValueError('PROFILE_VERSION_INVALID')
        expected = {
            'planner': ('codex_cli', 'openai', 'gpt-6-astra', 'high'),
            'coder': ('codex_cli', 'openai', 'gpt-6-astra', 'low'),
            'reviewer': ('claude_code', 'changhe', 'changhe/ch-g/kimi-k3', 'high'),
        }
        for name, role in normalized.items():
            if tuple(role[k] for k in ('runtime', 'provider', 'model', 'reasoning')) != expected[name]:
                raise ValueError('PROFILE_B_INVALID')
            if name != 'reviewer' and role['billing'] != 'subscription':
                raise ValueError('PROFILE_B_INVALID')
    elif profile == 'B':
        for name, model, reasoning in [('planner', 'gpt-6-astra', 'medium'),
                ('coder', 'gpt-5.6-sol', 'high'), ('reviewer', 'gpt-6-astra', 'medium')]:
            role = normalized[name]
            if tuple(role[k] for k in ('runtime', 'provider', 'model', 'reasoning', 'billing')) != (
                    'codex_cli', 'openai', model, reasoning, 'subscription'):
                raise ValueError('PROFILE_B_INVALID')
    elif normalized['coder']['runtime'] != 'claude_code':
        raise ValueError('PROFILE_A_INVALID')
    if normalized['coder']['model'] == normalized['reviewer']['model']:
        raise ValueError('SELF_REVIEW_FORBIDDEN')
    return normalized


class ProfileBody(VersionedRoleContract):
    profile: Literal['A', 'B']
    revision: Version
    roles: dict[Literal['planner', 'coder', 'reviewer'], Role]
    fallback: None

    @model_validator(mode='after')
    def exact_roles(self):
        validate_profile_roles(self.profile, self.roles, self.contract_version)
        return self


class ProfileSnapshot(ProfileBody):
    revision_id: Id
    input_sha256: Digest


def profile_snapshot(profile, *, revision_id, input_sha256):
    """Validate without reserializing historical role bodies or adding defaults."""
    body = dict(revision_id=revision_id, input_sha256=input_sha256, **profile)
    ProfileSnapshot.model_validate(body)
    return body


def register_profile(engine, *, revision_id, profile, revision, roles, **version):
    """Trusted startup configuration only; callers cannot register via selection."""
    SelectionRequest(request_id=revision_id, profile_revision_id=revision_id,
                     expected_feature_version=revision, expected_gate_version=1)
    body = ProfileBody.model_validate(dict(profile=profile, revision=revision,
        roles=roles, fallback=None, **version)).model_dump()
    encoded, sha = canonical_json(body), digest(body)
    def work(s):
        old = s.get(WorkflowProfileRevision, revision_id)
        if old:
            if old.body != encoded: raise ValueError('IMMUTABLE_REVISION')
            return
        if s.scalar(select(WorkflowProfileRevision).where(WorkflowProfileRevision.profile == profile,
                WorkflowProfileRevision.revision == revision)):
            raise ValueError('REVISION_CONFLICT')
        s.add(WorkflowProfileRevision(revision_id=revision_id, profile=profile, revision=revision, body=encoded, sha256=sha))
    _transaction(engine, work)


def select_workflow(engine, *, feature_id, actor, body):
    sha = digest(dict(feature_id=feature_id, actor=actor, **body.model_dump()))
    def work(s):
        previous = s.scalar(select(WorkflowSelection).where(WorkflowSelection.request_id == body.request_id))
        if previous:
            if previous.request_sha256 != sha: raise ValueError('IDEMPOTENCY_CONFLICT')
            return {'selection_id': previous.selection_id, 'snapshot_sha256': previous.snapshot_sha256}
        f, g = s.get(Feature, feature_id), s.get(ExecutionGate, feature_id)
        if not f or not g or f.version != body.expected_feature_version or g.version != body.expected_gate_version or g.mode != 'paused':
            raise ValueError('SELECTION_STALE')
        p = s.get(WorkflowProfileRevision, body.profile_revision_id)
        if not p or digest(json.loads(p.body)) != p.sha256: raise ValueError('PROFILE_UNAVAILABLE')
        actions = list(s.scalars(select(WorkflowAction).where(WorkflowAction.feature_id==feature_id,
            WorkflowAction.active_attempt_id.is_not(None))))
        if len(actions)!=1: raise ValueError('ACTION_AMBIGUOUS')
        snapshot = profile_snapshot(json.loads(p.body), revision_id=p.revision_id,
            input_sha256=actions[0].input_binding_sha256)
        snapshot_sha = digest(snapshot)
        if not s.get(ExecutionSnapshot, snapshot_sha):
            s.add(ExecutionSnapshot(sha256=snapshot_sha, revision_id=p.revision_id, body=canonical_json(snapshot)))
        s.flush()  # Persist the snapshot before its selection.
        row = WorkflowSelection(selection_id=new_id(), request_id=body.request_id, request_sha256=sha,
            action_id=actions[0].action_id if actions[0].execution_contract_version else None,
            feature_id=feature_id, feature_version=f.version, gate_version=g.version,
            snapshot_sha256=snapshot_sha, actor=actor, created_at=utc_now())
        s.add(row)
        return {'selection_id': row.selection_id, 'snapshot_sha256': snapshot_sha}
    return _transaction(engine, work)
