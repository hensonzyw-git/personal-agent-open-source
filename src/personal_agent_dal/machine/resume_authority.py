"""Authenticated approval import and consume-once replacement, without execution."""
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, update
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest, Version, digest
from typing import Literal
from pydantic import model_validator
from personal_agent_dal.machine.action_lifecycle import _transaction
from personal_agent_dal.machine.binding import build_state_binding
from personal_agent_dal.machine.registry import jcs_sha256
from personal_agent_dal.storage.models import Feature
from personal_agent_dal.storage.machine_models import (
    Approval, WorkflowAction, ProviderAttempt, ExecutionGate, ExecutionSnapshot, WorkflowSelection,
    ResumeProposal, ResumeApprovalBinding, ResumeRevocation, ResumeReceipt, ReplacementBudget, DispatchIntent,
)


class ResumeRequest(Closed):
    request_id: Id
    approval_id: Id


class ResumeBinding(Closed):
    feature_id: Id
    feature_version: Version
    gate_version: Version
    action_id: Id
    action_version: Version
    old_attempt_id: Id
    old_attempt_version: Version
    selection_id: Id
    snapshot_sha256: Digest
    input_sha256: Digest
    state_sha256: Digest
    artifact_sha256: Digest
    reason_code: Literal['unknown-result', 'user-pause']
    replacement_limit: Literal[1]
    isolation_id: Id | None
    isolation_binding_sha256: Digest | None

    @model_validator(mode='after')
    def isolation_pair(self):
        required = self.reason_code == 'unknown-result'
        if (self.isolation_id is not None) != required or (self.isolation_binding_sha256 is not None) != required:
            raise ValueError('ISOLATION_BINDING_INVALID')
        return self


def _binding(s, feature_id, selection_id, *, evidence_id=None):
    f, g = s.get(Feature, feature_id), s.get(ExecutionGate, feature_id)
    selection = s.get(WorkflowSelection, selection_id)
    if not f or not g or g.mode != 'paused' or f.state in ('completed','cancelled'):
        raise ValueError('RESUME_STATE_INVALID')
    if not selection or selection.feature_id != feature_id or (selection.feature_version,selection.gate_version) != (f.version,g.version):
        raise ValueError('SELECTION_STALE')
    latest = s.scalar(select(WorkflowSelection).where(WorkflowSelection.feature_id == feature_id).order_by(
        WorkflowSelection.created_at.desc(), WorkflowSelection.selection_id.desc()).limit(1))
    if latest.selection_id != selection_id: raise ValueError('SELECTION_STALE')
    snapshot = s.get(ExecutionSnapshot, selection.snapshot_sha256)
    if not snapshot or digest(json.loads(snapshot.body)) != snapshot.sha256:
        raise ValueError('SNAPSHOT_INVALID')
    actions = list(s.scalars(select(WorkflowAction).where(WorkflowAction.feature_id == feature_id,
                                                        WorkflowAction.active_attempt_id.is_not(None))))
    if len(actions) != 1: raise ValueError('ACTION_AMBIGUOUS')
    a = actions[0]
    if json.loads(snapshot.body)['input_sha256'] != a.input_binding_sha256:
        raise ValueError('SELECTION_INPUT_STALE')
    old = s.get(ProviderAttempt, a.active_attempt_id)
    if not old or old.state == 'superseded' or old.result_consumed_at:
        raise ValueError('ATTEMPT_INVALID')
    if not f.artifact_sha256: raise ValueError('ARTIFACT_BINDING_MISSING')
    unknown = old.dispatch_started_at is not None and old.result_consumed_at is None
    isolation_id = isolation_sha = None
    if unknown:
        from personal_agent_dal.machine.isolation_evidence import current_evidence
        evidence = current_evidence(s, old, a, isolation_id=evidence_id)
        isolation_id, isolation_sha = evidence.isolation_id, evidence.binding_sha256
    body = dict(feature_id=f.feature_id, feature_version=f.version, gate_version=g.version,
        action_id=a.action_id, action_version=a.version, old_attempt_id=old.attempt_id,
        old_attempt_version=old.version, selection_id=selection_id, snapshot_sha256=snapshot.sha256,
        input_sha256=a.input_binding_sha256, state_sha256=jcs_sha256(build_state_binding(f)),
        artifact_sha256=f.artifact_sha256, reason_code='unknown-result' if unknown else 'user-pause',
        replacement_limit=1, isolation_id=isolation_id, isolation_binding_sha256=isolation_sha)
    return ResumeBinding.model_validate(body).model_dump(), f, g, a, old


