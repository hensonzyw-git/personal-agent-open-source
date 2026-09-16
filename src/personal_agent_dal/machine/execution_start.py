"""Explicit operator preparation and first execution; no provider side effects."""
import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator
from sqlalchemy import select, update
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import _transaction
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest, Version, digest, profile_snapshot
from personal_agent_dal.storage.models import Feature
from personal_agent_dal.storage.machine_models import (
    WorkflowAction, ProviderAttempt, ExecutionGate, WorkflowProfileRevision,
    WorkflowSelection, ExecutionSnapshot, ExecutionJobBinding, ExecutionStartReceipt,
)
from personal_agent_dal.storage.worker_models import FeatureIntakeRequest
from personal_agent_dal.worker.queue import enqueue_job_in_session

CONTRACT = 'dal.action-execution/1.0'
# Fixed, displayed limits. A later runtime may impose stricter limits only.
BUDGET = dict(wall_seconds=600, output_bytes=4194304, cli_launches=1, stop_reserve_seconds=10, max_turns=64)


class TaskSource(Closed):
    kind: Literal['intake', 'operator']
    intake_key: Id | None = None

    @model_validator(mode='after')
    def source_key(self):
        if (self.kind == 'intake') != (self.intake_key is not None):
            raise ValueError('INPUT_SOURCE_INVALID')
        return self


class Artifact(Closed):
    kind: Literal['report', 'patch', 'test_report']
    artifact_id: Id
    sha256: Digest


class ExecutionInput(Closed):
    schema_: Literal['dal.execution-input/1.0'] = Field(alias='schema')
    feature_id: Id
    task_source: TaskSource
    task_description: Annotated[str, Field(strict=True, min_length=1, max_length=65536)]
    task_description_sha256: Digest
    repository_id: Id
    base_sha: Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{40}$')]
    branch_name: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    toolchain_ref: Id
    toolchain_manifest_sha256: Digest
    artifacts: Annotated[list[Artifact], Field(max_length=64)] = []


class PrepareExecutionRequest(Closed):
    request_id: Id
    action_key: Id
    execution_role: Literal['planner', 'coder', 'reviewer']
    execution_input: ExecutionInput
    profile_revision_id: Id
    expected_feature_version: Version
    # Zero explicitly means bootstrap a never-existing gate.
    expected_gate_version: Annotated[StrictInt, Field(ge=0)]
    completion_mode: Literal['report_only', 'feature_transition'] = 'report_only'
    completion_policy_revision: Id | None = None


class StartExecutionRequest(Closed):
    request_id: Id
    action_id: Id
    selection_id: Id
    expected_feature_version: Version
    expected_gate_version: Version
    expected_action_version: Version
    confirmed_execution_sha256: Digest
    expires_at: Annotated[StrictInt, Field(ge=1)]


def confirmation(action, selection, feature, gate):
    return dict(action_id=action.action_id, selection_id=selection.selection_id,
        execution_role=action.execution_role, input_binding_sha256=action.input_binding_sha256,
        snapshot_sha256=action.execution_snapshot_sha256, budget=BUDGET,
        completion_mode=action.completion_mode, completion_policy_revision=action.completion_policy_revision,
        feature_version=feature.version, gate_version=gate.version, action_version=action.version)


def _input(s, feature_id, body):
    value = body.model_dump(by_alias=True)
    encoded = canonical_json(value)
    if len(encoded.encode()) > 65536: raise ValueError('INPUT_TOO_LARGE')
    if body.feature_id != feature_id or hashlib.sha256(body.task_description.encode()).hexdigest() != body.task_description_sha256:
        raise ValueError('INPUT_BINDING_INVALID')
    if body.task_source.kind == 'intake':
        row = s.get(FeatureIntakeRequest, body.task_source.intake_key)
        if not row or (row.feature_id, row.task_description, row.task_description_sha256, row.toolchain_ref) != (
                feature_id, body.task_description, body.task_description_sha256, body.toolchain_ref):
            raise ValueError('INTAKE_BINDING_INVALID')
        if body.task_source.intake_key.startswith('pending:'):
            from personal_agent_dal.service.intake import _feature_id
            if (_feature_id(body.repository_id, body.task_description, body.base_sha) != feature_id
                    or body.task_source.intake_key != f'pending:{feature_id}'
                    or body.branch_name != f'codex/feature-{feature_id}'):
                raise ValueError('INTAKE_BINDING_INVALID')
    return encoded, digest(value)


