"""Authenticated resume and prelaunch transport; provider launch remains disabled."""
import json
from typing import Annotated
from pydantic import Field, StrictInt
from fastapi import Depends, HTTPException, Request
from personal_agent_core.timeutil import utc_now
from personal_agent.auth.device_keys import load_device_public_key
from personal_agent.api.dal_client import ProposalBridgeClaims, verify_closed_assertion
from personal_agent_dal.machine.workflow_selection import Closed, SelectionRequest, register_profile, select_workflow
from personal_agent_dal.machine.resume_authority import ResumeRequest, propose, import_decision, resume, decision_status, RevokeRequest, revoke_decision
from personal_agent_dal.machine.isolation_evidence import import_evidence, issue_challenge


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


def mount_routes(app, engine, service, config):
    from personal_agent_dal.service.app import _OperatorAuth, transport_body_guard
    if config is not None:
        for profile in config['profiles']: register_profile(engine,**profile)
    def enabled():
        if config is None: raise HTTPException(503,'DAL_RESUME_UNAVAILABLE')
    def call(fn,**kwargs):
        try: return fn(engine,**kwargs)
        except ValueError as exc: raise HTTPException(409,str(exc)) from exc
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
        if config is None:
            from sqlalchemy import select
            from personal_agent_dal.storage.engine import session_factory
            from personal_agent_dal.storage.machine_models import ResumeEpisode
            with session_factory(engine)() as session:
                if session.scalar(select(ResumeEpisode.intent_id).where(ResumeEpisode.job_id == job_id)):
                    enabled()
            return {'context': None}
        return {'context':call(prelaunch_context,job_id=job_id,worker_id=worker_id,**body.model_dump())}

    @app.post('/worker/jobs/{job_id}/prelaunch-manifest')
    def manifest(job_id: str, body: PrelaunchManifestRequest, worker_id=Depends(worker)):
        enabled()
        from personal_agent_dal.machine.resume_dispatch import acknowledge_manifest
        return call(acknowledge_manifest,job_id=job_id,worker_id=worker_id,**body.model_dump())

    @app.post('/worker/jobs/{job_id}/prelaunch-dispatch')
    def dispatch(job_id: str, body: PrelaunchDispatchRequest, worker_id=Depends(worker)):
        enabled()
        from personal_agent_dal.machine.resume_dispatch import dispatch_prelaunch
        return call(dispatch_prelaunch,job_id=job_id,worker_id=worker_id,**body.model_dump())

    @app.post('/operator/dispatch-intents/{intent_id}/episode')
    def consume_episode(intent_id: str, actor=Depends(_OperatorAuth(service,'control'))):
        enabled()
        if service.kill_switch:raise HTTPException(503,'kill_switch_active')
        from personal_agent_dal.machine.resume_dispatch import consume_intent
        return {'job_id':call(consume_intent,intent_id=intent_id)}
