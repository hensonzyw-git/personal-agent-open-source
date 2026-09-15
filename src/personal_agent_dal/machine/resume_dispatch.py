"""Consumed resume authority issues one bounded policy lease per claimed Job epoch.

No Stage or result semantics are introduced here.
"""
import json
from datetime import timedelta
from personal_agent_core.ids import new_id
from sqlalchemy import select
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import _transaction, _authority, _leases
from personal_agent_dal.machine.workflow_selection import digest
from personal_agent_dal.storage.machine_models import (
    DispatchIntent, ResumeEpisode, ReplacementBudget, ProviderAttempt, WorkflowAction,
    WorkflowSelection, ExecutionSnapshot, ExecutionGate, Lease, ResumeLeaseIssuance,
    Approval, ResumeApprovalBinding, ResumeRevocation, ResumeReceipt,
)
from personal_agent_dal.storage.worker_models import WorkerJob
from personal_agent_dal.storage.transport_models import IsolationEvidence, SupervisorLaunchManifest, WorkerEnrollment
from personal_agent_dal.machine.isolation_evidence import _current
from personal_agent_dal.worker.queue import enqueue_job_in_session


def _binding(s,intent):
    if not intent:raise ValueError('DISPATCH_INTENT_MISSING')
    attempt=s.get(ProviderAttempt,intent.attempt_id)
    action=s.get(WorkflowAction,attempt.action_id)
    gate=s.get(ExecutionGate,action.feature_id)
    refusal=_authority(s,attempt,action,gate)
    if refusal:raise ValueError(refusal)
    selection=s.get(WorkflowSelection,intent.selection_id)
    snapshot=s.get(ExecutionSnapshot,selection.snapshot_sha256) if selection else None
    if (not snapshot or selection.feature_id!=action.feature_id
        or snapshot.sha256!=action.execution_snapshot_sha256 or digest(json.loads(snapshot.body))!=snapshot.sha256
        or json.loads(snapshot.body)['input_sha256']!=action.input_binding_sha256):
        raise ValueError('SNAPSHOT_BINDING_STALE')
    budget=s.scalar(select(ReplacementBudget).where(ReplacementBudget.new_attempt_id==attempt.attempt_id))
    if not budget or budget.receipt_id!=intent.receipt_id:raise ValueError('REPLACEMENT_BINDING_MISSING')
    old=s.get(ProviderAttempt,budget.old_attempt_id)
    evidence=s.scalar(select(IsolationEvidence).where(IsolationEvidence.reserved_by==attempt.attempt_id))
    isolation=None
    if old.dispatch_started_at is not None:
        if not evidence or evidence.expires_at<=utc_now():raise ValueError('ISOLATION_RESERVATION_STALE')
        isolation=json.loads(evidence.binding)
        if digest(isolation)!=evidence.binding_sha256 or isolation['attempt_id']!=old.attempt_id:
            raise ValueError('ISOLATION_BINDING_STALE')
        _current(s,isolation)
    return attempt,action,selection,snapshot,old,isolation


def consume_intent(engine,*,intent_id):
    def work(s):
        intent=s.get(DispatchIntent,intent_id)
        attempt,action,_,_,old,_=_binding(s,intent)
        existing=s.get(ResumeEpisode,intent_id)
        if existing:return existing.job_id
        if attempt.state!='prepared' or attempt.dispatch_started_at is not None:
            raise ValueError('ATTEMPT_ALREADY_DISPATCHED')
        source=s.get(WorkerJob,old.job_id) if old.job_id else None
        if not source or source.feature_id!=action.feature_id:
            raise ValueError('WORKER_OPERATION_BINDING_UNIMPLEMENTED')
        job_id=enqueue_job_in_session(s,feature_id=source.feature_id,repository_id=source.repository_id,
            base_sha=source.base_sha,branch_name=source.branch_name,toolchain_ref=source.toolchain_ref,now=utc_now())
        s.add(ResumeEpisode(intent_id=intent_id,attempt_id=attempt.attempt_id,job_id=job_id,created_at=utc_now()))
        return job_id
    return _transaction(engine,work)


def consume_pending(engine):
    # Separate short transactions; no filesystem, network or process work.
    def pending(s):
        return list(s.scalars(select(DispatchIntent.intent_id).where(
            ~select(ResumeEpisode.intent_id).where(ResumeEpisode.intent_id==DispatchIntent.intent_id).exists())))
    for intent_id in _transaction(engine,pending):
        try:consume_intent(engine,intent_id=intent_id)
        except ValueError:pass  # Explicit context/dispatch reads expose the refusal.


