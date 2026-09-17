"""Evidence arrival and atomic report closure. No provider or Feature resolver."""
import json
import re
from sqlalchemy import select, update
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import (
    _transaction, _authority, _issued_leases, _record_result_in_session, _stop_gate,
)
from personal_agent_dal.machine.workflow_selection import digest
from personal_agent_dal.machine.execution_protocol import ExecutionResult, ExecutionResultRequest, ExecutionResultResponse, ExecutionStatus
from personal_agent_dal.machine.execution_manifest import LaunchManifestV11
from personal_agent_dal.storage.machine_models import (
    ProviderAttempt, WorkflowAction, ExecutionGate, Lease, ExecutionJobBinding,
    ExecutionResultEnvelope, ProviderResultObservation,
)
from personal_agent_dal.storage.transport_models import WorkerEnrollment, SupervisorLaunchManifest
from personal_agent_dal.storage.worker_models import WorkerJob, WorkerResultReceipt
from personal_agent_dal.worker.queue import _finish_job_in_session

# Scan every string, including nested output, before semantic binding or persistence.
_SECRET = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16})\b|\bBearer\s+[^\s"\\]+|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*[^\s,"}]+', re.I | re.S)


def canonical_result(value):
    raw = value.model_dump(by_alias=True) if isinstance(value, ExecutionResult) else value
    if len(canonical_json(raw).encode()) > 256*1024: raise ValueError('RESULT_TOO_LARGE')
    changed = False
    def scan(v):
        nonlocal changed
        if isinstance(v, str):
            clean = _SECRET.sub('[REDACTED]', v)
            changed |= clean != v
            return clean
        if isinstance(v, list): return [scan(x) for x in v]
        if isinstance(v, dict): return {scan(k):scan(x) for k,x in v.items()}
        return v
    clean = scan(raw)
    if changed: clean['redacted'] = True
    body = ExecutionResult.model_validate(clean).model_dump(by_alias=True)
    return body, digest(body)


def _historical(s, job_id, worker_id):
    worker = s.get(WorkerEnrollment, worker_id)
    binding = s.scalar(select(ExecutionJobBinding).where(ExecutionJobBinding.job_id == job_id))
    job = s.get(WorkerJob, job_id)
    if not worker or worker.revoked_at or not binding or not job or job.execution_mode != 'provider_v1':
        raise ValueError('EXECUTION_BINDING_REQUIRED')
    attempt = s.get(ProviderAttempt, binding.attempt_id)
    manifest = s.get(SupervisorLaunchManifest, binding.attempt_id)
    body = json.loads(manifest.body) if manifest else None
    if body:
        body = LaunchManifestV11.model_validate(body).model_dump(by_alias=True)
        if digest(body) != manifest.sha256: raise ValueError('MANIFEST_INVALID')
        if (body['worker_id'] != worker_id or body['machine_id'] != worker.machine_id
                or body['registration_epoch'] != worker.registration_epoch):
            raise ValueError('EXECUTION_WORKER_MISMATCH')
    elif job.worker_id != worker_id:
        raise ValueError('EXECUTION_WORKER_MISMATCH')
    if attempt.job_id != job_id or (body and (body['job_id'] != job_id or body['attempt_id'] != attempt.attempt_id)):
        raise ValueError('EXECUTION_BINDING_STALE')
    return job, attempt, manifest, body


def _static(s, job_id, worker_id, body):
    job, a, manifest, mb = _historical(s, job_id, worker_id)
    if not mb or a.dispatch_started_at is None: raise ValueError('EXECUTION_NOT_DISPATCHED')
    spec = mb['execution_spec']
    expected = {k:spec[k] for k in ('feature_id','action_id','attempt_id','job_id','worker_id','job_lease_epoch','lease_id','policy_lease_epoch','snapshot_sha256','execution_role')}
    expected.update(manifest_sha256=manifest.sha256,execution_spec_sha256=mb['execution_spec_sha256'],
                    fence=spec['expected_dispatch_fence'],attempt_version=spec['prepared_attempt_version']+1)
    if any(body[k] != v for k,v in expected.items()) or a.owner_id != worker_id or a.fence != body['fence']:
        raise ValueError('RESULT_BINDING_MISMATCH')
    return job, a


def known_complete(body):
    return body['outcome'] != 'unknown' and body['stop']['process_exited'] and not body['truncated']


def has_complete_evidence(s, attempt_id):
    return any(known_complete(json.loads(body)) for body in s.scalars(select(ExecutionResultEnvelope.body).where(
        ExecutionResultEnvelope.attempt_id == attempt_id)))