def prepare_execution(engine, *, feature_id, actor, body, kill_switch=lambda: False):
    request_sha = digest(dict(feature_id=feature_id, actor=actor, **body.model_dump(by_alias=True)))
    def work(s):
        if not actor: raise ValueError('OPERATOR_REQUIRED')
        old = s.scalar(select(WorkflowSelection).where(WorkflowSelection.request_id == body.request_id))
        if old:
            if old.request_sha256 != request_sha: raise ValueError('IDEMPOTENCY_CONFLICT')
            a = s.get(WorkflowAction, old.action_id)
            display = dict(action_id=a.action_id, selection_id=old.selection_id,
                execution_role=a.execution_role, input_binding_sha256=a.input_binding_sha256,
                snapshot_sha256=old.snapshot_sha256, budget=BUDGET, completion_mode=a.completion_mode,
                completion_policy_revision=a.completion_policy_revision, feature_version=old.feature_version,
                gate_version=old.gate_version, action_version=1)
            return dict(action_id=a.action_id, selection_id=old.selection_id, snapshot_sha256=old.snapshot_sha256,
                        confirmation=display, confirmed_execution_sha256=digest(display),
                        execution_input=json.loads(a.execution_input_body),
                        profile=json.loads(s.get(ExecutionSnapshot, a.execution_snapshot_sha256).body))
        if kill_switch(): raise ValueError('KILL_SWITCH_ACTIVE')
        if body.completion_mode != 'report_only': raise ValueError('COMPLETION_RESOLVER_UNAVAILABLE')
        if body.completion_policy_revision is not None: raise ValueError('COMPLETION_POLICY_INVALID')
        f, g = s.get(Feature, feature_id), s.get(ExecutionGate, feature_id)
        if not f or f.version != body.expected_feature_version or f.state in ('completed','cancelled'):
            raise ValueError('FEATURE_STALE')
        now = utc_now()
        if g is None:
            if body.expected_gate_version != 0: raise ValueError('EXECUTION_GATE_STALE')
            g = ExecutionGate(feature_id=feature_id, version=1, mode='paused', approval_epoch=0, created_at=now, updated_at=now)
            s.add(g)
        elif g.mode != 'paused' or g.version != body.expected_gate_version:
            raise ValueError('EXECUTION_GATE_STALE')
        if s.scalar(select(WorkflowAction.action_id).where(WorkflowAction.feature_id == feature_id,
                WorkflowAction.active_attempt_id.is_not(None))): raise ValueError('ACTIVE_ACTION_EXISTS')
        if s.scalar(select(WorkflowAction.action_id).where(WorkflowAction.feature_id == feature_id,
                WorkflowAction.action_key == body.action_key)): raise ValueError('IDEMPOTENCY_CONFLICT')
        encoded, input_sha = _input(s, feature_id, body.execution_input)
        p = s.get(WorkflowProfileRevision, body.profile_revision_id)
        if not p or digest(json.loads(p.body)) != p.sha256: raise ValueError('PROFILE_UNAVAILABLE')
        snapshot = profile_snapshot(json.loads(p.body), revision_id=p.revision_id, input_sha256=input_sha)
        snapshot_sha = digest(snapshot)
        if not s.get(ExecutionSnapshot, snapshot_sha):
            s.add(ExecutionSnapshot(sha256=snapshot_sha, revision_id=p.revision_id, body=canonical_json(snapshot)))
        a = WorkflowAction(action_id=new_id(), feature_id=feature_id, kind='provider', action_key=body.action_key,
            input_binding_sha256=input_sha, execution_snapshot_sha256=snapshot_sha, version=1,
            execution_contract_version=CONTRACT, execution_role=body.execution_role, execution_input_body=encoded,
            completion_mode=body.completion_mode, completion_policy_revision=None, created_at=now, updated_at=now)
        s.add(a)
        s.flush()
        selection = WorkflowSelection(selection_id=new_id(), request_id=body.request_id, request_sha256=request_sha,
            action_id=a.action_id, feature_id=feature_id, feature_version=f.version, gate_version=g.version,
            snapshot_sha256=snapshot_sha, actor=actor, created_at=now)
        s.add(selection)
        display = confirmation(a, selection, f, g)
        return dict(action_id=a.action_id, selection_id=selection.selection_id, snapshot_sha256=snapshot_sha,
                    confirmation=display, confirmed_execution_sha256=digest(display),
                        execution_input=json.loads(a.execution_input_body),
                        profile=json.loads(s.get(ExecutionSnapshot, a.execution_snapshot_sha256).body))
    return _transaction(engine, work)


