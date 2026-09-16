"""Authenticated resume and prelaunch transport; provider launch remains disabled."""
import json
import logging
from typing import Annotated
from sqlalchemy import inspect, select
from pydantic import Field, StrictInt
from fastapi import Depends, HTTPException, Request
from personal_agent_core.timeutil import utc_now
from personal_agent.auth.device_keys import load_device_public_key
from personal_agent.api.dal_client import ProposalBridgeClaims, verify_closed_assertion
from personal_agent_dal.machine.workflow_selection import Closed, SelectionRequest, register_profile, select_workflow
from personal_agent_dal.machine.resume_authority import ResumeRequest, propose, import_decision, resume, decision_status, RevokeRequest, revoke_decision
from personal_agent_dal.machine.isolation_evidence import import_evidence, issue_challenge
from personal_agent_dal.storage.machine_models import DispatchIntent, ResumeEpisode


from personal_agent_dal.machine.execution_start import (
    PrepareExecutionRequest, StartExecutionRequest, prepare_execution, start_execution,
)

from personal_agent_dal.machine.execution_protocol import ExecutionResultRequest

logger = logging.getLogger(__name__)


class AssertionRequest(Closed):
    assertion: Annotated[str, Field(strict=True,min_length=1,max_length=16384)]


class PrelaunchRequest(Closed):
    job_lease_epoch: Annotated[StrictInt, Field(ge=1)]


class PrelaunchManifestRequest(PrelaunchRequest):
    assertion: Annotated[str, Field(strict=True,min_length=1,max_length=16384)]


class PrelaunchDispatchRequest(PrelaunchRequest):
    manifest_sha256: Annotated[str, Field(strict=True,pattern=r'^[0-9a-f]{64}$')]


def load_config(path):
    from personal_agent.api.dal_client import _read_bridge_file, validate_trust_ids
    body=json.loads(_read_bridge_file(path, kind='CONFIG'))
    if set(body)!={'issuer','audience','keys','profiles'} or not body['keys'] or {p['profile'] for p in body['profiles']}!={'A','B'}:
        raise ValueError('DAL_RESUME_CONFIG_INVALID')
    validate_trust_ids(body['issuer'], body['audience'], body['keys'])
    body['keys']={kid:load_device_public_key(key) for kid,key in body['keys'].items()}
    return body


