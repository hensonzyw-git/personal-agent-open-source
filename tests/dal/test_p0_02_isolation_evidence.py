"""No transport token or local Boolean establishes workspace isolation."""
import pytest
from personal_agent_dal.machine.isolation_evidence import verify_isolation_assertion


@pytest.mark.parametrize('assertion', ['terminated=true', '{"status":"workspace_isolated"}', 'probe-receipt', 'expired-lease'])
def test_unsigned_evidence_refused(assertion):
    with pytest.raises(ValueError):
        verify_isolation_assertion(assertion, keys={}, now_epoch=100)

import json
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, func
from personal_agent_core.timeutil import utc_now
from personal_agent.auth.device_keys import encode_device_public_key
from personal_agent.api.dal_client import sign_decision
from personal_agent_dal.machine.isolation_evidence import register_supervisor, record_launch_manifest, issue_challenge, import_evidence
from personal_agent_dal.machine.action_lifecycle import pause_execution
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.transport_models import WorkerEnrollment, IsolationEvidence, IsolationChallenge, SupervisorIdentity
from tests.dal.test_p0_02_review_regressions import world, create, claim


def isolation_world(engine):
    a=create(engine)
    key=ec.generate_private_key(ec.SECP256R1())
    now=int(utc_now().timestamp())
    with session_factory(engine)() as s,s.begin():
        s.add(WorkerEnrollment(worker_id='w',machine_id='mini-synthetic',capabilities='[]',created_at=utc_now()))
    register_supervisor(engine,kid='supervisor',worker_id='w',machine_id='mini-synthetic',
        public_key=encode_device_public_key(key.public_key()),boot_id='boot',supervisor_epoch=1)
    manifest=dict(schema='dal.launch-manifest/1.0',kid='supervisor',worker_id='w',machine_id='mini-synthetic',
        registration_epoch=1,boot_id='boot',supervisor_epoch=1,attempt_id=a.attempt_id,workspace_id='old',
        workspace_generation=1,isolation_policy_sha256='a'*64,issued_at=now,expires_at=now+600,
        inventory_sha256='b'*64,reservation_id='synthetic-reservation',job_id='j',job_lease_epoch=3,lease_id='l',policy_lease_epoch=4)
    record_launch_manifest(engine,assertion=sign_decision(manifest,key=key,kid='supervisor'))
    claim(engine,a)
    pause_execution(engine,feature_id='f',expected_gate_version=1)
    challenge=issue_challenge(engine,attempt_id=a.attempt_id)
    body={**challenge['binding'],'issued_at':now,'expires_at':now+600}
    return a,key,body


def test_signed_reservation_contract_only(world):
    a,key,body=isolation_world(world)
    token=sign_decision(body,key=key,kid='supervisor')
    result=import_evidence(world,assertion=token,worker_id='w')
    world.dispose()
    assert import_evidence(world,assertion=token,worker_id='w')==result
    with session_factory(world)() as s:
        assert s.get(IsolationChallenge,body['challenge_id']).consumed_at is not None
        assert s.scalar(select(func.count()).select_from(IsolationEvidence))==1


@pytest.mark.parametrize('field,value',[('status','terminated'),('worker_id','other'),('machine_id','other'),
    ('registration_epoch',2),('boot_id','other'),('fence',10),('job_id','other'),('job_lease_epoch',9),
    ('lease_id','other'),('policy_lease_epoch',9),('old_workspace_id','other'),('new_workspace_id','old'),
    ('challenge_id','other'),('launch_manifest_sha256','b'*64)])
def test_signed_but_wrong_target_rejected(world,field,value):
    a,key,body=isolation_world(world)
    body[field]=value
    with pytest.raises(ValueError):import_evidence(world,assertion=sign_decision(body,key=key,kid='supervisor'),worker_id='w')
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(IsolationEvidence))==0


def test_registration_change_invalidates_evidence(world):
    a,key,body=isolation_world(world)
    with session_factory(world)() as s,s.begin():s.get(WorkerEnrollment,'w').registration_epoch+=1
    with pytest.raises(ValueError):import_evidence(world,assertion=sign_decision(body,key=key,kid='supervisor'),worker_id='w')


def test_historical_manifest_cannot_be_backfilled(world):
    a,key,body=isolation_world(world)
    now=int(utc_now().timestamp())
    manifest=dict(schema='dal.launch-manifest/1.0',kid='supervisor',worker_id='w',machine_id='mini-synthetic',
        registration_epoch=1,boot_id='boot',supervisor_epoch=1,attempt_id=a.attempt_id,workspace_id='old',
        workspace_generation=1,isolation_policy_sha256='a'*64,issued_at=now,expires_at=now+600)
    with pytest.raises(ValueError,match='BACKFILL_FORBIDDEN'):
        record_launch_manifest(world,assertion=sign_decision(manifest,key=key,kid='supervisor'))


def test_unknown_replacement_requires_cost_acceptance_and_reserves_evidence(world):
    from tests.dal.test_p0_02_resume_authority import setup_resume, approve
    from personal_agent_dal.machine.resume_authority import resume, ResumeRequest
    from personal_agent_dal.storage.machine_models import ProviderAttempt
    a,key,body=isolation_world(world)
    import_evidence(world,assertion=sign_decision(body,key=key,kid='supervisor'),worker_id='w')
    old,p,pa_key,claims=setup_resume(world)
    with pytest.raises(ValueError,match='DUPLICATE_COST'):approve(world,pa_key,claims)
    claims['decision']='approve_once_accept_duplicate_cost'
    approved=approve(world,pa_key,claims)
    result=resume(world,feature_id='f',body=ResumeRequest(request_id='resume-unknown',approval_id=approved['approval_id']))
    with session_factory(world)() as s:
        assert s.get(IsolationEvidence,body['isolation_id']).reserved_by==result['new_attempt_id']
        prior=s.get(ProviderAttempt,a.attempt_id)
        assert prior.state=='superseded' and prior.dispatch_started_at is not None and prior.job_id=='j'
        new=s.get(ProviderAttempt,result['new_attempt_id'])
        assert new.job_id is None and new.lease_id is None


