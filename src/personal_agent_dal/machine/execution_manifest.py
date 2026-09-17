"""Version 1.1 signing/validation. Legacy evidence is never upgraded in place."""
import json
from typing import Literal
from pydantic import Field, model_validator
from sqlalchemy import select
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.workflow_selection import Id, Digest, Version, digest
from personal_agent_dal.machine.execution_protocol import ExecutionSpec
from personal_agent_dal.machine.isolation_evidence import LaunchManifestAssertion, IsolationAssertion, _current
from personal_agent_dal.storage.transport_models import SupervisorLaunchManifest, IsolationEvidence

class LaunchManifestV11(LaunchManifestAssertion):
    schema_version: Literal['dal.launch-manifest/1.1'] = Field(alias='schema')
    inventory_sha256: Digest
    reservation_id: Id
    job_id: Id
    job_lease_epoch: Version
    lease_id: Id
    policy_lease_epoch: Version
    execution_spec: ExecutionSpec
    execution_spec_sha256: Digest
    launcher_plan_sha256: Digest
    source_reservation_sha256: Digest
    isolation_id: Id | None
    isolation_binding_sha256: Digest | None

    @model_validator(mode='after')
    def binding(self):
        if self.execution_spec_sha256 != digest(self.execution_spec.model_dump(by_alias=True)):
            raise ValueError('SPEC_DIGEST_INVALID')
        if (self.isolation_id is None) != (self.isolation_binding_sha256 is None):
            raise ValueError('ISOLATION_BINDING_INVALID')
        if self.expires_at <= self.issued_at or self.expires_at-self.issued_at > 900:
            raise ValueError('MANIFEST_WINDOW_INVALID')
        return self

class IsolationV11(IsolationAssertion):
    schema_version: Literal['dal.workspace-isolation/1.1'] = Field(alias='schema')
    status: Literal['workspace_ready']
    new_reservation_id: Id
    new_inventory_sha256: Digest
    stop_observation_sha256: Digest
    runtime_policy_revision: Literal['trusted-single-user/1.0']


def sign_execution_manifest(payload, *, key):
    """Pure producer for the next Supervisor; never provisions or spawns."""
    from personal_agent.api.dal_client import sign_decision
    body = LaunchManifestV11.model_validate(payload).model_dump(by_alias=True)
    return sign_decision(body, key=key, kid=body['kid']), digest(body)


def validate_execution_manifest(s, *, attempt, job, lease, owner):
    from personal_agent_dal.machine.resume_dispatch import _context
    row = s.get(SupervisorLaunchManifest, attempt.attempt_id)
    if not row: return 'PRELAUNCH_MANIFEST_REQUIRED'
    try:
        body = LaunchManifestV11.model_validate(json.loads(row.body)).model_dump(by_alias=True)
        if digest(body) != row.sha256: return 'MANIFEST_INVALID'
        _current(s, body)
        context = _context(s, job_id=job.job_id, worker_id=owner, job_lease_epoch=job.lease_epoch)
        if body['execution_spec'] != context['execution_spec'] or body['execution_spec_sha256'] != context['execution_spec_sha256']:
            return 'EXECUTION_SPEC_STALE'
        if body['attempt_id'] != attempt.attempt_id: return 'MANIFEST_TARGET_MISMATCH'
        for key,value in dict(worker_id=owner,job_id=job.job_id,job_lease_epoch=job.lease_epoch,
                              lease_id=lease.lease_id,policy_lease_epoch=lease.epoch).items():
            if body[key] != value: return 'MANIFEST_AUTHORITY_STALE'
        if body['expires_at'] <= int(utc_now().timestamp()) or body['expires_at'] > int(lease.expires_at.timestamp()):
            return 'MANIFEST_EXPIRED'
        isolation = context['isolation']
        if isolation:
            evidence = s.get(IsolationEvidence, body['isolation_id']) if body['isolation_id'] else None
            if (not evidence or evidence.reserved_by != attempt.attempt_id
                or evidence.binding_sha256 != body['isolation_binding_sha256']
                or json.loads(evidence.binding) != isolation
                or isolation.get('schema') != 'dal.workspace-isolation/1.1'
                or body['workspace_id'] != isolation['new_workspace_id']
                or body['workspace_generation'] != isolation['workspace_generation']
                or body['reservation_id'] != isolation['new_reservation_id']
                or body['source_reservation_sha256'] != isolation['new_inventory_sha256']):
                return 'ISOLATION_RESERVATION_STALE'
        elif body['isolation_id'] is not None or body['source_reservation_sha256'] != body['inventory_sha256']:
            return 'SOURCE_RESERVATION_INVALID'
    except ValueError:
        return 'EXECUTION_MANIFEST_V1_1_REQUIRED'
    return None


def sign_execution_isolation(payload, *, key):
    """Pure 1.1 evidence producer; caller supplies actual stop/NEW observations."""
    from personal_agent.api.dal_client import sign_decision
    body = IsolationV11.model_validate(payload).model_dump(by_alias=True)
    return sign_decision(body, key=key, kid=body['kid']), digest(body)
