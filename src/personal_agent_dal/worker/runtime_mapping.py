"""Exact immutable role resolution. This module grants no launch capability."""
from pathlib import Path
from pydantic import Field
from typing import Annotated
from personal_agent_dal.machine.workflow_selection import Closed, Role, Digest, digest, ProfileSnapshot, Id


class RuntimePin(Closed):
    role: str
    configuration: Role
    executable: str
    version: Annotated[str, Field(min_length=1)]
    executable_sha256: Digest


def resolve_snapshot(body, expected_sha256, pins):
    if isinstance(body,dict) and body.get('contract_version')=='dal.role-contract/3.0':
        return resolve_v3_snapshot(body,expected_sha256,pins)
    if not isinstance(body, dict) or digest(body) != expected_sha256:
        raise ValueError('SNAPSHOT_DIGEST_MISMATCH')
    ProfileSnapshot.model_validate(body)
    registered=[RuntimePin.model_validate(p) for p in pins]
    result={}
    for name, raw in body['roles'].items():
        role=Role.model_validate(raw)
        matches=[p for p in registered if p.role==name and p.configuration==role]
        if len(matches)!=1 or not Path(matches[0].executable).is_absolute():
            raise ValueError('EXPLICIT_RUNTIME_PIN_REQUIRED')
        result[name]=matches[0]
    return result


class RuntimePinV3(Closed):
    """Explicit resolution of opaque v3 references; never a legacy profile."""
    schema_version: __import__('typing').Literal['dal.runtime-pin/3.0'] = Field(alias='schema')
    role: __import__('typing').Literal['planner','coder','reviewer']
    configuration: dict
    runtime: __import__('typing').Literal['codex_cli','claude_code']
    provider: Id
    executable: str
    version: Annotated[str, Field(min_length=1)]
    executable_sha256: Digest


def resolve_v3_snapshot(body,expected_sha256,pins):
    from types import SimpleNamespace
    from personal_agent_dal.timeline.roles import Configuration, RoleV3
    if not isinstance(body,dict) or digest(body)!=expected_sha256:raise ValueError('SNAPSHOT_DIGEST_MISMATCH')
    if set(body)!={'contract_version','configuration_id','revision','roles','digest','source'}:
        raise ValueError('ROLE_CONTRACT_VERSION_INVALID')
    config=Configuration.model_validate({k:v for k,v in body.items() if k not in ('digest','source')}).model_dump()
    if digest(config)!=body['digest'] or body['source'] not in ('system','project','task'):
        raise ValueError('ROLE_CONFIG_INTEGRITY')
    if set(config['roles'])!={'planner','coder','reviewer'}:raise ValueError('ROLE_UNAVAILABLE')
    if any(config['roles'][r]['model']==config['roles']['reviewer']['model'] for r in ('planner','coder')):
        raise ValueError('REVIEW_NOT_INDEPENDENT')
    registered=[RuntimePinV3.model_validate(p) for p in pins]
    result={}
    for name,raw in config['roles'].items():
        role=RoleV3.model_validate(raw)
        if role.permission!=('workspace_write' if name=='coder' else 'read_only'):raise ValueError('ROLE_PERMISSION_INVALID')
        matches=[p for p in registered if p.role==name and p.configuration==raw]
        if len(matches)!=1 or not Path(matches[0].executable).is_absolute():raise ValueError('EXPLICIT_RUNTIME_PIN_REQUIRED')
        pin=matches[0]
        # Local launch representation only. Immutable wire bytes remain v3.
        launch_role=SimpleNamespace(**{k:v for k,v in raw.items() if k not in ('runtime_ref','provider_ref','placement_ref')},
            runtime=pin.runtime,provider=pin.provider,placement=raw['placement_ref'])
        result[name]=SimpleNamespace(configuration=launch_role,executable=pin.executable,
            executable_sha256=pin.executable_sha256,version=pin.version)
    return result
