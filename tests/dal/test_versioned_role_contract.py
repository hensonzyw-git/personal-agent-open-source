"""Synthetic contract evidence only: no CLI, credentials or provider calls."""
import copy
import json

import pytest

from personal_agent_core.manifest import canonical_json
from personal_agent_dal.machine.execution_protocol import Snapshot
from personal_agent_dal.machine.execution_start import prepare_execution
from personal_agent_dal.machine.workflow_selection import (
    ROLE_CONTRACT_V2, ProfileBody, SelectionRequest, digest, register_profile,
    select_workflow,
)
from personal_agent_dal.service.execution_config import ExecutionConfig
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import (
    ExecutionSnapshot, WorkflowProfileRevision, WorkflowAction, ProviderAttempt,
)
from personal_agent_dal.worker.runtime_mapping import resolve_snapshot
from tests.dal.test_runtime_mapping import snapshot, pins
from tests.dal.test_trusted_execution_unit1 import world, preparation, start


def new_snapshot(billing='api'):
    body = snapshot()
    body.update(contract_version=ROLE_CONTRACT_V2, revision_id='B-synthetic-v2', revision=2)
    body['roles']['planner']['reasoning'] = 'high'
    body['roles']['coder'].update(model='gpt-6-astra', reasoning='low')
    body['roles']['reviewer'].update(runtime='claude_code', provider='changhe',
        model='changhe/ch-g/kimi-k3', reasoning='high', billing=billing)
    return body


def profile(body):
    return {k: v for k, v in body.items() if k not in ('revision_id', 'input_sha256')}


def config(body, schema='dal.execution-profiles/1.1'):
    return dict(schema_version=schema, approved_inputs=[],
        profiles=[{k: v for k, v in body.items() if k not in ('input_sha256', 'fallback')}])


def register(engine, body):
    register_profile(engine, **{k: v for k, v in body.items()
                               if k not in ('input_sha256', 'fallback')})


def test_exact_new_contract_and_explicit_billing_roundtrip():
    body = new_snapshot()
    assert Snapshot.model_validate(body).model_dump() == body
    assert ProfileBody.model_validate(profile(body)).model_dump() == profile(body)
    cfg = ExecutionConfig.model_validate(config(body))
    assert cfg.model_dump() == config(body)
    assert json.loads(cfg.model_dump_json()) == config(body)
    mapped = resolve_snapshot(body, digest(body), pins(body))
    assert mapped['reviewer'].configuration.billing == 'api'
    assert mapped['reviewer'].configuration.reasoning == 'high'
    assert mapped['planner'].configuration.billing == 'subscription'
    assert mapped['coder'].configuration.billing == 'subscription'
    assert mapped['coder'].configuration.reasoning == 'low'


@pytest.mark.parametrize('mutation', [
    'no_version', 'unknown_version', 'null_version', 'legacy_with_version',
    'a_with_version', 'old_coder', 'old_planner', 'old_reviewer',
    'anthropic', 'wrong_model', 'wrong_effort', 'missing_billing', 'unknown_billing',
    'planner_api', 'coder_api', 'reviewer_subscription',
    'self_review', 'permission', 'placement', 'missing_role',
    'extra_role', 'extra_field',
])
def test_same_rejection_at_registration_config_and_worker(world, mutation):
    body = new_snapshot()
    if mutation == 'no_version': body.pop('contract_version')
    elif mutation == 'unknown_version': body['contract_version'] = 'dal.role-contract/999.0'
    elif mutation == 'null_version': body['contract_version'] = None
    elif mutation == 'legacy_with_version': body = dict(snapshot(), contract_version=ROLE_CONTRACT_V2)
    elif mutation == 'a_with_version': body['profile'] = 'A'
    elif mutation.startswith('old_'):
        role = mutation.removeprefix('old_')
        body['roles'][role] = snapshot()['roles'][role]
    elif mutation == 'anthropic': body['roles']['reviewer']['provider'] = 'anthropic'
    elif mutation == 'wrong_model': body['roles']['reviewer']['model'] = 'kimi-k3'
    elif mutation == 'wrong_effort': body['roles']['reviewer']['reasoning'] = 'medium'
    elif mutation == 'missing_billing': body['roles']['reviewer'].pop('billing')
    elif mutation == 'unknown_billing': body['roles']['reviewer']['billing'] = 'gateway'
    elif mutation in ('planner_api', 'coder_api'):
        body['roles'][mutation.removesuffix('_api')]['billing'] = 'api'
    elif mutation == 'reviewer_subscription': body['roles']['reviewer']['billing'] = 'subscription'
    elif mutation == 'self_review': body['roles']['reviewer']['model'] = 'gpt-6-astra'
    elif mutation == 'permission': body['roles']['reviewer']['permission'] = 'workspace_write'
    elif mutation == 'placement': body['roles']['coder']['placement'] = 'ecs'
    elif mutation == 'missing_role': body['roles'].pop('reviewer')
    elif mutation == 'extra_role': body['roles']['fallback'] = body['roles']['coder']
    elif mutation == 'extra_field': body['automatic_fallback'] = True
    with pytest.raises(ValueError): Snapshot.model_validate(body)
    with pytest.raises(ValueError): resolve_snapshot(body, digest(body), pins(body))
    with pytest.raises(ValueError): ExecutionConfig.model_validate(config(body))
    with pytest.raises(ValueError): register(world, body)


