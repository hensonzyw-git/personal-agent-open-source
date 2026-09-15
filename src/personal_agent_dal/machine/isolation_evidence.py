"""Supervisor assertions are evidence, never a substitute for physical acceptance."""
import json
from typing import Literal
from pydantic import StrictInt, Field
from sqlalchemy import select
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now
from personal_agent.auth.device_keys import load_device_public_key
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest, Version, digest
from personal_agent_dal.machine.action_lifecycle import _transaction
from personal_agent_dal.storage.transport_models import SupervisorIdentity, IsolationChallenge, IsolationEvidence, WorkerEnrollment


class IsolationAssertion(Closed):
    schema_version: Literal['dal.workspace-isolation/1.0'] = Field(alias='schema')
    kid: Id
    worker_id: Id
    machine_id: Id
    registration_epoch: Version
    boot_id: Id
    supervisor_epoch: Version
    feature_id: Id
    action_id: Id
    attempt_id: Id
    attempt_version: Version
    owner_id: Id
    fence: StrictInt
    job_id: Id
    job_lease_epoch: StrictInt
    lease_id: Id
    policy_lease_epoch: StrictInt
    isolation_id: Id
    old_workspace_id: Id
    new_workspace_id: Id
    workspace_generation: Version
    launch_manifest_sha256: Digest
    isolation_policy_sha256: Digest
    challenge_id: Id
    issued_at: StrictInt
    expires_at: StrictInt
    status: Literal['workspace_isolated']


def verify_isolation_assertion(assertion, *, keys, now_epoch):
    from personal_agent.api.dal_client import verify_closed_assertion
    from personal_agent_dal.machine.execution_manifest import IsolationV11
    try:
        body = verify_closed_assertion(assertion, keys=keys, now_epoch=now_epoch, schema=IsolationV11)
    except ValueError:
        body = verify_closed_assertion(assertion, keys=keys, now_epoch=now_epoch, schema=IsolationAssertion)
    import jwt
    if body['kid'] != jwt.get_unverified_header(assertion)['kid']:
        raise ValueError('ISOLATION_KEY_MISMATCH')
    if body['new_workspace_id'] == body['old_workspace_id'] or min(body['fence'], body['job_lease_epoch'], body['policy_lease_epoch']) < 0:
        raise ValueError('ISOLATION_TARGET_INVALID')
    return body


def _current(s, body):
    identity = s.get(SupervisorIdentity, body['kid'])
    enrollment = s.get(WorkerEnrollment, body['worker_id'])
    if not identity or identity.revoked_at or not enrollment or enrollment.revoked_at:
        raise ValueError('SUPERVISOR_UNAVAILABLE')
    if enrollment.registration_epoch != body['registration_epoch'] or enrollment.machine_id != body['machine_id'] or any(getattr(identity,k) != body[k] for k in
        ('worker_id','machine_id','registration_epoch','boot_id','supervisor_epoch')):
        raise ValueError('SUPERVISOR_STALE')