def propose(engine, *, feature_id, selection_id, request_id):
    def work(s):
        previous = s.scalar(select(ResumeProposal).where(ResumeProposal.request_id == request_id))
        binding, _, _, source_action, _ = _binding(s, feature_id, selection_id,
            evidence_id=json.loads(previous.binding)['isolation_id'] if previous else None)
        sha = digest(binding)
        if previous:
            if previous.binding_sha256 != sha: raise ValueError('IDEMPOTENCY_CONFLICT')
            row = previous
        else:
            row = ResumeProposal(proposal_id=new_id(), request_id=request_id, binding=canonical_json(binding),
                source_snapshot_sha256=source_action.execution_snapshot_sha256,
                binding_sha256=sha, expires_at=utc_now()+timedelta(minutes=15))
            s.add(row)
        if row.expires_at <= utc_now(): raise ValueError('PROPOSAL_EXPIRED')
        return dict(proposal_id=row.proposal_id, binding=json.loads(row.binding),
                    binding_sha256=row.binding_sha256, expires_at=row.expires_at.isoformat())
    return _transaction(engine, work)


class RevokeRequest(Closed):
    request_id: Id


def revoke_decision(engine, *, decision_id, body=None, actor='local-service'):
    # Existing in-process callers retain their operation-specific entry point.
    body = body or RevokeRequest(request_id='revoke:'+digest(dict(decision_id=decision_id)))
    from pydantic import TypeAdapter
    TypeAdapter(Id).validate_python(decision_id)
    from personal_agent_dal.storage.machine_models import ResumeRevokeReceipt
    request_sha = digest(dict(decision_id=decision_id, actor=actor, **body.model_dump()))
    def work(s):
        previous=s.get(ResumeRevokeReceipt,body.request_id)
        if previous:
            if previous.request_sha256 != request_sha: raise ValueError('IDEMPOTENCY_CONFLICT')
            return json.loads(previous.body)
        now=utc_now()
        if not s.get(ResumeRevocation, decision_id):
            s.add(ResumeRevocation(decision_id=decision_id, recorded_at=now))
        s.flush()
        receipt_id=new_id()
        result=dict(receipt_id=receipt_id,decision_id=decision_id,status='revoked_for_future_use')
        s.add(ResumeRevokeReceipt(request_id=body.request_id,receipt_id=receipt_id,
            request_sha256=request_sha,decision_id=decision_id,actor_id=actor,
            body=canonical_json(result),recorded_at=now))
        from personal_agent_dal.storage.audit import append_audit_event
        append_audit_event(s,event_id=receipt_id,trace_id=receipt_id,event_type='execution.approval_revoke',
            redacted_summary='explicit approval revoked for future use; no execution undo',now=now)
        return result
    return _transaction(engine, work)