@pytest.mark.parametrize('body', [snapshot(), new_snapshot()])
def test_no_fallback_duplicate_pin_or_digest_reinterpretation(body):
    with pytest.raises(ValueError):
        resolve_snapshot(body, digest(body), pins(body) + [pins(body)[0]])
    for fallback in ('A', 'B', {}, False):
        changed = dict(body, fallback=fallback)
        with pytest.raises(ValueError): Snapshot.model_validate(changed)
        with pytest.raises(ValueError): resolve_snapshot(changed, digest(changed), pins(body))
    changed = copy.deepcopy(body)
    changed['roles']['planner']['reasoning'] = 'low'
    with pytest.raises(ValueError, match='SNAPSHOT_DIGEST_MISMATCH'):
        resolve_snapshot(changed, digest(body), pins(body))


def test_legacy_reader_and_config_cannot_accept_new_contract():
    body = new_snapshot()
    # Historical Worker reader required this exact closed set before mapping.
    legacy_fields = {'revision_id', 'profile', 'revision', 'input_sha256', 'roles', 'fallback'}
    assert set(body) != legacy_fields
    with pytest.raises(ValueError, match='PROFILE_CONFIG_VERSION_MISMATCH'):
        ExecutionConfig.model_validate(config(body, 'dal.execution-profiles/1.0'))
    with pytest.raises(ValueError):
        ExecutionConfig.model_validate(config(body, 'dal.execution-profiles/999.0'))


@pytest.mark.parametrize('profile_name', ['A', 'B'])
def test_legacy_shapes_remain_unversioned(profile_name):
    body = snapshot()
    if profile_name == 'A':
        body['profile'] = 'A'
        body['roles']['coder'].update(runtime='claude_code', provider='synthetic',
            model='synthetic-coder', reasoning='none', billing='api')
    encoded = canonical_json(body)
    assert canonical_json(Snapshot.model_validate(body).model_dump()) == encoded
    assert digest(Snapshot.model_validate(body).model_dump()) == digest(body)
    cfg = config(body, 'dal.execution-profiles/1.0')
    assert ExecutionConfig.model_validate(cfg).model_dump() == cfg
    assert resolve_snapshot(body, digest(body), pins(body))['coder'].configuration.model == body['roles']['coder']['model']


def test_frozen_legacy_snapshot_and_profile_digests(world):
    # Golden hashes of the pre-versioning fixture's exact historical wire shape.
    body = snapshot()
    assert digest(Snapshot.model_validate(body).model_dump()) == (
        '67735e2b1e5dc898788b7806d31733881fa61d420215aa876f3f7806c81f6b8d')
    assert digest(ProfileBody.model_validate(profile(body)).model_dump()) == (
        'a5af2ac9db5e31f0ddbdfd3ff175df5ee32145a5d21a457d73e4f8e53893d190')
    with session_factory(world)() as s:
        stored = s.get(WorkflowProfileRevision, 'B-1')
        assert stored.body == canonical_json(profile(body))
        assert stored.sha256 == 'a5af2ac9db5e31f0ddbdfd3ff175df5ee32145a5d21a457d73e4f8e53893d190'


