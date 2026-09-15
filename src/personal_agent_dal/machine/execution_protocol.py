"""Closed wire contracts shared by the authority and offline Worker adapters."""
from typing import Annotated, Literal
from pydantic import Field, StrictBool, StrictInt, model_validator
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest, Version, Role, digest
from personal_agent_dal.machine.execution_start import ExecutionInput, Artifact, CONTRACT, BUDGET

Text = Annotated[str, Field(strict=True, max_length=131072)]
Count = Annotated[StrictInt, Field(ge=0, le=2**53)]

class Budget(Closed):
    wall_seconds: Literal[600]
    output_bytes: Literal[4194304]
    cli_launches: Literal[1]
    stop_reserve_seconds: Literal[10]
    # No unbounded autonomous continuation or child model calls.
    max_turns: Literal[64] = 64

class ExecutionSpec(Closed):
    schema_: Literal['dal.execution-spec/1.0'] = Field(alias='schema')
    action_execution_version: Literal['dal.action-execution/1.0']
    runtime_policy_revision: Literal['trusted-single-user/1.0']
    feature_id: Id
    action_id: Id
    attempt_id: Id
    job_id: Id
    action_version: Version
    prepared_attempt_version: Version
    expected_dispatch_fence: Version
    feature_version: Version
    capability_epoch: Count
    approval_epoch: Count
    worker_id: Id
    job_lease_epoch: Version
    lease_id: Id
    policy_lease_epoch: Version
    policy_expires_at: Version
    selection_id: Id
    snapshot_sha256: Digest
    execution_role: Literal['planner','coder','reviewer']
    role_config: Role
    input_binding_sha256: Digest
    task_description_sha256: Digest
    repository_id: Id
    base_sha: Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{40}$')]
    branch_name: Annotated[str, Field(strict=True,min_length=1,max_length=256)]
    toolchain_ref: Id
    toolchain_manifest_sha256: Digest
    permission: Literal['read_only','workspace_write']
    # Business-source scope only. Task scratch/report/cache/test-copy writes
    # remain permitted for read-only roles; reviewed source must stay unchanged.
    write_scope: Literal['none','task_workspace']
    budget: Budget
    completion_mode: Literal['report_only']
    completion_policy_revision: None

    @model_validator(mode='after')
    def permissions(self):
        if self.permission != self.role_config.permission or self.write_scope != ('task_workspace' if self.permission == 'workspace_write' else 'none'):
            raise ValueError('SPEC_PERMISSION_INVALID')
        return self


def complete_context(context, *, action, attempt, lease, inp):
    role = context['snapshot']['roles'][action.execution_role]
    spec = ExecutionSpec.model_validate(dict(schema='dal.execution-spec/1.0',
        action_execution_version=CONTRACT,runtime_policy_revision='trusted-single-user/1.0',
        **{k:context[k] for k in ('feature_id','action_id','attempt_id','job_id','worker_id','job_lease_epoch','lease_id','policy_lease_epoch','selection_id','snapshot_sha256')},
        action_version=action.version,prepared_attempt_version=attempt.version,expected_dispatch_fence=attempt.fence+1,
        feature_version=attempt.feature_version,capability_epoch=attempt.capability_epoch,approval_epoch=attempt.approval_epoch,
        policy_expires_at=int(lease.expires_at.timestamp()),execution_role=action.execution_role,role_config=role,
        input_binding_sha256=action.input_binding_sha256,
        **{k:getattr(inp,k) for k in ('task_description_sha256','repository_id','base_sha','branch_name','toolchain_ref','toolchain_manifest_sha256')},
        permission=role['permission'],write_scope='task_workspace' if role['permission']=='workspace_write' else 'none',
        budget=BUDGET,completion_mode=action.completion_mode,completion_policy_revision=action.completion_policy_revision)).model_dump(by_alias=True)
    return validate_execution_context(dict(context, schema='dal.worker-execution-transport/1.0',
        isolation_reserved_by=context.get('isolation_reserved_by'),
        execution_spec=spec, execution_spec_sha256=digest(spec),
        execution_input=inp.model_dump(by_alias=True), execution_role=action.execution_role,
        completion_mode=action.completion_mode, budget=BUDGET))

class StopObservation(Closed):
    requested: StrictBool
    process_exited: StrictBool
    forced: StrictBool

class Usage(Closed):
    input_tokens: Count | None
    output_tokens: Count | None
    provider_requests: Count | None

