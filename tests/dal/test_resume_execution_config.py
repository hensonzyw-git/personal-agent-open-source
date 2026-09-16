"""Offline bridge trust plus explicit execution pins, using real SQLite/signatures."""
import copy
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import select

from personal_agent.api.dal_client import sign_decision
from personal_agent.auth.device_keys import encode_device_public_key
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.action_lifecycle import pause_execution
from personal_agent_dal.machine.execution_manifest import sign_execution_manifest
from personal_agent_dal.machine.isolation_evidence import register_supervisor
from personal_agent_dal.service.app import create_app
from personal_agent_dal.service.resume_routes import load_config
from personal_agent_dal.service.tokens import issue_token
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import (
    Approval, DispatchIntent, Lease, ResumeProposal,
    WorkflowProfileRevision,
)
from personal_agent_dal.storage.models import Feature
from personal_agent_dal.storage.transport_models import WorkerEnrollment
from personal_agent_dal.storage.worker_models import WorkerJob
from personal_agent_dal.worker.queue import claim_job
from tests.dal.test_execution_operator_composition import KEY, config, post, token
from tests.dal.test_operator_api import _post_json
from tests.dal.test_trusted_execution_unit1 import world, preparation, request
from tests.dal.test_versioned_role_contract import new_snapshot, config as versioned_config
from tests.dal.test_p0_02_review_regressions import world as historical_world


def trust_file(tmp_path, key, **changes):
    body = dict(schema_version='dal.resume-trust/1.0', issuer='pa-resume',
        audience='dal-resume', keys={'resume': encode_device_public_key(key.public_key())})
    body.update(changes)
    path = tmp_path/'trust.json'
    path.write_text(json.dumps(body)); path.chmod(0o600)
    return path


def composition(engine, trust, cfg):
    return TestClient(create_app(engine, service_key=KEY, enrollment_secret=b'enroll',
        resume_config=trust, execution_config=cfg, lease_ttl_seconds=720))