def test_initial_and_replacement_selection_keep_old_bytes_and_hash(world):
    old = prepare_execution(world, feature_id='f', actor='operator', body=preparation())
    with session_factory(world)() as s:
        old_profile = s.get(WorkflowProfileRevision, 'B-1')
        old_profile_bytes, old_profile_sha = old_profile.body, old_profile.sha256
        old_bytes = s.get(ExecutionSnapshot, old['snapshot_sha256']).body
    assert 'contract_version' not in json.loads(old_bytes)
    register(world, new_snapshot())
    # A new initial action gets the new version, without upgrading the old one.
    initial = prepare_execution(world, feature_id='f', actor='operator', body=preparation(
        request_id='new-initial', action_key='new-task', profile_revision_id='B-synthetic-v2',
        expected_gate_version=1))
    assert initial['profile']['contract_version'] == ROLE_CONTRACT_V2
    assert initial['snapshot_sha256'] != old['snapshot_sha256']
    started = start(world, old)
    from personal_agent_dal.machine.action_lifecycle import pause_execution
    pause_execution(world, feature_id='f', expected_gate_version=2)
    selection = select_workflow(world, feature_id='f', actor='operator', body=SelectionRequest(
        request_id='replacement', profile_revision_id='B-synthetic-v2',
        expected_feature_version=1, expected_gate_version=3))
    with session_factory(world)() as s:
        replacement = s.get(ExecutionSnapshot, selection['snapshot_sha256'])
        assert json.loads(replacement.body)['contract_version'] == ROLE_CONTRACT_V2
        assert replacement.sha256 == initial['snapshot_sha256']
        assert s.get(ExecutionSnapshot, old['snapshot_sha256']).body == old_bytes
        assert s.get(WorkflowProfileRevision, 'B-1').body == old_profile_bytes
        assert s.get(WorkflowProfileRevision, 'B-1').sha256 == old_profile_sha
        assert digest(json.loads(old_bytes)) == old['snapshot_sha256']
        # Selection grants no replacement authority and never mutates old attempts.
        assert s.get(WorkflowAction, old['action_id']).execution_snapshot_sha256 == old['snapshot_sha256']
        assert s.get(ProviderAttempt, started['attempt_id']).action_id == old['action_id']
    with pytest.raises(ValueError, match='IMMUTABLE_REVISION'):
        register(world, dict(new_snapshot(), revision_id='B-1', revision=1))


def test_new_reviewer_remains_unready_without_real_route():
    from personal_agent_dal.worker.role_adapter import validate_auth_route
    from personal_agent_dal.worker.supervisor import SupervisorRefusal
    body = new_snapshot()
    role = resolve_snapshot(body, digest(body), pins(body))['reviewer'].configuration
    with pytest.raises(SupervisorRefusal, match='AUTH_ROUTE_UNSUPPORTED'):
        validate_auth_route(role, {'roles': {'reviewer': {
            'mode': 'claude_login', 'home': '/synthetic/not-read', 'environment': {}}}}, 'reviewer')


@pytest.mark.parametrize('versioned', [False, True])
@pytest.mark.parametrize('execution_role', ['planner', 'coder', 'reviewer'])
def test_initial_context_preserves_contract_and_bound_role(world, versioned, execution_role):
    from personal_agent_core.timeutil import utc_now
    from personal_agent_dal.machine.resume_dispatch import prelaunch_context
    from personal_agent_dal.machine.execution_protocol import validate_execution_context
    from personal_agent_dal.storage.transport_models import WorkerEnrollment
    from personal_agent_dal.worker.queue import claim_job
    revision_id = 'B-1'
    if versioned:
        register(world, new_snapshot())
        revision_id = 'B-synthetic-v2'
    p = prepare_execution(world, feature_id='f', actor='operator', body=preparation(
        profile_revision_id=revision_id, execution_role=execution_role))
    started = start(world, p)
    with session_factory(world)() as s, s.begin():
        s.add(WorkerEnrollment(worker_id='w', machine_id='synthetic', capabilities='[]', created_at=utc_now()))
    assert claim_job(world, worker_id='w', lease_ttl_seconds=720) == started['job_id']
    context = prelaunch_context(world, job_id=started['job_id'], worker_id='w', job_lease_epoch=1)
    assert ('contract_version' in context['snapshot']) is versioned
    assert context['snapshot'] == p['profile']
    assert context['execution_spec']['role_config'] == p['profile']['roles'][execution_role]
    assert context['snapshot_sha256'] == p['snapshot_sha256']
    assert validate_execution_context(context) == context
    tampered = copy.deepcopy(context)
    tampered['snapshot']['contract_version'] = 'future'
    tampered['snapshot_sha256'] = digest(tampered['snapshot'])
    tampered['execution_spec']['snapshot_sha256'] = tampered['snapshot_sha256']
    tampered['execution_spec_sha256'] = digest(tampered['execution_spec'])
    with pytest.raises(ValueError): validate_execution_context(tampered)