class ExecutionResult(Closed):
    schema_: Literal['dal.execution-result/1.0'] = Field(alias='schema')
    request_id: Id
    feature_id: Id
    action_id: Id
    attempt_id: Id
    job_id: Id
    worker_id: Id
    attempt_version: Version
    fence: Version
    job_lease_epoch: Version
    lease_id: Id
    policy_lease_epoch: Version
    snapshot_sha256: Digest
    execution_spec_sha256: Digest
    manifest_sha256: Digest
    execution_role: Literal['planner','coder','reviewer']
    outcome: Literal['succeeded','failed','interaction_required','unknown']
    reason: Id | None
    started_at: Count
    ended_at: Count
    stop: StopObservation
    report: Text
    cli_exit_code: StrictInt | None
    tool_events: Annotated[list[Text], Field(max_length=128)]
    tests: Annotated[list[Text], Field(max_length=128)]
    git_evidence: Annotated[list[Artifact], Field(max_length=64)]
    artifacts: Annotated[list[Artifact], Field(max_length=64)]
    usage: Usage
    unverified: Annotated[list[Text], Field(max_length=64)]
    truncated: StrictBool
    redacted: StrictBool

    @model_validator(mode='after')
    def times(self):
        if self.ended_at < self.started_at: raise ValueError('RESULT_TIME_INVALID')
        if self.outcome == 'succeeded' and (self.truncated or self.cli_exit_code != 0 or not self.stop.process_exited):
            raise ValueError('RESULT_INCOMPLETE')
        return self

class ExecutionResultRequest(Closed):
    schema_: Literal['dal.worker-execution-transport/1.0'] = Field(alias='schema')
    result: ExecutionResult
    result_sha256: Digest

class ExecutionResultResponse(Closed):
    schema_: Literal['dal.worker-execution-transport/1.0'] = Field(alias='schema')
    job_id: Id
    attempt_id: Id
    code: Id
    reason: Id | None
    accepted: StrictBool
    replay: StrictBool
    receipt_id: Id | None
    result_sha256: Digest
    job_state: Literal['pending','leased','running','succeeded','failed','expired','cancelled']

class ExecutionStatus(Closed):
    schema_: Literal['dal.worker-execution-transport/1.0'] = Field(alias='schema')
    job_id: Id
    attempt_id: Id
    attempt_state: Id
    attempt_version: Version
    fence: Count
    manifest_sha256: Digest | None
    job_lease_valid: StrictBool
    policy_lease_valid: StrictBool
    stop_required: StrictBool
    result_digest: Digest | None
    report_receipt_id: Id | None
    consumption_receipt_id: Id | None
    evidence_digests: Annotated[list[Digest], Field(max_length=64)]
    evidence_truncated: StrictBool
    classification: Literal['prepared','running','report_complete','consumed','execution_effects_unknown','result_available_not_accepted']


class SnapshotRoles(Closed):
    planner: Role
    coder: Role
    reviewer: Role

class Snapshot(Closed):
    revision_id: Id
    input_sha256: Digest
    profile: Literal['A','B']
    revision: Version
    roles: SnapshotRoles
    fallback: None


def validate_execution_context(value):
    """Validate the new wire shape; the legacy context remains a distinct reader."""
    from personal_agent_dal.machine.execution_manifest import IsolationV11
    from personal_agent_dal.machine.isolation_evidence import IsolationAssertion
    class Context(Closed):
        schema_: Literal['dal.worker-execution-transport/1.0'] = Field(alias='schema')
        intent_id: Id | None
        attempt_id: Id
        attempt_version: Version
        feature_id: Id
        action_id: Id
        job_id: Id
        worker_id: Id
        job_lease_epoch: Version
        lease_id: Id
        policy_lease_epoch: Version
        snapshot_sha256: Digest
        snapshot: Snapshot
        selection_id: Id
        isolation: IsolationV11 | IsolationAssertion | None
        isolation_reserved_by: Id | None
        execution_spec: ExecutionSpec
        execution_spec_sha256: Digest
        execution_input: ExecutionInput
        execution_role: Literal['planner','coder','reviewer']
        completion_mode: Literal['report_only']
        budget: Budget
    body=Context.model_validate(value).model_dump(by_alias=True)
    spec=body['execution_spec']
    if (digest(spec)!=body['execution_spec_sha256'] or digest(body['snapshot'])!=body['snapshot_sha256']
            or digest(body['execution_input'])!=spec['input_binding_sha256']
            or body['snapshot']['roles'][body['execution_role']]!=spec['role_config']
            or any(body[k]!=spec[k] for k in ('attempt_id','action_id','feature_id','job_id','worker_id','job_lease_epoch','lease_id','policy_lease_epoch','selection_id','snapshot_sha256','execution_role','completion_mode'))
            or body['attempt_version']!=spec['prepared_attempt_version']
            or (body['isolation'] is None) != (body['isolation_reserved_by'] is None)
            or body['isolation_reserved_by'] not in (None,body['attempt_id'])):
        raise ValueError('EXECUTION_CONTEXT_BINDING_INVALID')
    return body