def mount_routes(app, engine, service, config, execution_config=None):
    from personal_agent_dal.service.app import _OperatorAuth, transport_body_guard
    if config is not None:
        for profile in config['profiles']: register_profile(engine,**profile)
    else:
        with engine.connect() as connection:
            schema = inspect(connection)
            if schema.has_table('dispatch_intents') and schema.has_table('resume_episodes'):
                unmaterialized = select(DispatchIntent.intent_id).where(
                    ~select(ResumeEpisode.intent_id).where(
                        ResumeEpisode.intent_id == DispatchIntent.intent_id
                    ).correlate(DispatchIntent).exists()
                ).exists()
                if connection.scalar(select(unmaterialized)):
                    logger.error('DAL_RESUME_DISABLED_PENDING_INTENTS')
    from personal_agent_dal.service.execution_config import ExecutionConfig
    from personal_agent_dal.service.execution_routes import mount_execution_routes
    if execution_config is not None:
        execution_config = ExecutionConfig.model_validate(execution_config)
        for profile in execution_config.profiles:
            register_profile(engine, **profile.model_dump())
    mount_execution_routes(app, engine, service, execution_config)

    def execution_enabled(revision_id=None, inp=None):
        if execution_config is None:
            enabled()  # Backward-compatible combined configuration.
        else:
            try:
                if revision_id is not None: execution_config.check_profile(revision_id)
                if inp is not None: execution_config.check_input(inp)
            except ValueError as exc: raise HTTPException(409, str(exc)) from exc

    def job_enabled(job_id, worker_id, job_lease_epoch):
        from personal_agent_dal.storage.engine import session_factory
        from personal_agent_dal.storage.machine_models import ExecutionJobBinding, ExecutionSnapshot, ProviderAttempt, WorkflowAction
        from personal_agent_dal.machine.resume_dispatch import require_job_authority
        with session_factory(engine)() as session:
            try:
                job = require_job_authority(session, job_id=job_id,
                    worker_id=worker_id, job_lease_epoch=job_lease_epoch)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            binding = session.scalar(select(ExecutionJobBinding).where(ExecutionJobBinding.job_id == job_id))
            if not binding and job and job.execution_mode == 'legacy_non_provider':
                # Intentional configuration-independent legacy path: context=None.
                # Manifest/dispatch still refuse PRELAUNCH_NOT_APPLICABLE.
                return
            if binding and binding.origin == 'initial':
                snapshot = session.get(ExecutionSnapshot, binding.snapshot_sha256)
                attempt = session.get(ProviderAttempt, binding.attempt_id)
                action = session.get(WorkflowAction, attempt.action_id) if attempt else None
                from personal_agent_dal.machine.execution_start import ExecutionInput
                inp = ExecutionInput.model_validate_json(action.execution_input_body) if action else None
                execution_enabled(snapshot.revision_id if snapshot else '', inp)
            else:
                enabled()  # Replacement always requires original PA bridge trust.

    def enabled():
        if config is None: raise HTTPException(503,'DAL_RESUME_UNAVAILABLE')
    def call(fn,**kwargs):
        try: return fn(engine,**kwargs)
        except ValueError as exc: raise HTTPException(409,str(exc)) from exc
    @app.post('/operator/features/{feature_id}/execution-selections')
    def prepare(feature_id: str, body: PrepareExecutionRequest,
                actor=Depends(_OperatorAuth(service,'control')), _=Depends(transport_body_guard)):
        execution_enabled(body.profile_revision_id, body.execution_input)
        return call(prepare_execution, feature_id=feature_id, actor=actor, body=body,
                    kill_switch=lambda: service.kill_switch)

    @app.post('/operator/features/{feature_id}/executions')
    def start(feature_id: str, body: StartExecutionRequest,
              actor=Depends(_OperatorAuth(service,'control')), _=Depends(transport_body_guard)):
        from personal_agent_dal.storage.engine import session_factory
        from personal_agent_dal.storage.machine_models import WorkflowSelection, ExecutionSnapshot, WorkflowAction
        from personal_agent_dal.machine.execution_start import ExecutionInput
        with session_factory(engine)() as session:
            selection = session.get(WorkflowSelection, body.selection_id)
            snapshot = session.get(ExecutionSnapshot, selection.snapshot_sha256) if selection else None
            action = session.get(WorkflowAction, body.action_id)
            inp = ExecutionInput.model_validate_json(action.execution_input_body) if action and action.execution_input_body else None
            execution_enabled(snapshot.revision_id if snapshot else '', inp)
        return call(start_execution, feature_id=feature_id, actor=actor, body=body,
                    kill_switch=lambda: service.kill_switch)

    @app.post('/operator/features/{feature_id}/workflow-selection')
    def selection(feature_id: str, body: SelectionRequest, actor=Depends(_OperatorAuth(service,'control'))):
        enabled()
        return call(select_workflow,feature_id=feature_id,actor=actor,body=body)
    @app.post('/operator/features/{feature_id}/resume')
    def execute_resume(feature_id: str, body: ResumeRequest, actor=Depends(_OperatorAuth(service,'control'))):
        enabled()
        return call(resume,feature_id=feature_id,body=body,kill_switch=lambda: service.kill_switch)
    @app.post('/operator/human-decisions/{decision_id}/revoke')
    def revoke(decision_id: str, body: RevokeRequest, actor=Depends(_OperatorAuth(service,'control')),
               _=Depends(transport_body_guard)):
        enabled()
        return call(revoke_decision,decision_id=decision_id,body=body,actor=actor)
    @app.get('/operator/human-decisions/{decision_id}')
    def lookup(decision_id: str, actor=Depends(_OperatorAuth(service,'read'))):
        enabled()
        return call(decision_status,decision_id=decision_id)
    @app.post('/internal/human-decisions')
    def decision(body: AssertionRequest):
        enabled()
        return call(import_decision,assertion=body.assertion,keys=config['keys'],issuer=config['issuer'],audience=config['audience'])
    @app.post('/internal/resume-proposals')
    def proposal(body: AssertionRequest):
        enabled()
        try:
            claims=verify_closed_assertion(body.assertion,keys=config['keys'],issuer=config['issuer'],audience=config['audience'],
                now_epoch=int(utc_now().timestamp()),schema=ProposalBridgeClaims)
        except ValueError as exc: raise HTTPException(403,'BRIDGE_AUTH_INVALID') from exc
        return call(propose,feature_id=claims['feature_id'],selection_id=claims['selection_id'],request_id=claims['request_id'])
    @app.post('/operator/provider-attempts/{attempt_id}/isolation-challenge')
    def challenge(attempt_id: str, actor=Depends(_OperatorAuth(service,'control'))):
        enabled()
        return call(issue_challenge,attempt_id=attempt_id)
    @app.post('/worker/isolation-evidence')
    def evidence(body: AssertionRequest,request: Request):
        enabled()
        worker_id,_=service.auth(request)
        return call(import_evidence,assertion=body.assertion,worker_id=worker_id)

    def worker(auth=Depends(service.auth)):
        identity,_=auth
        if service.kill_switch:raise HTTPException(503,'kill_switch_active')
        return identity

    @app.post('/worker/jobs/{job_id}/prelaunch-context')
    def context(job_id: str, body: PrelaunchRequest, worker_id=Depends(worker)):
        from personal_agent_dal.machine.resume_dispatch import prelaunch_context
        job_enabled(job_id, worker_id, body.job_lease_epoch)
        try:
            result=prelaunch_context(engine,job_id=job_id,worker_id=worker_id,
                enabled=True,**body.model_dump())
        except ValueError as exc:
            status=503 if str(exc)=='DAL_RESUME_UNAVAILABLE' else 409
            raise HTTPException(status,str(exc)) from exc
        return {'context':result}

    @app.post('/worker/jobs/{job_id}/prelaunch-manifest')
    def manifest(job_id: str, body: PrelaunchManifestRequest, worker_id=Depends(worker)):
        job_enabled(job_id, worker_id, body.job_lease_epoch)
        from personal_agent_dal.machine.resume_dispatch import acknowledge_manifest
        return call(acknowledge_manifest,job_id=job_id,worker_id=worker_id,**body.model_dump())

    @app.post('/worker/jobs/{job_id}/prelaunch-dispatch')
    def dispatch(job_id: str, body: PrelaunchDispatchRequest, worker_id=Depends(worker)):
        job_enabled(job_id, worker_id, body.job_lease_epoch)
        from personal_agent_dal.machine.resume_dispatch import dispatch_prelaunch
        return call(dispatch_prelaunch,job_id=job_id,worker_id=worker_id,**body.model_dump())

    @app.post('/operator/dispatch-intents/{intent_id}/episode')
    def consume_episode(intent_id: str, actor=Depends(_OperatorAuth(service,'control'))):
        enabled()
        if service.kill_switch:raise HTTPException(503,'kill_switch_active')
        from personal_agent_dal.machine.resume_dispatch import consume_intent
        return {'job_id':call(consume_intent,intent_id=intent_id)}

    @app.get('/worker/jobs/{job_id}/execution-status')
    def execution_status_route(job_id: str, request: Request, identity=Depends(service.auth)):
        if request.query_params or getattr(request, '_body', b''):
            raise HTTPException(400, 'EXECUTION_STATUS_REQUEST_INVALID')
        from personal_agent_dal.machine.execution_results import execution_status
        return call(execution_status, job_id=job_id, worker_id=identity[0], kill_switch=lambda: service.kill_switch)

    @app.post('/worker/jobs/{job_id}/execution-result')
    def execution_result_route(job_id: str, body: ExecutionResultRequest, identity=Depends(service.auth)):
        from personal_agent_dal.machine.execution_results import submit_execution_result
        return call(submit_execution_result, job_id=job_id, worker_id=identity[0], request=body,
                    kill_switch=lambda: service.kill_switch)