def start_execution(engine, *, feature_id, actor, body, kill_switch=lambda: False):
    request_sha = digest(dict(feature_id=feature_id, actor=actor, **body.model_dump()))
    def work(s):
        if not actor: raise ValueError('OPERATOR_REQUIRED')
        old = s.get(ExecutionStartReceipt, body.request_id)
        if old:
            if old.request_sha256 != request_sha: raise ValueError('IDEMPOTENCY_CONFLICT')
            binding = s.get(ExecutionJobBinding, old.attempt_id)
            return dict(attempt_id=old.attempt_id, job_id=binding.job_id, request_id=old.request_id)
        if kill_switch(): raise ValueError('KILL_SWITCH_ACTIVE')
        now = utc_now()
        expires = datetime.fromtimestamp(body.expires_at, timezone.utc)
        a, selection = s.get(WorkflowAction, body.action_id), s.get(WorkflowSelection, body.selection_id)
        f, g = s.get(Feature, feature_id), s.get(ExecutionGate, feature_id)
        if (not a or not selection or not f or not g or a.feature_id != feature_id
                or a.execution_contract_version != CONTRACT or selection.action_id != a.action_id
                or selection.feature_id != feature_id or selection.snapshot_sha256 != a.execution_snapshot_sha256
                or selection.feature_version != f.version or selection.gate_version != g.version
                or a.active_attempt_id is not None or a.version != body.expected_action_version
                or f.version != body.expected_feature_version or g.version != body.expected_gate_version
                or g.mode != 'paused' or f.state in ('completed','cancelled') or expires <= now):
            raise ValueError('EXECUTION_CONFIRMATION_STALE')
        if a.completion_mode != 'report_only': raise ValueError('COMPLETION_RESOLVER_UNAVAILABLE')
        if digest(confirmation(a, selection, f, g)) != body.confirmed_execution_sha256:
            raise ValueError('EXECUTION_CONFIRMATION_MISMATCH')
        if s.scalar(select(WorkflowAction.action_id).where(WorkflowAction.feature_id == feature_id,
                WorkflowAction.active_attempt_id.is_not(None))): raise ValueError('ACTIVE_ACTION_EXISTS')
        if s.scalar(select(ExecutionStartReceipt).where(ExecutionStartReceipt.action_id == a.action_id)):
            raise ValueError('ACTION_ALREADY_STARTED')
        inp = ExecutionInput.model_validate(json.loads(a.execution_input_body))
        _, input_sha = _input(s, feature_id, inp)
        snap = s.get(ExecutionSnapshot, a.execution_snapshot_sha256)
        if (input_sha != a.input_binding_sha256 or not snap or digest(json.loads(snap.body)) != snap.sha256
                or json.loads(snap.body)['input_sha256'] != input_sha): raise ValueError('INPUT_BINDING_INVALID')
        changed = s.execute(update(ExecutionGate).where(ExecutionGate.feature_id == feature_id,
            ExecutionGate.version == g.version, ExecutionGate.mode == 'paused').values(
                mode='open', version=g.version+1, approval_epoch=g.approval_epoch+1, updated_at=now))
        if changed.rowcount != 1: raise ValueError('EXECUTION_GATE_STALE')
        s.refresh(g)
        job_id = enqueue_job_in_session(s, feature_id=feature_id, repository_id=inp.repository_id,
            base_sha=inp.base_sha, branch_name=inp.branch_name, toolchain_ref=inp.toolchain_ref,
            now=now, execution_mode='provider_v1')
        attempt = ProviderAttempt(attempt_id=new_id(), action_id=a.action_id, attempt_no=1, state='prepared',
            version=1, fence=0, job_id=job_id, approval_epoch=g.approval_epoch, feature_version=f.version,
            capability_epoch=f.capability_epoch, created_at=now, updated_at=now)
        s.add(attempt)
        s.flush()
        s.add(ExecutionJobBinding(attempt_id=attempt.attempt_id, job_id=job_id, selection_id=selection.selection_id,
            snapshot_sha256=snap.sha256, input_binding_sha256=input_sha, origin='initial', created_at=now))
        s.add(ExecutionStartReceipt(request_id=body.request_id, request_sha256=request_sha, actor=actor,
            action_id=a.action_id, selection_id=selection.selection_id, binding_sha256=body.confirmed_execution_sha256,
            expires_at=expires, attempt_id=attempt.attempt_id, recorded_at=now))
        changed = s.execute(update(WorkflowAction).where(WorkflowAction.action_id == a.action_id,
            WorkflowAction.version == body.expected_action_version, WorkflowAction.active_attempt_id.is_(None)).values(
                active_attempt_id=attempt.attempt_id, version=body.expected_action_version+1, updated_at=now))
        if changed.rowcount != 1: raise ValueError('ACTION_STALE')
        return dict(attempt_id=attempt.attempt_id, job_id=job_id, request_id=body.request_id)
    return _transaction(engine, work)