def test_expired_challenge_and_reuse_with_new_isolation_id(world):
    from datetime import timedelta
    a,key,body=isolation_world(world)
    token=sign_decision(body,key=key,kid='supervisor')
    import_evidence(world,assertion=token,worker_id='w')
    body['isolation_id']='other'
    with pytest.raises(ValueError):import_evidence(world,assertion=sign_decision(body,key=key,kid='supervisor'),worker_id='w')
    with session_factory(world)() as s,s.begin():
        s.get(IsolationChallenge,body['challenge_id']).expires_at=utc_now()-timedelta(seconds=1)
    with pytest.raises(ValueError):import_evidence(world,assertion=token,worker_id='w')


@pytest.mark.parametrize('stale', ['version', 'boot'])
def test_stale_and_fresh_evidence_coexist(world, stale):
    from personal_agent_dal.storage.machine_models import ProviderAttempt, WorkflowAction
    from personal_agent_dal.machine.isolation_evidence import current_evidence
    from personal_agent_dal.machine.workflow_selection import digest
    from personal_agent_core.manifest import canonical_json
    a,key,body=isolation_world(world)
    import_evidence(world,assertion=sign_decision(body,key=key,kid='supervisor'),worker_id='w')
    with session_factory(world)() as s,s.begin():
        if stale == 'version': s.get(ProviderAttempt,a.attempt_id).version += 1
        else:
            row=s.get(IsolationEvidence,body['isolation_id'])
            old=json.loads(row.binding); old['boot_id']='prior-boot'
            row.binding=canonical_json(old); row.binding_sha256=digest(old)
    challenge=issue_challenge(world,attempt_id=a.attempt_id)
    fresh={**challenge['binding'],'issued_at':body['issued_at'],'expires_at':body['expires_at']}
    import_evidence(world,assertion=sign_decision(fresh,key=key,kid='supervisor'),worker_id='w')
    with session_factory(world)() as s:
        assert current_evidence(s,s.get(ProviderAttempt,a.attempt_id),s.get(WorkflowAction,a.action_id)).isolation_id==fresh['isolation_id']


def test_consumption_uses_proposal_evidence_even_with_newer_proof(world):
    from tests.dal.test_p0_02_resume_authority import setup_resume, approve
    from personal_agent_dal.machine.resume_authority import resume, ResumeRequest
    from datetime import timedelta
    a,key,body=isolation_world(world)
    import_evidence(world,assertion=sign_decision(body,key=key,kid='supervisor'),worker_id='w')
    _,p,pa_key,claims=setup_resume(world)
    # Another valid proof must not displace the proof already shown to the human.
    challenge=issue_challenge(world,attempt_id=a.attempt_id)
    fresh={**challenge['binding'],'issued_at':body['issued_at'],'expires_at':body['expires_at']+1}
    import_evidence(world,assertion=sign_decision(fresh,key=key,kid='supervisor'),worker_id='w')
    claims['decision']='approve_once_accept_duplicate_cost'
    approved=approve(world,pa_key,claims)
    result=resume(world,feature_id='f',body=ResumeRequest(request_id='bound-proof',approval_id=approved['approval_id']))
    with session_factory(world)() as s:
        assert s.get(IsolationEvidence,body['isolation_id']).reserved_by==result['new_attempt_id']
        assert s.get(IsolationEvidence,fresh['isolation_id']).reserved_by is None


def test_invalid_proposal_proof_never_falls_back_to_valid_other_proof(world):
    from tests.dal.test_p0_02_resume_authority import setup_resume, approve
    a,key,body=isolation_world(world)
    import_evidence(world,assertion=sign_decision(body,key=key,kid='supervisor'),worker_id='w')
    _,p,pa_key,claims=setup_resume(world)
    challenge=issue_challenge(world,attempt_id=a.attempt_id)
    fresh={**challenge['binding'],'issued_at':body['issued_at'],'expires_at':body['expires_at']}
    import_evidence(world,assertion=sign_decision(fresh,key=key,kid='supervisor'),worker_id='w')
    with session_factory(world)() as s,s.begin():
        s.get(IsolationEvidence,body['isolation_id']).binding_sha256='0'*64
    claims['decision']='approve_once_accept_duplicate_cost'
    with pytest.raises(ValueError): approve(world,pa_key,claims)


def test_old_signed_metadata_without_inventory_is_not_isolation_proof(world):
    from personal_agent_dal.storage.transport_models import SupervisorLaunchManifest
    from personal_agent_dal.machine.workflow_selection import digest
    from personal_agent_core.manifest import canonical_json
    a,key,body=isolation_world(world)
    with session_factory(world)() as s,s.begin():
        manifest=s.get(SupervisorLaunchManifest,a.attempt_id)
        old=json.loads(manifest.body)
        old['inventory_sha256']=None
        old['reservation_id']=None
        manifest.body=canonical_json(old);manifest.sha256=digest(old)
    with pytest.raises(ValueError,match='HISTORICAL_PRELAUNCH_INVENTORY_MISSING'):
        issue_challenge(world,attempt_id=a.attempt_id)