def _issue_policy_lease(s, *, episode, intent, attempt, action, job, worker_id):
    now=utc_now()
    worker=s.get(WorkerEnrollment,worker_id)
    if not worker or worker.revoked_at is not None:
        raise ValueError('WORKER_ENROLLMENT_STALE')
    if (job.feature_id!=action.feature_id or job.worker_id!=worker_id
            or job.state not in ('leased','running') or job.lease_expires_at is None
            or job.lease_expires_at<=now):
        raise ValueError('JOB_LEASE_STALE')
    budget=s.scalar(select(ReplacementBudget).where(ReplacementBudget.new_attempt_id==attempt.attempt_id))
    approval=s.get(Approval,budget.approval_id)
    binding=s.get(ResumeApprovalBinding,budget.approval_id)
    receipt=s.get(ResumeReceipt,budget.receipt_id)
    if (not approval or not binding or not receipt or approval.action!='resume'
            or approval.feature_id!=action.feature_id or approval.consumed_at is None
            or approval.consumed_by_command_id!=receipt.request_id
            or budget.new_action_id!=action.action_id or episode.attempt_id!=attempt.attempt_id
            or budget.receipt_id!=intent.receipt_id):
        raise ValueError('CONSUMED_RESUME_BINDING_INVALID')
    if s.get(ResumeRevocation,binding.decision_id):raise ValueError('APPROVAL_REVOKED')
    issued=s.get(ResumeLeaseIssuance,(episode.intent_id,job.lease_epoch))
    if issued:
        lease=s.get(Lease,issued.lease_id)
        if _leases(job,lease,action.feature_id,worker_id,now):raise ValueError('POLICY_LEASE_STALE')
        return lease
    # Fresh reclaim gets a distinct lease, within the original approval and
    # first-issuance ceiling. It never revives the old lease.
    previous=list(s.scalars(select(ResumeLeaseIssuance).where(ResumeLeaseIssuance.intent_id==episode.intent_id)))
    ceiling=min(now+timedelta(minutes=15),approval.expires_at,job.lease_expires_at)
    for prior in previous:
        lease=s.get(Lease,prior.lease_id)
        if (prior.job_lease_epoch>=job.lease_epoch or lease is None
                or lease.revoked_at is not None):
            raise ValueError('POLICY_LEASE_STALE')
        ceiling=min(ceiling,lease.created_at+timedelta(minutes=15))
    if ceiling<=now:raise ValueError('RESUME_LEASE_CEILING_EXPIRED')
    # A client or legacy producer cannot inject the replacement lease.
    if s.scalar(select(Lease.lease_id).where(Lease.job_id==job.job_id,Lease.epoch==job.lease_epoch)):
        raise ValueError('POLICY_LEASE_BINDING_CONFLICT')
    lease=Lease(lease_id=new_id(),feature_id=action.feature_id,job_id=job.job_id,
        worker_id=worker_id,epoch=job.lease_epoch,expires_at=ceiling,created_at=now)
    s.add(lease)
    s.flush()
    s.add(ResumeLeaseIssuance(intent_id=episode.intent_id,job_lease_epoch=job.lease_epoch,lease_id=lease.lease_id))
    return lease


def _context(s,*,job_id,worker_id,job_lease_epoch):
    episode=s.scalar(select(ResumeEpisode).where(ResumeEpisode.job_id==job_id))
    if not episode:return None
    intent=s.get(DispatchIntent,episode.intent_id)
    attempt,action,selection,snapshot,old,isolation=_binding(s,intent)
    if attempt.state!='prepared' or attempt.dispatch_started_at is not None:
        raise ValueError('ATTEMPT_ALREADY_DISPATCHED')
    job=s.get(WorkerJob,job_id)
    if not job or job.lease_epoch!=job_lease_epoch:raise ValueError('JOB_LEASE_STALE')
    lease=_issue_policy_lease(s,episode=episode,intent=intent,attempt=attempt,action=action,
        job=job,worker_id=worker_id)
    refusal=_leases(job,lease,action.feature_id,worker_id,utc_now())
    if refusal:raise ValueError(refusal)
    if isolation and isolation['worker_id']!=worker_id:raise ValueError('ISOLATION_WORKER_MISMATCH')
    return dict(intent_id=intent.intent_id,attempt_id=attempt.attempt_id,attempt_version=attempt.version,
        feature_id=action.feature_id,action_id=action.action_id,job_id=job_id,worker_id=worker_id,
        job_lease_epoch=job.lease_epoch,lease_id=lease.lease_id,policy_lease_epoch=lease.epoch,
        snapshot_sha256=snapshot.sha256,snapshot=json.loads(snapshot.body),selection_id=selection.selection_id,
        isolation=isolation)