def test_replacement_version_requires_new_approval_and_new_attempt(world):
    from cryptography.hazmat.primitives.asymmetric import ec
    from sqlalchemy import select
    from personal_agent_core.timeutil import utc_now
    from personal_agent_dal.machine.action_lifecycle import pause_execution
    from personal_agent_dal.machine.resume_authority import propose, resume, ResumeRequest
    from personal_agent_dal.machine.resume_dispatch import consume_intent, prelaunch_context
    from personal_agent_dal.storage.machine_models import DispatchIntent, ExecutionJobBinding
    from personal_agent_dal.storage.models import Feature
    from personal_agent_dal.storage.transport_models import WorkerEnrollment
    from personal_agent_dal.storage.worker_models import WorkerJob
    from personal_agent_dal.worker.queue import claim_job
    from tests.dal.test_p0_02_resume_authority import approve

    p = prepare_execution(world, feature_id='f', actor='operator', body=preparation())
    first = start(world, p)
    register(world, new_snapshot())
    with session_factory(world)() as s, s.begin():
        s.get(Feature, 'f').artifact_sha256 = 'a' * 64
        s.add(WorkerEnrollment(worker_id='w', machine_id='synthetic', capabilities='[]', created_at=utc_now()))
    assert pause_execution(world, feature_id='f', expected_gate_version=2).code == 'PAUSED'
    selected = select_workflow(world, feature_id='f', actor='operator', body=SelectionRequest(
        request_id='new-selection', profile_revision_id='B-synthetic-v2',
        expected_feature_version=1, expected_gate_version=3))
    with pytest.raises(ValueError):
        resume(world, feature_id='f', body=ResumeRequest(request_id='no-approval', approval_id='absent'))
    proposal = propose(world, feature_id='f', selection_id=selected['selection_id'], request_id='proposal')
    key = ec.generate_private_key(ec.SECP256R1())
    now = int(utc_now().timestamp())
    approval = approve(world, key, dict(iss='pa-resume', aud='dal-resume', jti='jti', iat=now, exp=now+600,
        decision_id='decision', device_id='phone', subject_id='device:phone', key_thumbprint='a'*43,
        decision='approve_once', proposal_id=proposal['proposal_id'], binding_sha256=proposal['binding_sha256']))
    replacement = resume(world, feature_id='f', body=ResumeRequest(request_id='resume', approval_id=approval['approval_id']))
    assert replacement['new_attempt_id'] != first['attempt_id']
    with session_factory(world)() as s:
        intent = s.scalar(select(DispatchIntent.intent_id))
    job_id = consume_intent(world, intent_id=intent)
    with session_factory(world)() as s, s.begin():
        s.get(WorkerJob, first['job_id']).state = 'cancelled'
    assert claim_job(world, worker_id='w', lease_ttl_seconds=720) == job_id
    context = prelaunch_context(world, job_id=job_id, worker_id='w', job_lease_epoch=1)
    assert context['snapshot']['contract_version'] == ROLE_CONTRACT_V2
    assert context['snapshot_sha256'] == selected['snapshot_sha256']
    assert context['execution_spec']['role_config']['model'] == 'gpt-6-astra'
    assert context['execution_spec']['role_config']['reasoning'] == 'low'
    with session_factory(world)() as s:
        assert s.get(ExecutionJobBinding, replacement['new_attempt_id']).origin == 'replacement'
        assert s.get(WorkflowAction, p['action_id']).execution_snapshot_sha256 == p['snapshot_sha256']
        assert 'contract_version' not in json.loads(s.get(ExecutionSnapshot, p['snapshot_sha256']).body)