def execution_binding(s, attempt, job):
    """Read the immutable cross-table binding; never infer role or input."""
    binding = s.get(ExecutionJobBinding, attempt.attempt_id)
    action = s.get(WorkflowAction, attempt.action_id)
    if not binding or not action or job.execution_mode != 'provider_v1' or attempt.job_id != job.job_id or binding.job_id != job.job_id:
        raise ValueError('EXECUTION_BINDING_REQUIRED')
    selection = s.get(WorkflowSelection, binding.selection_id)
    snapshot = s.get(ExecutionSnapshot, binding.snapshot_sha256)
    if (action.execution_contract_version != CONTRACT or not selection or not snapshot
            or selection.feature_id != action.feature_id or job.feature_id != action.feature_id
            or selection.snapshot_sha256 != snapshot.sha256 or action.execution_snapshot_sha256 != snapshot.sha256
            or digest(json.loads(snapshot.body)) != snapshot.sha256
            or binding.input_binding_sha256 != action.input_binding_sha256
            or json.loads(snapshot.body)['input_sha256'] != action.input_binding_sha256):
        raise ValueError('EXECUTION_BINDING_STALE')
    inp = ExecutionInput.model_validate(json.loads(action.execution_input_body))
    if digest(inp.model_dump(by_alias=True)) != action.input_binding_sha256 or inp.feature_id != action.feature_id:
        raise ValueError('INPUT_BINDING_INVALID')
    if any(getattr(job, name) != getattr(inp, name) for name in ('repository_id','base_sha','branch_name','toolchain_ref')):
        raise ValueError('JOB_INPUT_MISMATCH')
    if action.completion_mode != 'report_only': raise ValueError('COMPLETION_RESOLVER_UNAVAILABLE')
    return binding, action, selection, snapshot, inp