def execution_status(engine, *, job_id, worker_id, kill_switch=lambda: False):
    def work(s):
        job,a,m,_ = _historical(s,job_id,worker_id)
        action = s.get(WorkflowAction,a.action_id)
        gate = s.get(ExecutionGate,action.feature_id)
        now = utc_now()
        from personal_agent_dal.storage.machine_models import ExecutionPolicyLeaseIssuance
        issued = s.get(ExecutionPolicyLeaseIssuance,(a.attempt_id,job.lease_epoch))
        lease = s.get(Lease,a.lease_id or issued.lease_id) if a.lease_id or issued else None
        jvalid = bool(job.worker_id == worker_id and job.state in ('leased','running') and job.lease_expires_at and job.lease_expires_at > now and (a.job_lease_epoch is None or a.job_lease_epoch == job.lease_epoch))
        pvalid = bool(lease and not lease.revoked_at and lease.expires_at > now and (a.policy_lease_epoch is None or lease.epoch == a.policy_lease_epoch))
        evidence = list(s.scalars(select(ExecutionResultEnvelope.result_sha256).where(ExecutionResultEnvelope.attempt_id == a.attempt_id).order_by(ExecutionResultEnvelope.recorded_at.desc(),ExecutionResultEnvelope.result_sha256).limit(65)))
        classification = ('report_complete' if a.report_receipt_id else 'consumed' if a.result_consumed_at else
            'result_available_not_accepted' if a.result_digest or has_complete_evidence(s,a.attempt_id) else
            'execution_effects_unknown' if a.state == 'unknown' else 'running' if a.dispatch_started_at else 'prepared')
        return ExecutionStatus.model_validate(dict(schema='dal.worker-execution-transport/1.0',job_id=job_id,
            attempt_id=a.attempt_id,attempt_state=a.state,attempt_version=a.version,fence=a.fence,
            manifest_sha256=m.sha256 if m else None,job_lease_valid=jvalid,policy_lease_valid=pvalid,
            stop_required=bool(kill_switch() or not jvalid or not pvalid or _authority(s,a,action,gate) or a.report_receipt_id or a.result_consumed_at or classification in ('result_available_not_accepted','execution_effects_unknown')),
            result_digest=a.result_digest,report_receipt_id=a.report_receipt_id,consumption_receipt_id=a.consumption_receipt_id,
            evidence_digests=evidence[:64],evidence_truncated=len(evidence)>64,classification=classification)).model_dump(by_alias=True)
    return _transaction(engine,work)


