"""Exact immutable role resolution. This module grants no launch capability."""
from pathlib import Path
from pydantic import Field
from typing import Annotated
from personal_agent_dal.machine.workflow_selection import Closed, Role, Digest, digest, ProfileSnapshot


class RuntimePin(Closed):
    role: str
    configuration: Role
    executable: str
    version: Annotated[str, Field(min_length=1)]
    executable_sha256: Digest


def resolve_snapshot(body, expected_sha256, pins):
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