@pytest.fixture
def setup(world, tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    trust = load_config(trust_file(tmp_path, key))
    cfg = versioned_config(new_snapshot())
    cfg['approved_inputs'] = config(world)['approved_inputs']
    c = composition(world, trust, cfg)
    prepare = preparation(profile_revision_id=cfg['profiles'][0]['revision_id']).model_dump(by_alias=True)
    p = post(c, '/operator/features/f/execution-selections', prepare)
    assert p.status_code == 200, p.text
    initial = post(c, '/operator/features/f/executions', request(p.json()).model_dump())
    assert initial.status_code == 200, initial.text
    with session_factory(world)() as s, s.begin():
        s.get(Feature, 'f').artifact_sha256 = 'a'*64
        s.add(WorkerEnrollment(worker_id='w', machine_id='synthetic', capabilities='[]', created_at=utc_now()))
    assert pause_execution(world, feature_id='f', expected_gate_version=2).code == 'PAUSED'
    return c, trust, cfg, key, initial.json()


def select_body(cfg, **changes):
    body = dict(request_id='replacement-selection', profile_revision_id=cfg['profiles'][0]['revision_id'],
        expected_feature_version=1, expected_gate_version=3)
    body.update(changes)
    return body


def approved(c, cfg, key):
    selection = post(c, '/operator/features/f/workflow-selection', select_body(cfg))
    assert selection.status_code == 200, selection.text
    now = int(time.time())
    proposal_claims = dict(iss='pa-resume', aud='dal-resume', jti='proposal-jti', iat=now, exp=now+600,
        operation='resume-proposal', feature_id='f', selection_id=selection.json()['selection_id'], request_id='proposal')
    proposal = _post_json(c, '/internal/resume-proposals', {
        'assertion': sign_decision(proposal_claims, key=key, kid='resume')})
    assert proposal.status_code == 200, proposal.text
    claims = dict(iss='pa-resume', aud='dal-resume', jti='decision-jti', iat=now, exp=now+600,
        decision_id='decision', device_id='phone', subject_id='device:phone', key_thumbprint='a'*43,
        decision='approve_once', proposal_id=proposal.json()['proposal_id'],
        binding_sha256=proposal.json()['binding_sha256'])
    approval = _post_json(c, '/internal/human-decisions', {'assertion': sign_decision(claims, key=key, kid='resume')})
    assert approval.status_code == 200, approval.text
    return approval.json(), proposal.json()


def replacement(world, setup):
    c, trust, cfg, key, initial = setup
    approval, proposal = approved(c, cfg, key)
    resumed = post(c, '/operator/features/f/resume', dict(request_id='resume', approval_id=approval['approval_id']))
    assert resumed.status_code == 200, resumed.text
    with session_factory(world)() as s:
        intent = s.scalar(select(DispatchIntent.intent_id))
    episode = post(c, f'/operator/dispatch-intents/{intent}/episode', {})
    assert episode.status_code == 200, episode.text
    job = episode.json()['job_id']
    with session_factory(world)() as s, s.begin():
        s.get(WorkerJob, initial['job_id']).state = 'cancelled'
    assert claim_job(world, worker_id='w', lease_ttl_seconds=720) == job
    auth = issue_token(worker_id='w', capabilities=[], key=KEY, expires_at_epoch=int(time.time())+600)
    return job, auth, approval, proposal


def prelaunch(c, job, auth, path='prelaunch-context', **body):
    return _post_json(c, f'/worker/jobs/{job}/{path}', dict(job_lease_epoch=1, **body), auth)


def test_trust_only_closed_and_requires_valid_execution_config(world, tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    trust = load_config(trust_file(tmp_path, key))
    assert 'profiles' not in trust
    with pytest.raises(ValueError, match='DAL_RESUME_EXECUTION_CONFIG_REQUIRED'):
        composition(world, trust, None)
    with pytest.raises(ValueError):
        composition(world, trust, {'schema_version': 'unknown'})
    for changes in [dict(schema_version='future'), dict(profiles=[]), dict(keys={}), dict(issuer=True), dict(extra=True)]:
        with pytest.raises(ValueError):
            load_config(trust_file(tmp_path, key, **changes))
    # Legacy combined format retains its exact A+B requirement.
    body = json.loads(trust_file(tmp_path, key).read_text()); body.pop('schema_version')
    body['profiles'] = config(world)['profiles']
    path = tmp_path/'legacy.json'; path.write_text(json.dumps(body)); path.chmod(0o600)
    with pytest.raises(ValueError, match='DAL_RESUME_CONFIG_INVALID'): load_config(path)
    a = copy.deepcopy(body['profiles'][0])
    a.update(profile='A', revision_id='A-1')
    a['roles']['coder'].update(runtime='claude_code', provider='anthropic', model='claude-opus-4-6', reasoning='high')
    body['profiles'].append(a)
    path.write_text(json.dumps(body))
    legacy = load_config(path)
    composition(world, legacy, None)


def test_trust_only_real_replacement_signed_dispatch(world, setup):
    c, trust, cfg, key, _ = setup
    job, auth, _, _ = replacement(world, setup)
    response = prelaunch(c, job, auth)
    assert response.status_code == 200, response.text
    ctx = response.json()['context']
    assert ctx['execution_input'] == preparation().execution_input.model_dump(by_alias=True)
    assert ctx['snapshot']['revision_id'] == cfg['profiles'][0]['revision_id']
    assert ctx['snapshot']['roles']['reviewer']['billing'] == 'api'
    with session_factory(world)() as s:
        assert set(s.scalars(select(WorkflowProfileRevision.profile))) == {'B'}
    register_supervisor(world, kid='supervisor', worker_id='w', machine_id='synthetic',
        public_key=encode_device_public_key(key.public_key()), boot_id='boot', supervisor_epoch=1)
    # Supervisor provisioning invalidates the former epoch-less credential.
    assert prelaunch(c, job, auth).status_code == 403
    auth = issue_token(worker_id='w', machine_id='synthetic', registration_epoch=1,
                       capabilities=[], key=KEY, expires_at_epoch=int(time.time())+600)
    payload = dict(schema='dal.launch-manifest/1.1', kid='supervisor', worker_id='w', machine_id='synthetic',
        registration_epoch=1, boot_id='boot', supervisor_epoch=1, attempt_id=ctx['attempt_id'],
        workspace_id='workspace', workspace_generation=1, isolation_policy_sha256='a'*64,
        issued_at=int(time.time()), expires_at=ctx['execution_spec']['policy_expires_at'],
        inventory_sha256='b'*64, reservation_id='reservation', job_id=job, job_lease_epoch=1,
        lease_id=ctx['lease_id'], policy_lease_epoch=ctx['policy_lease_epoch'], execution_spec=ctx['execution_spec'],
        execution_spec_sha256=ctx['execution_spec_sha256'], launcher_plan_sha256='c'*64,
        source_reservation_sha256='b'*64, isolation_id=None, isolation_binding_sha256=None)
    assertion, sha = sign_execution_manifest(payload, key=key)
    ack = prelaunch(c, job, auth, 'prelaunch-manifest', assertion=assertion)
    assert ack.status_code == 200, ack.text
    dispatched = prelaunch(c, job, auth, 'prelaunch-dispatch', manifest_sha256=sha)
    assert dispatched.status_code == 200 and dispatched.json()['code'] == 'DISPATCH_GRANTED', dispatched.text


@pytest.mark.parametrize('change,expected', [('profile', 'PROFILE_UNAVAILABLE'), ('input', 'EXECUTION_INPUT_NOT_APPROVED')])
def test_changed_config_refuses_resume_without_consuming_pending_approval(world, setup, change, expected):
    c, trust, cfg, key, _ = setup
    approval, proposal = approved(c, cfg, key)
    modified = copy.deepcopy(cfg)
    modified['profiles' if change == 'profile' else 'approved_inputs'] = []
    restricted = composition(world, trust, modified)
    response = post(restricted, '/operator/features/f/resume', dict(request_id='resume', approval_id=approval['approval_id']))
    assert response.status_code == 409 and expected in response.text
    with session_factory(world)() as s:
        assert s.get(Approval, approval['approval_id']).consumed_at is None
        assert s.get(ResumeProposal, proposal['proposal_id']) is not None
        assert s.scalar(select(DispatchIntent)) is None
    lookup = restricted.get('/operator/human-decisions/decision', headers={'Authorization': 'Bearer '+token()})
    assert lookup.status_code == 200
    # Restoring the approved pins keeps the pending authority usable.
    response = post(c, '/operator/features/f/resume', dict(request_id='resume', approval_id=approval['approval_id']))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('change,expected', [('profile', 'PROFILE_UNAVAILABLE'), ('input', 'EXECUTION_INPUT_NOT_APPROVED')])
def test_replacement_rechecks_config_at_every_prelaunch_boundary(world, setup, change, expected):
    c, trust, cfg, _, _ = setup
    job, auth, _, _ = replacement(world, setup)
    assert prelaunch(c, job, auth).status_code == 200
    modified = copy.deepcopy(cfg)
    modified['profiles' if change == 'profile' else 'approved_inputs'] = []
    restricted = composition(world, trust, modified)
    for route, body in [('prelaunch-context', {}), ('prelaunch-manifest', {'assertion': 'invalid'}),
                        ('prelaunch-dispatch', {'manifest_sha256': 'a'*64})]:
        result = prelaunch(restricted, job, auth, route, **body)
        assert result.status_code == 409 and expected in result.text, result.text
    # Independent committed lease invalidation must be observed on the same app.
    with session_factory(world)() as s, s.begin():
        s.get(WorkerJob, job).lease_epoch += 1
    stale = prelaunch(restricted, job, auth)
    assert stale.status_code == 409 and 'JOB_LEASE_STALE' in stale.text


def test_selection_refuses_stored_old_revision_unapproved_or_missing_input(world, setup):
    c, trust, cfg, _, _ = setup
    from personal_agent_dal.machine.workflow_selection import register_profile
    a = copy.deepcopy(config(world)['profiles'][0])
    a.update(profile='A', revision_id='A-1')
    a['roles']['coder'].update(runtime='claude_code', provider='synthetic', model='synthetic-coder', billing='api')
    register_profile(world, **a)
    for revision in ['A-1', 'B-1']:
        result = post(c, '/operator/features/f/workflow-selection', select_body(cfg, profile_revision_id=revision))
        assert result.status_code == 409 and 'PROFILE_UNAVAILABLE' in result.text
    modified = copy.deepcopy(cfg); modified['approved_inputs'] = []
    result = post(composition(world, trust, modified), '/operator/features/f/workflow-selection', select_body(cfg))
    assert result.status_code == 409 and 'EXECUTION_INPUT_NOT_APPROVED' in result.text


def test_historical_action_without_execution_input_is_readable_but_not_executable(historical_world, tmp_path):
    from tests.dal.test_p0_02_resume_authority import setup_resume, approve
    engine = historical_world
    _, proposal, key, claims = setup_resume(engine)
    approval = approve(engine, key, claims)
    trust = load_config(trust_file(tmp_path, key))
    cfg = config(engine)
    c = composition(engine, trust, cfg)
    result = post(c, '/operator/features/f/workflow-selection', select_body(cfg, expected_gate_version=2))
    assert result.status_code == 409 and 'EXECUTION_INPUT_REQUIRED' in result.text
    result = post(c, '/operator/features/f/resume', dict(request_id='resume', approval_id=approval['approval_id']))
    assert result.status_code == 409 and 'EXECUTION_INPUT_REQUIRED' in result.text
    assert c.get('/operator/human-decisions/decision', headers={'Authorization': 'Bearer '+token()}).status_code == 200
    with session_factory(engine)() as s:
        assert s.get(ResumeProposal, proposal['proposal_id']) is not None
        assert s.get(Approval, approval['approval_id']).consumed_at is None


@pytest.mark.parametrize('kind', ['policy', 'approval'])
def test_replacement_revocation_remains_authoritative(world, setup, kind):
    c, trust, cfg, _, _ = setup
    job, auth, _, _ = replacement(world, setup)
    response = prelaunch(c, job, auth)
    assert response.status_code == 200
    if kind == 'policy':
        with session_factory(world)() as s, s.begin():
            s.get(Lease, response.json()['context']['lease_id']).revoked_at = utc_now()
    else:
        result = post(c, '/operator/human-decisions/decision/revoke', {'request_id': 'revoke'})
        assert result.status_code == 200, result.text
    for route, body in [('prelaunch-context', {}), ('prelaunch-manifest', {'assertion': 'invalid'}),
                        ('prelaunch-dispatch', {'manifest_sha256': 'a'*64})]:
        result = prelaunch(c, job, auth, route, **body)
        assert result.status_code == 409, result.text


def test_explicit_legacy_non_provider_path_stays_configuration_independent(historical_world, tmp_path):
    from sqlalchemy import text
    from tests.dal.test_p0_02_resume_authority import setup_resume
    engine = historical_world
    setup_resume(engine)
    key = ec.generate_private_key(ec.SECP256R1())
    trust = load_config(trust_file(tmp_path, key))
    cfg = config(engine); cfg['profiles'] = []; cfg['approved_inputs'] = []
    c = composition(engine, trust, cfg)
    with engine.begin() as conn:
        conn.execute(text("UPDATE worker_jobs SET execution_mode='legacy_non_provider', lease_epoch=1 WHERE job_id='j'"))
    with session_factory(engine)() as s, s.begin():
        s.add(WorkerEnrollment(worker_id='w', machine_id='synthetic', capabilities='[]', created_at=utc_now()))
    auth = issue_token(worker_id='w', capabilities=[], key=KEY, expires_at_epoch=int(time.time())+600)
    response = prelaunch(c, 'j', auth)
    assert response.status_code == 200 and response.json() == {'context': None}
    for route, body in [('prelaunch-manifest', {'assertion': 'invalid'}),
                        ('prelaunch-dispatch', {'manifest_sha256': 'a'*64})]:
        result = prelaunch(c, 'j', auth, route, **body)
        assert result.status_code == 409 and 'PRELAUNCH_NOT_APPLICABLE' in result.text


@pytest.mark.parametrize('revision', ['A-1', 'B-1'])
def test_old_stored_initial_job_cannot_launch_under_new_b_only_config(world, tmp_path, revision):
    from personal_agent_dal.machine.workflow_selection import register_profile
    from personal_agent_dal.machine.execution_start import prepare_execution
    from tests.dal.test_trusted_execution_unit1 import start
    if revision == 'A-1':
        a = copy.deepcopy(config(world)['profiles'][0])
        a.update(profile='A', revision_id='A-1')
        a['roles']['coder'].update(runtime='claude_code', provider='synthetic', model='synthetic-coder', billing='api')
        register_profile(world, **a)
    prepared = prepare_execution(world, feature_id='f', actor='operator', body=preparation(profile_revision_id=revision))
    initial = start(world, prepared)
    with session_factory(world)() as s, s.begin():
        s.add(WorkerEnrollment(worker_id='w', machine_id='synthetic', capabilities='[]', created_at=utc_now()))
    assert claim_job(world, worker_id='w', lease_ttl_seconds=720) == initial['job_id']
    key = ec.generate_private_key(ec.SECP256R1())
    trust = load_config(trust_file(tmp_path, key))
    cfg = versioned_config(new_snapshot()); cfg['approved_inputs'] = config(world)['approved_inputs']
    c = composition(world, trust, cfg)
    auth = issue_token(worker_id='w', capabilities=[], key=KEY, expires_at_epoch=int(time.time())+600)
    for route, body in [('prelaunch-context', {}), ('prelaunch-manifest', {'assertion': 'invalid'}),
                        ('prelaunch-dispatch', {'manifest_sha256': 'a'*64})]:
        result = prelaunch(c, initial['job_id'], auth, route, **body)
        assert result.status_code == 409 and 'PROFILE_UNAVAILABLE' in result.text, result.text