def prelaunch_context(engine,*,job_id,worker_id,job_lease_epoch):
    return _transaction(engine,lambda s:_context(s,job_id=job_id,worker_id=worker_id,job_lease_epoch=job_lease_epoch))


def validate_manifest(s,*,attempt,job,lease,owner):
    episode=s.scalar(select(ResumeEpisode).where(ResumeEpisode.attempt_id==attempt.attempt_id))
    if not episode or episode.job_id!=job.job_id:return 'REPLACEMENT_EPISODE_REQUIRED'
    try:context=_context(s,job_id=job.job_id,worker_id=owner,job_lease_epoch=job.lease_epoch)
    except ValueError as exc:return str(exc)
    manifest=s.get(SupervisorLaunchManifest,attempt.attempt_id)
    if not manifest:return 'PRELAUNCH_MANIFEST_REQUIRED'
    body=json.loads(manifest.body)
    if digest(body)!=manifest.sha256:return 'MANIFEST_INVALID'
    try:_current(s,body)
    except ValueError as exc:return str(exc)
    if body['expires_at']<=int(utc_now().timestamp()):return 'MANIFEST_EXPIRED'
    if not body.get('inventory_sha256') or not body.get('reservation_id'):return 'PRELAUNCH_INVENTORY_REQUIRED'
    for key,value in dict(worker_id=owner,job_id=job.job_id,job_lease_epoch=job.lease_epoch,
                          lease_id=lease.lease_id,policy_lease_epoch=lease.epoch).items():
        if body.get(key)!=value:return 'MANIFEST_AUTHORITY_STALE'
    isolation=context['isolation']
    if isolation and (body['workspace_id']!=isolation['new_workspace_id'] or
                       body['workspace_generation']!=isolation['workspace_generation']):
        return 'ISOLATION_RESERVATION_STALE'
    return None


def acknowledge_manifest(engine, *, job_id, worker_id, job_lease_epoch, assertion):
    """Authenticate and recheck before persisting the signed inventory reference."""
    from personal_agent_dal.machine.isolation_evidence import record_launch_manifest
    def work(s):
        context=_context(s,job_id=job_id,worker_id=worker_id,job_lease_epoch=job_lease_epoch)
        if context is None:raise ValueError('REPLACEMENT_EPISODE_REQUIRED')
        sha=record_launch_manifest(engine,assertion=assertion,worker_id=worker_id,transaction_session=s)
        s.flush()
        row=s.get(SupervisorLaunchManifest,context['attempt_id'])
        if not row or row.sha256!=sha:raise ValueError('MANIFEST_TARGET_MISMATCH')
        refusal=validate_manifest(s,attempt=s.get(ProviderAttempt,context['attempt_id']),
            job=s.get(WorkerJob,job_id),lease=s.get(Lease,context['lease_id']),owner=worker_id)
        if refusal:raise ValueError(refusal)
        return {'manifest_sha256':sha}
    return _transaction(engine,work)


def dispatch_prelaunch(engine, *, job_id, worker_id, job_lease_epoch, manifest_sha256):
    from personal_agent_dal.machine.action_lifecycle import claim_dispatch
    context=prelaunch_context(engine,job_id=job_id,worker_id=worker_id,job_lease_epoch=job_lease_epoch)
    if not context:raise ValueError('REPLACEMENT_EPISODE_REQUIRED')
    result=claim_dispatch(engine,attempt_id=context['attempt_id'],expected_version=context['attempt_version'],
        owner_id=worker_id,job_id=job_id,lease_id=context['lease_id'],manifest_sha256=manifest_sha256)
    return {'code':result.code}


def check_episode_heartbeat(engine, *, job_id, worker_id, job_lease_epoch):
    """Both leases must still be live; an expired policy lease is never revived."""
    def work(s):
        episode=s.scalar(select(ResumeEpisode).where(ResumeEpisode.job_id==job_id))
        if not episode:return True
        attempt,action,*_=_binding(s,s.get(DispatchIntent,episode.intent_id))
        job=s.get(WorkerJob,job_id)
        if not job or job.lease_epoch!=job_lease_epoch:return False
        issued=s.get(ResumeLeaseIssuance,(episode.intent_id,job_lease_epoch))
        if not issued:return False
        lease=s.get(Lease,issued.lease_id)
        if attempt.lease_id is not None and attempt.lease_id!=issued.lease_id:return False
        if _leases(job,lease,action.feature_id,worker_id,utc_now()):return False
        if attempt.dispatch_started_at and (attempt.job_lease_epoch!=job.lease_epoch or attempt.policy_lease_epoch!=lease.epoch):return False
        # Keep the existing policy expiry. No new renewal policy is authorized.
        return True
    try:return _transaction(engine,work)
    except ValueError:return False
