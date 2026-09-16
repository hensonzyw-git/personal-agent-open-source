"""Business registration and authenticated report reads, never generic writes."""
import hashlib
import json
import hmac
from typing import Annotated
from fastapi import Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from personal_agent_core.manifest import canonical_json
from personal_agent_dal.machine.workflow_selection import Id, digest
from personal_agent_dal.service.execution_config import ApprovedInput
from personal_agent_dal.storage.engine import session_factory


class RegisterRequest(ApprovedInput):
    task_description: Annotated[str, Field(strict=True, min_length=1, max_length=32768)]


def read_report(engine, *, attempt_id):
    from personal_agent_dal.storage.machine_models import ProviderAttempt, WorkflowAction, ExecutionResultEnvelope
    from personal_agent_dal.storage.models import Feature
    from personal_agent_dal.storage.worker_models import WorkerResultReceipt
    with session_factory(engine)() as s:
        attempt = s.get(ProviderAttempt, attempt_id)
        if not attempt:
            raise ValueError('ATTEMPT_NOT_FOUND')
        action = s.get(WorkflowAction, attempt.action_id)
        feature = s.get(Feature, action.feature_id)
        rows = list(s.scalars(select(ExecutionResultEnvelope).where(
            ExecutionResultEnvelope.attempt_id == attempt_id).order_by(
                ExecutionResultEnvelope.recorded_at, ExecutionResultEnvelope.result_sha256).limit(65)))
        evidence = []
        for row in rows[:64]:
            body = json.loads(row.body)
            if digest(body) != row.result_sha256:
                raise ValueError('RESULT_DIGEST_MISMATCH')
            evidence.append(dict(result_sha256=row.result_sha256, result=body))
        receipt = s.get(WorkerResultReceipt, attempt.report_receipt_id) if attempt.report_receipt_id else None
        if attempt.report_receipt_id and (not receipt or receipt.result_sha256 != attempt.result_digest or receipt.job_id != attempt.job_id):
            raise ValueError('RESULT_RECEIPT_MISMATCH')
        return dict(schema_version='dal.operator-execution-report/1.0', attempt_id=attempt_id,
            feature_id=feature.feature_id, feature_state=feature.state,
            completion_mode=action.completion_mode, feature_completed_by_report=False,
            attempt_state=attempt.state, report_receipt_id=attempt.report_receipt_id,
            accepted_result_sha256=attempt.result_digest if receipt else None,
            evidence=evidence, evidence_truncated=len(rows)>64)


def mount_execution_routes(app, engine, service, config):
    from personal_agent_dal.service.app import _OperatorAuth, transport_body_guard, _append_redacted_audit
    from personal_agent_dal.service.intake import intake_task, IntakeRefusal

    @app.post('/operator/tasks/register')
    def register(body: RegisterRequest, actor=Depends(_OperatorAuth(service, 'control')),
                 _=Depends(transport_body_guard)):
        if service.kill_switch:
            raise HTTPException(503, 'kill_switch_active')
        if config is None:
            raise HTTPException(503, 'EXECUTION_REGISTRATION_UNAVAILABLE')
        try:
            config.check_input(body)
            outcome = intake_task(engine, task_description=body.task_description,
                repository_id=body.repository_id, base_sha=body.base_sha,
                toolchain_ref=body.toolchain_ref, pending_only=True)
        except (ValueError, IntakeRefusal) as exc:
            raise HTTPException(409, getattr(exc, 'code', str(exc))) from exc
        intake_key = f'pending:{outcome.feature_id}'
        _append_redacted_audit(engine, event_type='operator.task.register', outcome='accepted')
        from personal_agent_dal.storage.models import Feature
        from personal_agent_dal.storage.machine_models import ExecutionGate
        with session_factory(engine)() as s:
            feature = s.get(Feature, outcome.feature_id)
            gate = s.get(ExecutionGate, outcome.feature_id)
            feature_version, gate_version = feature.version, gate.version if gate else 0
        return dict(feature_id=outcome.feature_id, feature_state=outcome.feature_state,
            feature_version=feature_version, gate_version=gate_version, job_id=None, duplicate=outcome.duplicate,
            execution_input=dict(schema='dal.execution-input/1.0', feature_id=outcome.feature_id,
                task_source=dict(kind='intake', intake_key=intake_key),
                task_description=body.task_description,
                task_description_sha256=hashlib.sha256(body.task_description.encode()).hexdigest(),
                repository_id=body.repository_id, base_sha=body.base_sha,
                branch_name=f'codex/feature-{outcome.feature_id}', toolchain_ref=body.toolchain_ref,
                toolchain_manifest_sha256=body.toolchain_manifest_sha256, artifacts=[]))

    @app.get('/operator/provider-attempts/{attempt_id}/report')
    def report(attempt_id: str, actor=Depends(_OperatorAuth(service, 'read'))):
        try:
            body = read_report(engine, attempt_id=attempt_id)
        except ValueError as exc:
            raise HTTPException(404 if str(exc)=='ATTEMPT_NOT_FOUND' else 409, str(exc)) from exc
        # Domain-separated service attestation of the retrieved stored evidence.
        # This is not a provider signature or a claim of Feature completion.
        signature = hmac.new(service.service_key,
            b'dal.operator-execution-report/1.0\0'+canonical_json(body).encode(), hashlib.sha256).hexdigest()
        return dict(report=body, report_sha256=digest(body), attestation=dict(algorithm='HMAC-SHA256', signature=signature))