def issue_challenge(engine, *, attempt_id):
    from datetime import timedelta
    from personal_agent_dal.storage.transport_models import SupervisorLaunchManifest
    from personal_agent_dal.storage.machine_models import ProviderAttempt, WorkflowAction, ExecutionGate
    def work(s):
        old = s.get(ProviderAttempt, attempt_id)
        manifest = s.get(SupervisorLaunchManifest, attempt_id)
        if not old or not manifest:
            raise ValueError('HISTORICAL_LAUNCH_MANIFEST_MISSING')
        a = s.get(WorkflowAction, old.action_id)
        gate = s.get(ExecutionGate, a.feature_id)
        if not gate or gate.mode != 'paused' or a.active_attempt_id != old.attempt_id or old.dispatch_started_at is None:
            raise ValueError('ISOLATION_TARGET_INVALID')
        source = json.loads(manifest.body)
        if not source.get('inventory_sha256') or not source.get('reservation_id'):
            raise ValueError('HISTORICAL_PRELAUNCH_INVENTORY_MISSING')
        if manifest.recorded_at > old.dispatch_started_at or source['worker_id'] != old.owner_id:
            raise ValueError('MANIFEST_BINDING_INVALID')
        _current(s, source)
        if digest(source) != manifest.sha256:
            raise ValueError('MANIFEST_INVALID')
        challenge_id, isolation_id = new_id(), new_id()
        expires = utc_now()+timedelta(minutes=15)
        body = {k:source[k] for k in ('kid','worker_id','machine_id','registration_epoch','boot_id','supervisor_epoch')}
        body.update(feature_id=a.feature_id,action_id=a.action_id,attempt_id=old.attempt_id,
            attempt_version=old.version,owner_id=old.owner_id,fence=old.fence,job_id=old.job_id,
            job_lease_epoch=old.job_lease_epoch,lease_id=old.lease_id,policy_lease_epoch=old.policy_lease_epoch,
            isolation_id=isolation_id,old_workspace_id=source['workspace_id'],new_workspace_id=new_id(),
            workspace_generation=source['workspace_generation']+1,launch_manifest_sha256=manifest.sha256,
            isolation_policy_sha256=source['isolation_policy_sha256'],challenge_id=challenge_id,
            status='workspace_isolated',schema='dal.workspace-isolation/1.0')
        if source['schema'] == 'dal.launch-manifest/1.1':
            body.update(schema='dal.workspace-isolation/1.1', status='workspace_ready',
                        runtime_policy_revision='trusted-single-user/1.0')
        s.add(IsolationChallenge(challenge_id=challenge_id,body=canonical_json(body),expires_at=expires,consumed_at=None))
        return dict(challenge_id=challenge_id,binding=body,expires_at=expires.isoformat())
    return _transaction(engine,work)


def import_evidence(engine, *, assertion, worker_id):
    from datetime import datetime, timezone
    def work(s):
        keys = {row.kid: load_device_public_key(row.public_key) for row in s.scalars(select(SupervisorIdentity).where(SupervisorIdentity.revoked_at.is_(None)))}
        body = verify_isolation_assertion(assertion, keys=keys, now_epoch=int(utc_now().timestamp()))
        if body['worker_id'] != worker_id: raise ValueError('ISOLATION_WORKER_MISMATCH')
        _current(s,body)
        challenge = s.get(IsolationChallenge,body['challenge_id'])
        if not challenge or challenge.expires_at <= utc_now(): raise ValueError('CHALLENGE_INVALID')
        expected = json.loads(challenge.body)
        if body['expires_at'] > int(challenge.expires_at.timestamp()): raise ValueError('CHALLENGE_EXPIRY_MISMATCH')
        if any(body.get(k) != v for k,v in expected.items()): raise ValueError('CHALLENGE_TARGET_MISMATCH')
        existing = s.get(IsolationEvidence,body['isolation_id'])
        if existing:
            if existing.binding_sha256 != digest(body): raise ValueError('EVIDENCE_CONFLICT')
            return dict(isolation_id=existing.isolation_id,status=body['status'],expires_at=existing.expires_at.isoformat())
        if challenge.consumed_at: raise ValueError('CHALLENGE_CONSUMED')
        challenge.consumed_at = utc_now()
        row = IsolationEvidence(isolation_id=body['isolation_id'],challenge_id=body['challenge_id'],
            binding=canonical_json(body),binding_sha256=digest(body),
            expires_at=min(challenge.expires_at,datetime.fromtimestamp(body['expires_at'],timezone.utc)),reserved_by=None)
        s.add(row)
        return dict(isolation_id=row.isolation_id,status=body['status'],expires_at=row.expires_at.isoformat())
    return _transaction(engine,work)


def current_evidence(s, old, action, *, isolation_id=None):
    query = select(IsolationEvidence).where(
        IsolationEvidence.reserved_by.is_(None), IsolationEvidence.expires_at > utc_now())
    if isolation_id is not None:
        query = query.where(IsolationEvidence.isolation_id == isolation_id)
    # Proposal selection is stable; import/consume validate exactly its named row.
    query = query.order_by(IsolationEvidence.expires_at.desc(), IsolationEvidence.isolation_id.asc())
    for row in s.scalars(query):
        body = json.loads(row.binding)
        if body['attempt_id'] != old.attempt_id:
            continue
        if digest(body) != row.binding_sha256:
            raise ValueError('ISOLATION_BINDING_INVALID')
        expected = dict(feature_id=action.feature_id,action_id=action.action_id,attempt_id=old.attempt_id,
            attempt_version=old.version,owner_id=old.owner_id,fence=old.fence,job_id=old.job_id,
            job_lease_epoch=old.job_lease_epoch,lease_id=old.lease_id,policy_lease_epoch=old.policy_lease_epoch)
        try:
            _current(s,body)
            if any(body[k] != v for k,v in expected.items()):
                raise ValueError('ISOLATION_BINDING_STALE')
        except ValueError:
            if isolation_id is not None:
                raise
            continue  # Old evidence cannot veto another current, valid proof.
        return row
    raise ValueError('HISTORICAL_LAUNCH_MANIFEST_MISSING')


