"""Exact immutable role resolution. This module grants no launch capability."""
from pathlib import Path
from pydantic import Field
from typing import Annotated
from personal_agent_dal.machine.workflow_selection import Closed, Role, Digest, digest


class RuntimePin(Closed):
    role: str
    configuration: Role
    executable: str
    version: Annotated[str, Field(min_length=1)]
    executable_sha256: Digest


def resolve_snapshot(body, expected_sha256, pins):
    if not isinstance(body, dict) or digest(body) != expected_sha256:
        raise ValueError('SNAPSHOT_DIGEST_MISMATCH')
    if set(body) != {'revision_id','profile','revision','input_sha256','roles','fallback'}:
        raise ValueError('SNAPSHOT_SHAPE')
    if body['fallback'] is not None or body['profile'] not in ('A','B'):
        raise ValueError('RUNTIME_FALLBACK_FORBIDDEN')
    if set(body['roles']) != {'coder','planner','reviewer'}:
        raise ValueError('ROLE_SET_INVALID')
    registered=[RuntimePin.model_validate(p) for p in pins]
    result={}
    for name, raw in body['roles'].items():
        role=Role.model_validate(raw)
        if role.permission != ('workspace_write' if name=='coder' else 'read_only'):
            raise ValueError('ROLE_PERMISSION_INVALID')
        if body['profile']=='B':
            expected=('gpt-5.6-sol','high') if name=='coder' else ('gpt-6-astra','medium')
            if (role.runtime,role.provider,role.model,role.reasoning,role.billing) != ('codex_cli','openai',*expected,'subscription'):
                raise ValueError('PROFILE_B_INVALID')
        elif name=='coder' and role.runtime!='claude_code':
            raise ValueError('PROFILE_A_INVALID')
        matches=[p for p in registered if p.role==name and p.configuration==role]
        if len(matches)!=1 or not Path(matches[0].executable).is_absolute():
            raise ValueError('EXPLICIT_RUNTIME_PIN_REQUIRED')
        result[name]=matches[0]
    if result['coder'].configuration.model==result['reviewer'].configuration.model:
        raise ValueError('SELF_REVIEW_FORBIDDEN')
    return result