def submit_execution_result(engine, *, job_id, worker_id, request, kill_switch=lambda: False):
    request = ExecutionResultRequest.model_validate(request) if isinstance(request,dict) else request
    if len(canonical_json(request.model_dump(by_alias=True)).encode()) > 256*1024:
        raise ValueError('RESULT_TOO_LARGE')
    body, sha = canonical_result(request.result)
    if sha != request.result_sha256: raise ValueError('RESULT_DIGEST_MISMATCH')
    def arrival(s):
        _,a = _static(s,job_id,worker_id,body)
        if not s.get(ExecutionResultEnvelope,(a.attempt_id,sha)):
            s.add(ExecutionResultEnvelope(attempt_id=a.attempt_id,result_sha256=sha,body=canonical_json(body),recorded_at=utc_now()))
            s.add(ProviderResultObservation(observation_id=new_id(),attempt_id=a.attempt_id,owner_id=worker_id,
                fence=body['fence'],expected_version=body['attempt_version'],digest=sha,code='RESULT_ARRIVED',recorded_at=utc_now()))
    _transaction(engine,arrival)
    def work(s):
        job,a = _static(s,job_id,worker_id,body)
        action = s.get(WorkflowAction,a.action_id)
        gate = s.get(ExecutionGate,action.feature_id)
        state = 'succeeded' if body['outcome']=='succeeded' else 'failed'
        error = body['reason'] if state=='failed' else None
        def response(code, accepted=False, replay=False, receipt=None, reason=None):
            return ExecutionResultResponse.model_validate(dict(schema='dal.worker-execution-transport/1.0',job_id=job_id,
                attempt_id=a.attempt_id,code=code,reason=reason,accepted=accepted,replay=replay,receipt_id=receipt,
                result_sha256=sha,job_state=job.state)).model_dump(by_alias=True)
        if a.report_receipt_id:
            receipt = s.get(WorkerResultReceipt,a.report_receipt_id)
            if (receipt and receipt.job_id==job_id and receipt.result_sha256==sha and a.result_digest==sha
                    and job.result_sha256==sha and job.state==state and job.last_error==error and a.state=='result_recorded'):
                return response('REPORT_REPLAY',True,True,receipt.receipt_id)
            return response('RESULT_CONFLICT')
        if a.result_digest and a.result_digest != sha: return response('RESULT_CONFLICT')
        arrivals = list(s.scalars(select(ExecutionResultEnvelope.body).where(ExecutionResultEnvelope.attempt_id==a.attempt_id)))
        if any(json.loads(v)['request_id']==body['request_id'] and digest(json.loads(v))!=sha for v in arrivals):
            return response('RESULT_CONFLICT')
        from personal_agent_dal.machine.execution_start import execution_binding
        binding_refusal = None
        try:
            execution_binding(s,a,job)
            if action.execution_role != body['execution_role'] or action.execution_snapshot_sha256 != body['snapshot_sha256']:
                binding_refusal = 'EXECUTION_BINDING_STALE'
        except ValueError as exc:
            binding_refusal = str(exc)
        refusal = ('KILL_SWITCH_ACTIVE' if kill_switch() else
            binding_refusal if binding_refusal else
            'COMPLETION_RESOLVER_UNAVAILABLE' if action.completion_mode!='report_only' else
            'JOB_CANCELLED' if job.state=='cancelled' else
            'ATTEMPT_VERSION_STALE' if a.version!=body['attempt_version'] else _authority(s,a,action,gate) or _issued_leases(s,a,action,utc_now()))
        if not known_complete(body) or refusal or a.state == 'unknown':
            code = refusal or ('ATTEMPT_UNKNOWN' if a.state=='unknown' else 'EXECUTION_EFFECTS_UNKNOWN')
            # Evidence never restores authority. Park only this current execution.
            if action.active_attempt_id==a.attempt_id:
                if gate and gate.mode=='open' and gate.approval_epoch==a.approval_epoch:
                    _stop_gate(s,action.feature_id,gate.version,'paused',utc_now())
                if not known_complete(body) and a.state=='dispatching':
                    a.state='unknown'; a.version+=1
                if job.state in ('leased','running'):
                    job.state='expired'; job.last_error=('result_available_not_accepted' if known_complete(body) else 'execution_effects_unknown')
                    job.worker_id=None; job.lease_expires_at=None; job.heartbeat_at=None
            s.add(ProviderResultObservation(observation_id=new_id(),attempt_id=a.attempt_id,owner_id=worker_id,
                fence=body['fence'],expected_version=body['attempt_version'],digest=sha,code=code,recorded_at=utc_now()))
            return response('RESULT_AVAILABLE_NOT_ACCEPTED' if known_complete(body) else 'EXECUTION_EFFECTS_UNKNOWN', reason=code)
        outcome = _record_result_in_session(s,attempt_id=a.attempt_id,expected_version=body['attempt_version'],
            owner_id=worker_id,fence=body['fence'],digest=sha)
        if outcome.code not in ('RESULT_RECORDED','RESULT_REPLAY'): raise ValueError(outcome.code)
        if not _finish_job_in_session(s,job_id=job_id,worker_id=worker_id,lease_epoch=body['job_lease_epoch'],state=state,result_sha256=sha,last_error=error):
            raise ValueError('RESULT_FINISH_STALE')
        s.flush(); s.expire_all()
        a=s.get(ProviderAttempt,body['attempt_id']); action=s.get(WorkflowAction,a.action_id); gate=s.get(ExecutionGate,action.feature_id)
        receipt=s.scalar(select(WorkerResultReceipt).where(WorkerResultReceipt.job_id==job_id))
        if not receipt: raise ValueError('RESULT_RECEIPT_MISSING')
        a.report_receipt_id=receipt.receipt_id; a.version+=1; a.updated_at=utc_now()
        if action.active_attempt_id != a.attempt_id: raise ValueError('ATTEMPT_OWNERSHIP_LOST')
        action.active_attempt_id=None; action.version+=1; action.updated_at=utc_now()
        if gate.mode!='open' or gate.approval_epoch!=a.approval_epoch: raise ValueError('EXECUTION_AUTHORIZATION_STALE')
        if _stop_gate(s,action.feature_id,gate.version,'paused',utc_now()).code != 'PAUSED':
            raise ValueError('EXECUTION_GATE_STALE')
        s.flush(); s.refresh(job)
        return response('REPORT_ACCEPTED',True,False,receipt.receipt_id)
    return _transaction(engine,work)