class LaunchManifestAssertion(Closed):
    schema_version: Literal['dal.launch-manifest/1.0'] = Field(alias='schema')
    kid: Id
    worker_id: Id
    machine_id: Id
    registration_epoch: Version
    boot_id: Id
    supervisor_epoch: Version
    attempt_id: Id
    workspace_id: Id
    workspace_generation: Version
    isolation_policy_sha256: Digest
    issued_at: StrictInt
    expires_at: StrictInt
    inventory_sha256: Digest | None = None
    reservation_id: Id | None = None
    job_id: Id | None = None
    job_lease_epoch: StrictInt | None = None
    lease_id: Id | None = None
    policy_lease_epoch: StrictInt | None = None


def register_supervisor(engine, *, kid, worker_id, machine_id, public_key, boot_id, supervisor_epoch):
    """Trusted provisioning entrypoint, never exposed to an enrolled Worker.

    Calling this requires provisioning outside this unit. Registering a public
    key does not prove its private key is inaccessible to child processes.
    """
    load_device_public_key(public_key)
    def work(s):
        enrollment=s.get(WorkerEnrollment,worker_id)
        if not enrollment or enrollment.revoked_at or enrollment.machine_id != machine_id:
            raise ValueError('WORKER_REGISTRATION_INVALID')
        if s.get(SupervisorIdentity,kid): raise ValueError('SUPERVISOR_KID_IMMUTABLE')
        enrollment.registration_epoch=(enrollment.registration_epoch or 0)+1
        s.add(SupervisorIdentity(kid=kid,worker_id=worker_id,machine_id=machine_id,
            registration_epoch=enrollment.registration_epoch,boot_id=boot_id,supervisor_epoch=supervisor_epoch,
            public_key=public_key,revoked_at=None))
        return enrollment.registration_epoch
    return _transaction(engine,work)


def record_launch_manifest(engine, *, assertion, worker_id=None, transaction_session=None):
    """Persist immutable signed metadata; new episodes additionally bind inventory and leases."""
    from personal_agent.api.dal_client import verify_closed_assertion
    from personal_agent_dal.storage.transport_models import SupervisorLaunchManifest
    from personal_agent_dal.storage.machine_models import ProviderAttempt
    def work(s):
        keys={r.kid:load_device_public_key(r.public_key) for r in s.scalars(select(SupervisorIdentity))}
        from personal_agent_dal.machine.execution_manifest import LaunchManifestV11
        try:
            body=verify_closed_assertion(assertion,keys=keys,now_epoch=int(utc_now().timestamp()),schema=LaunchManifestV11)
        except ValueError:
            body=verify_closed_assertion(assertion,keys=keys,now_epoch=int(utc_now().timestamp()),schema=LaunchManifestAssertion)
        import jwt
        if body['kid'] != jwt.get_unverified_header(assertion)['kid']: raise ValueError('MANIFEST_KEY_MISMATCH')
        _current(s,body)
        if worker_id is not None and body['worker_id'] != worker_id:
            raise ValueError('MANIFEST_WORKER_MISMATCH')
        old=s.get(ProviderAttempt,body['attempt_id'])
        if not old or old.dispatch_started_at is not None or old.state!='prepared':
            raise ValueError('HISTORICAL_MANIFEST_BACKFILL_FORBIDDEN')
        prior=s.get(SupervisorLaunchManifest,old.attempt_id)
        if prior:
            if prior.sha256!=digest(body):raise ValueError('MANIFEST_IMMUTABLE')
            return prior.sha256
        s.add(SupervisorLaunchManifest(attempt_id=old.attempt_id,body=canonical_json(body),
            sha256=digest(body),recorded_at=utc_now()))
        return digest(body)
    return work(transaction_session) if transaction_session is not None else _transaction(engine,work)
