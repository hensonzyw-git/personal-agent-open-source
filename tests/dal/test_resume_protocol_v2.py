"""Exact proposal authority and frozen pre-upgrade replay, with real SQL/ES256."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, event

from personal_agent.api.dal_client import sign_decision, verify_decision
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.machine.resume_authority import propose, resume, ResumeRequest, revoke_decision
from personal_agent_dal.machine.workflow_selection import digest
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import Approval, ResumeApprovalBinding, ResumeProposal, ResumeReceipt
from personal_agent_dal.storage.models import Feature
from tests.dal.test_p0_02_review_regressions import world
from tests.dal.test_p0_02_resume_authority import setup_resume, approve


def seed_historical(engine, p, claims):
    """Frozen old-shape imported rows; never grant legacy authority via new importer."""
    assert 'proposal_id' not in claims
    b = p['binding']
    now = datetime.fromtimestamp(claims['iat'], timezone.utc)
    with session_factory(engine)() as s, s.begin():
        f = s.get(Feature, b['feature_id'])
        s.add(Approval(approval_id='historical', action='resume', feature_id=f.feature_id,
            decision_id=claims['decision_id'], decision_version=1, expected_feature_version=f.version,
            expected_state=f.state, state_sha256=b['state_sha256'], artifact_sha256=b['artifact_sha256'],
            device_id=claims['device_id'], subject_id=claims['subject_id'], valid_from=now,
            expires_at=datetime.fromtimestamp(claims['exp'], timezone.utc),
            idempotency_key='resume:'+claims['decision_id'], policy_version=f.policy_version,
            replay_policy='consume_once', recorded_at=now))
        s.flush()
        s.add(ResumeApprovalBinding(approval_id='historical', proposal_id=p['proposal_id'],
            decision_id=claims['decision_id'], jti=claims['jti'], claims=canonical_json(claims),
            claims_sha256=digest(claims), key_thumbprint=claims['key_thumbprint']))
    engine.dispose()
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1


def test_same_digest_binds_original_proposal(world):
    _, p, key, claims = setup_resume(world)
    p2 = propose(world, feature_id='f', selection_id=p['binding']['selection_id'], request_id='second')
    assert p2['binding_sha256'] == p['binding_sha256'] and p2['proposal_id'] != p['proposal_id']
    result = approve(world, key, claims)
    world.dispose()
    assert approve(world, key, claims) == result
    with session_factory(world)() as s:
        assert s.get(ResumeApprovalBinding, result['approval_id']).proposal_id == p['proposal_id']
    with pytest.raises(ValueError, match='DECISION_CONFLICT'):
        approve(world, key, {**claims, 'proposal_id': p2['proposal_id']})


@pytest.mark.parametrize('problem', ['expired', 'hash', 'missing-id'])
def test_new_proposal_never_rescues_signed_source(world, problem):
    _, p, key, claims = setup_resume(world)
    propose(world, feature_id='f', selection_id=p['binding']['selection_id'], request_id='second')
    if problem == 'expired':
        with session_factory(world)() as s, s.begin():
            s.get(ResumeProposal, p['proposal_id']).expires_at = utc_now()-timedelta(seconds=1)
    elif problem == 'hash': claims['binding_sha256'] = '0'*64
    else: claims['proposal_id'] = 'absent'
    with pytest.raises(ValueError): approve(world, key, claims)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 0


@pytest.mark.parametrize('mutation', ['legacy', 'null', 'extra', 'integer', 'empty'])
def test_missing_or_malformed_id_never_imports(world, mutation):
    _, p, key, claims = setup_resume(world)
    if mutation in ('legacy', 'extra'): claims.pop('proposal_id')
    if mutation == 'extra': claims['extra'] = 'x'
    if mutation == 'null': claims['proposal_id'] = None
    if mutation == 'integer': claims['proposal_id'] = 1
    if mutation == 'empty': claims['proposal_id'] = ''
    expected = 'LEGACY_DECISION_REAPPROVAL_REQUIRED' if mutation == 'legacy' else 'ASSERTION_INVALID'
    with pytest.raises(ValueError, match=expected): approve(world, key, claims)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 0
        assert s.scalar(select(func.count()).select_from(ResumeApprovalBinding)) == 0


def test_legacy_exact_replay_and_consumption_survive_upgrade(world):
    _, p, key, claims = setup_resume(world)
    claims.pop('proposal_id')
    seed_historical(world, p, claims)
    token = sign_decision(claims, key=key, kid='resume')
    with pytest.raises(ValueError):
        verify_decision(token, keys={'resume':key.public_key()}, issuer='pa-resume',
                        audience='dal-resume', now_epoch=int(utc_now().timestamp()))
    result = approve(world, key, claims)
    body = ResumeRequest(request_id='consume', approval_id='historical')
    receipt = resume(world, feature_id='f', body=body)
    world.dispose()
    assert approve(world, key, claims) == result
    assert resume(world, feature_id='f', body=body) == receipt
    with pytest.raises(ValueError):
        resume(world, feature_id='f', body=ResumeRequest(request_id='again', approval_id='historical'))
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 1
        assert s.scalar(select(func.count()).select_from(ResumeReceipt)) == 1
        assert s.get(ResumeApprovalBinding, 'historical').claims == canonical_json(claims)


@pytest.mark.parametrize('problem', ['decision', 'jti', 'digest', 'revoked', 'expired', 'extra', 'null'])
def test_legacy_conflicts_fail_closed(world, problem):
    _, p, key, claims = setup_resume(world)
    claims.pop('proposal_id')
    seed_historical(world, p, claims)
    if problem == 'decision': claims['decision_id'] = 'other'
    elif problem == 'jti': claims['jti'] = 'other'
    elif problem == 'digest': claims['key_thumbprint'] = 'b'*43
    elif problem == 'revoked': revoke_decision(world, decision_id=claims['decision_id'])
    elif problem == 'expired': claims['exp'] = int(utc_now().timestamp())
    elif problem == 'extra': claims['extra'] = 'x'
    else: claims['proposal_id'] = None
    with pytest.raises(ValueError): approve(world, key, claims)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 1
        assert s.scalar(select(func.count()).select_from(ResumeApprovalBinding)) == 1


@pytest.mark.parametrize('problem', ['signature', 'null', 'extra', 'identity', 'issuer', 'audience'])
def test_bad_signature_rejected_before_sql(world, problem):
    _, p, key, claims = setup_resume(world)
    claims.pop('proposal_id')
    if problem == 'null': claims['proposal_id'] = None
    elif problem == 'extra': claims['extra'] = 'x'
    elif problem == 'identity': claims['subject_id'] = 'device:wrong'
    elif problem == 'issuer': claims['iss'] = 'wrong'
    elif problem == 'audience': claims['aud'] = 'wrong'
    def no_sql(*args): raise AssertionError('unverified claims reached SQL')
    event.listen(world, 'before_cursor_execute', no_sql)
    try:
        from personal_agent_dal.machine.resume_authority import import_decision
        with pytest.raises(ValueError):
            import_decision(world, assertion=sign_decision(claims, key=key, kid='untrusted' if problem == 'signature' else 'resume'),
                keys={'resume':key.public_key()}, issuer='pa-resume', audience='dal-resume')
    finally:
        event.remove(world, 'before_cursor_execute', no_sql)


def test_exact_legacy_expiry_is_checked_before_replay(world):
    from personal_agent_dal.machine.resume_authority import import_decision
    _, p, key, claims = setup_resume(world)
    claims.pop('proposal_id')
    seed_historical(world, p, claims)
    with pytest.raises(ValueError, match='ASSERTION_INVALID'):
        import_decision(world, assertion=sign_decision(claims, key=key, kid='resume'),
            keys={'resume':key.public_key()}, issuer='pa-resume', audience='dal-resume',
            now=datetime.fromtimestamp(claims['exp'], timezone.utc))