def initial_context(s, *, job, worker_id):
    from datetime import timedelta
    from personal_agent_dal.machine.action_lifecycle import _authority, _leases
    from personal_agent_dal.storage.machine_models import Lease, ExecutionPolicyLeaseIssuance, ResumeEpisode
    from personal_agent_dal.storage.transport_models import WorkerEnrollment, SupervisorLaunchManifest
    binding = s.scalar(select(ExecutionJobBinding).where(ExecutionJobBinding.job_id == job.job_id))
    if not binding: raise ValueError('EXECUTION_BINDING_REQUIRED')
    attempt = s.get(ProviderAttempt, binding.attempt_id)
    binding, action, selection, snapshot, inp = execution_binding(s, attempt, job)
    if binding.origin != 'initial' or selection.action_id != action.action_id or s.scalar(select(ResumeEpisode).where(ResumeEpisode.attempt_id == attempt.attempt_id)):
        raise ValueError('INITIAL_BINDING_INVALID')
    gate = s.get(ExecutionGate, action.feature_id)
    refusal = _authority(s, attempt, action, gate)
    if refusal: raise ValueError(refusal)
    if attempt.state != 'prepared' or attempt.dispatch_started_at is not None: raise ValueError('ATTEMPT_ALREADY_DISPATCHED')
    receipt = s.scalar(select(ExecutionStartReceipt).where(ExecutionStartReceipt.attempt_id == attempt.attempt_id))
    now = utc_now()
    if not receipt or receipt.action_id != action.action_id or receipt.selection_id != selection.selection_id or receipt.expires_at <= now:
        raise ValueError('START_AUTHORIZATION_STALE')
    original = dict(action_id=action.action_id, selection_id=selection.selection_id,
        execution_role=action.execution_role, input_binding_sha256=action.input_binding_sha256,
        snapshot_sha256=action.execution_snapshot_sha256, budget=BUDGET, completion_mode=action.completion_mode,
        completion_policy_revision=action.completion_policy_revision, feature_version=selection.feature_version,
        gate_version=selection.gate_version, action_version=1)
    if receipt.binding_sha256 != digest(original) or gate.approval_epoch != attempt.approval_epoch:
        raise ValueError('START_AUTHORIZATION_STALE')
    worker = s.get(WorkerEnrollment, worker_id)
    if not worker or worker.revoked_at is not None: raise ValueError('WORKER_ENROLLMENT_STALE')
    issued = s.get(ExecutionPolicyLeaseIssuance, (attempt.attempt_id, job.lease_epoch))
    if issued:
        lease = s.get(Lease, issued.lease_id)
    else:
        if s.get(SupervisorLaunchManifest, attempt.attempt_id): raise ValueError('MANIFEST_LEASE_IMMUTABLE')
        ceiling = min(now+timedelta(minutes=15), receipt.expires_at, job.lease_expires_at)
        for prior in s.scalars(select(ExecutionPolicyLeaseIssuance).where(ExecutionPolicyLeaseIssuance.attempt_id == attempt.attempt_id)):
            old = s.get(Lease, prior.lease_id)
            if prior.job_lease_epoch >= job.lease_epoch or not old or old.revoked_at is not None:
                raise ValueError('POLICY_LEASE_STALE')
            ceiling = min(ceiling, old.created_at+timedelta(minutes=15))
        if ceiling <= now: raise ValueError('POLICY_LEASE_STALE')
        if s.scalar(select(Lease).where(Lease.job_id == job.job_id, Lease.epoch == job.lease_epoch)):
            raise ValueError('POLICY_LEASE_BINDING_CONFLICT')
        lease = Lease(lease_id=new_id(), feature_id=action.feature_id, job_id=job.job_id, worker_id=worker_id,
            epoch=job.lease_epoch, expires_at=ceiling, created_at=now)
        s.add(lease)
        s.flush()
        s.add(ExecutionPolicyLeaseIssuance(attempt_id=attempt.attempt_id, job_lease_epoch=job.lease_epoch, lease_id=lease.lease_id))
    refusal = _leases(job, lease, action.feature_id, worker_id, now)
    if refusal: raise ValueError(refusal)
    from personal_agent_dal.machine.execution_protocol import complete_context
    context = dict(intent_id=None, attempt_id=attempt.attempt_id, attempt_version=attempt.version,
        feature_id=action.feature_id, action_id=action.action_id, job_id=job.job_id, worker_id=worker_id,
        job_lease_epoch=job.lease_epoch, lease_id=lease.lease_id, policy_lease_epoch=lease.epoch,
        snapshot_sha256=snapshot.sha256, snapshot=json.loads(snapshot.body), selection_id=selection.selection_id,
        isolation=None, execution_role=action.execution_role, execution_input=inp.model_dump(by_alias=True),
        completion_mode=action.completion_mode, budget=BUDGET)

    return complete_context(context, action=action, attempt=attempt, lease=lease, inp=inp)