def import_decision(engine, *, assertion, keys, issuer, audience, now=None):
    from personal_agent.api.dal_client import verify_decision
    timestamp = now or utc_now()
    claims = verify_decision(assertion, keys=keys, issuer=issuer, audience=audience,
                             now_epoch=int(timestamp.timestamp()))
    sha = digest(claims)
    def work(s):
        if s.get(ResumeRevocation, claims['decision_id']): raise ValueError('APPROVAL_REVOKED')
        previous = s.scalar(select(ResumeApprovalBinding).where(
            (ResumeApprovalBinding.decision_id == claims['decision_id']) | (ResumeApprovalBinding.jti == claims['jti'])))
        if previous:
            if previous.claims_sha256 != sha: raise ValueError('DECISION_CONFLICT')
            return dict(decision_id=previous.decision_id, approval_id=previous.approval_id, status='accepted')
        if claims['decision'] == 'reject': raise ValueError('DECISION_REJECTED')
        p = s.scalar(select(ResumeProposal).where(ResumeProposal.binding_sha256 == claims['binding_sha256'],
            ResumeProposal.expires_at > timestamp).order_by(ResumeProposal.expires_at.desc()).limit(1))
        if not p or p.expires_at <= timestamp: raise ValueError('PROPOSAL_EXPIRED')
        binding = json.loads(p.binding)
        current, f, _, action, _ = _binding(s, binding['feature_id'], binding['selection_id'], evidence_id=binding['isolation_id'])
        if action.execution_snapshot_sha256 != p.source_snapshot_sha256: raise ValueError('SOURCE_SNAPSHOT_STALE')
        if current != binding or digest(binding) != p.binding_sha256: raise ValueError('BINDING_STALE')
        if binding['isolation_id'] and claims['decision'] != 'approve_once_accept_duplicate_cost':
            raise ValueError('DUPLICATE_COST_NOT_ACCEPTED')
        approval_id = new_id()
        s.add(Approval(approval_id=approval_id, action='resume', feature_id=f.feature_id,
            decision_id=claims['decision_id'], decision_version=1, expected_feature_version=f.version,
            expected_state=f.state, state_sha256=binding['state_sha256'], artifact_sha256=binding['artifact_sha256'],
            device_id=claims['device_id'], subject_id=claims['subject_id'], valid_from=timestamp,
            expires_at=min(p.expires_at,datetime.fromtimestamp(claims['exp'],timezone.utc)),
            idempotency_key='resume:'+claims['decision_id'], policy_version=f.policy_version,
            replay_policy='consume_once', recorded_at=timestamp))
        s.flush()  # Approval precedes its authority binding.
        s.add(ResumeApprovalBinding(approval_id=approval_id, proposal_id=p.proposal_id,
            decision_id=claims['decision_id'], jti=claims['jti'], claims=canonical_json(claims),
            claims_sha256=sha, key_thumbprint=claims['key_thumbprint']))
        return dict(decision_id=claims['decision_id'], approval_id=approval_id, status='accepted')
    return _transaction(engine, work)


def resume(engine, *, feature_id, body, now=None, kill_switch=None):
    request_sha = digest(dict(feature_id=feature_id, **body.model_dump()))
    def work(s):
        previous = s.scalar(select(ResumeReceipt).where(ResumeReceipt.request_id == body.request_id))
        if previous:
            if previous.request_sha256 != request_sha: raise ValueError('IDEMPOTENCY_CONFLICT')
            return json.loads(previous.body)
        if kill_switch is not None and kill_switch(): raise ValueError('KILL_SWITCH_ACTIVE')
        timestamp = now or utc_now()
        auth = s.get(ResumeApprovalBinding, body.approval_id)
        approval = s.get(Approval, body.approval_id)
        if not auth or not approval or approval.action != 'resume' or approval.feature_id != feature_id:
            raise ValueError('APPROVAL_INVALID')
        if s.get(ResumeRevocation, auth.decision_id) or not approval.valid_from <= timestamp < approval.expires_at:
            raise ValueError('APPROVAL_INVALID')
        p = s.get(ResumeProposal, auth.proposal_id)
        binding = json.loads(p.binding)
        current, f, g, a, old = _binding(s, feature_id, binding['selection_id'], evidence_id=binding['isolation_id'])
        if a.execution_snapshot_sha256 != p.source_snapshot_sha256: raise ValueError('SOURCE_SNAPSHOT_STALE')
        if current != binding or digest(binding) != p.binding_sha256 or timestamp >= p.expires_at:
            raise ValueError('BINDING_STALE')
        if s.get(ReplacementBudget, old.attempt_id): raise ValueError('REPLACEMENT_ALREADY_RESERVED')
        result = s.execute(update(Approval).where(Approval.approval_id == approval.approval_id,
            Approval.consumed_by_command_id.is_(None)).values(consumed_by_command_id=body.request_id, consumed_at=timestamp))
        if result.rowcount != 1: raise ValueError('APPROVAL_CONSUMED')
        old.state = 'superseded'
        old.version += 1
        old.updated_at = timestamp
        g.mode = 'open'
        g.version += 1
        g.approval_epoch += 1
        g.updated_at = timestamp
        f.capability_epoch += 1
        f.version += 1
        f.updated_at = timestamp
        old_action_id = a.action_id
        new_attempt_id, receipt_id = new_id(), new_id()
        if a.execution_snapshot_sha256 != binding['snapshot_sha256'] or a.input_binding_sha256 != binding['input_sha256']:
            a.active_attempt_id = None
            a.version += 1
            a.updated_at = timestamp
            a = WorkflowAction(action_id=new_id(), feature_id=feature_id, stage_id=a.stage_id, kind=a.kind,
                action_key='resume:'+receipt_id, input_binding_sha256=binding['input_sha256'],
                execution_snapshot_sha256=binding['snapshot_sha256'], version=1,
                active_attempt_id=new_attempt_id, created_at=timestamp, updated_at=timestamp)
            s.add(a)
            number = 1
        else:
            a.version += 1
            a.active_attempt_id = new_attempt_id
            a.updated_at = timestamp
            number = old.attempt_no+1
        s.flush()
        s.add(ProviderAttempt(attempt_id=new_attempt_id, action_id=a.action_id, attempt_no=number,
            state='prepared', version=1, owner_id=None, fence=old.fence+1, approval_epoch=g.approval_epoch,
            feature_version=f.version, capability_epoch=f.capability_epoch, created_at=timestamp, updated_at=timestamp))
        s.flush()  # The replacement attempt must exist before any references.
        if binding['isolation_id']:
            from personal_agent_dal.storage.transport_models import IsolationEvidence
            evidence = s.get(IsolationEvidence, binding['isolation_id'])
            if evidence.reserved_by: raise ValueError('ISOLATION_ALREADY_RESERVED')
            evidence.reserved_by = new_attempt_id
        receipt = dict(receipt_id=receipt_id, code='RESUMED', old_attempt_id=old.attempt_id,
                       new_attempt_id=new_attempt_id, gate_version=g.version)
        s.add(ResumeReceipt(receipt_id=receipt_id, request_id=body.request_id, request_sha256=request_sha,
                            body=canonical_json(receipt), recorded_at=timestamp))
        s.flush()  # Receipt precedes budget and dispatch intent.
        s.add(ReplacementBudget(old_attempt_id=old.attempt_id,new_attempt_id=new_attempt_id,
            old_action_id=old_action_id,new_action_id=a.action_id,approval_id=approval.approval_id,receipt_id=receipt_id))
        s.add(DispatchIntent(intent_id=new_id(),attempt_id=new_attempt_id,receipt_id=receipt_id,
            selection_id=binding['selection_id'],status='awaiting_episode',created_at=timestamp))
        from personal_agent_dal.storage.audit import append_audit_event
        append_audit_event(s,event_id=receipt_id,trace_id=f.trace_id,event_type='execution.resume',
                          redacted_summary='consume-once replacement reserved; execution pending',now=timestamp)
        return receipt
    return _transaction(engine, work)


def decision_status(engine, *, decision_id):
    """Resolve PA's returned decision ID without exposing assertion or device data."""
    from personal_agent_dal.storage.engine import session_factory
    with session_factory(engine)() as s:
        binding=s.scalar(select(ResumeApprovalBinding).where(ResumeApprovalBinding.decision_id==decision_id))
        if binding is None: raise ValueError('DECISION_NOT_IMPORTED')
        approval=s.get(Approval,binding.approval_id)
        if s.get(ResumeRevocation,decision_id): status='revoked'
        elif approval.consumed_at: status='consumed'
        elif approval.expires_at<=utc_now(): status='expired'
        else: status='accepted'
        return dict(decision_id=decision_id,approval_id=approval.approval_id,status=status)
