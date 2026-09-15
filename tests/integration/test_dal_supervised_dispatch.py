"""Real migrated service/remote transport, synthetic signer and executor only."""
from datetime import timedelta
from types import SimpleNamespace
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import select
from personal_agent.auth.device_keys import encode_device_public_key
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.service.app import create_app
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import Lease, ProviderAttempt
from personal_agent_dal.storage.transport_models import WorkerEnrollment
from personal_agent_dal.machine.isolation_evidence import register_supervisor
from personal_agent_dal.worker.remote import RemoteHttpAdapter, CachedToken
from personal_agent_dal.worker.supervisor import Supervisor, SupervisorRefusal
from personal_agent_dal.worker.prelaunch import prepare
from tests.dal.test_p0_02_review_regressions import world
from tests.dal.test_resume_dispatch import replacement
from tests.dal.test_runtime_mapping import pins


@pytest.fixture
def composed(world,tmp_path):
    from personal_agent_dal.service.tokens import issue_token
    intent,attempt=replacement(world)
    key=ec.generate_private_key(ec.SECP256R1())
    with session_factory(world)() as s,s.begin():
        s.add(WorkerEnrollment(worker_id='w',machine_id='synthetic-mini',capabilities='[]',created_at=utc_now()))
    register_supervisor(world,kid='supervisor',worker_id='w',machine_id='synthetic-mini',
        public_key=encode_device_public_key(key.public_key()),boot_id='boot',supervisor_epoch=1)
    app=create_app(world,service_key=b'synthetic-service',enrollment_secret=b'synthetic-enroll',
        resume_config={'issuer':'pa','audience':'dal','keys':{},'profiles':[]})
    settings=SimpleNamespace(worker_id='w',endpoint='https://testserver',retry_attempts=1)
    transport=RemoteHttpAdapter(settings,client=TestClient(app))
    expires=int(utc_now().timestamp())+600
    transport._token=CachedToken('w',issue_token(worker_id='w',capabilities=[],key=b'synthetic-service',expires_at_epoch=expires),expires)
    lease=transport.claim()
    assert lease is not None and lease.job_id!='j'
    identity=dict(kid='supervisor',worker_id='w',machine_id='synthetic-mini',registration_epoch=1,
                  boot_id='boot',supervisor_epoch=1)
    supervisor=Supervisor(tmp_path/'supervisor',boot_id='boot',epoch=1)
    return transport,lease,supervisor,key,identity,attempt


def prepared(composed):
    t,l,s,key,identity,attempt=composed
    context=t.prelaunch_context(l)
    reservation,sha=prepare(t,l,context,supervisor=s,pins=pins(context['snapshot']),read_roots=(),identity=identity,key=key)
    return reservation,sha


def test_missing_machine_proof_zero_spawn(composed,world):
    t,l,s,*_=composed;r,sha=prepared(composed);calls=[]
    with pytest.raises(SupervisorRefusal,match='MINI_ACCEPTANCE_REQUIRED'):
        s.launch(r['reservation_id'],executor=lambda _:calls.append(1))
    with session_factory(world)() as session:
        assert session.get(ProviderAttempt,composed[-1]).dispatch_started_at is None
    assert calls==[]


def test_controlled_executor_committed_gate_and_zero_redispatch(composed,world):
    t,l,s,*_=composed;r,sha=prepared(composed);calls=[]
    s.controlled_prelaunch(r['reservation_id'],commit_permission=lambda:t.dispatch_prelaunch(l,sha)['code'],
                          executor=lambda _:calls.append(1))
    with session_factory(world)() as session:
        assert session.get(ProviderAttempt,composed[-1]).state=='dispatching'
    with pytest.raises(SupervisorRefusal):
        s.controlled_prelaunch(r['reservation_id'],commit_permission=lambda:t.dispatch_prelaunch(l,sha)['code'],executor=lambda _:calls.append(1))
    assert calls==[1]
    assert t.claim() is None


@pytest.mark.parametrize('kind',['policy','job','cancel','ack'])
def test_prelaunch_races_do_not_execute(composed,world,kind,monkeypatch):
    from personal_agent_dal.storage.worker_models import WorkerJob
    from personal_agent_dal.machine.action_lifecycle import cancel_execution
    t,l,s,*_=composed;calls=[]
    if kind=='ack':
        original=t.acknowledge_prelaunch
        def lost(*args): original(*args);raise ValueError('response_lost')
        monkeypatch.setattr(t,'acknowledge_prelaunch',lost)
        with pytest.raises(ValueError):prepared(composed)
    else:
        r,sha=prepared(composed)
        if kind=='cancel':cancel_execution(world,feature_id='f',expected_gate_version=3)
        else:
            with session_factory(world)() as session,session.begin():
                if kind=='policy':session.get(Lease,t.prelaunch_context(l)['lease_id']).expires_at=utc_now()-timedelta(seconds=1)
                else:session.get(WorkerJob,l.job_id).lease_epoch+=1
        with pytest.raises((ValueError,RuntimeError)):
            s.controlled_prelaunch(r['reservation_id'],commit_permission=lambda:t.dispatch_prelaunch(l,sha)['code'],executor=lambda _:calls.append(1))
    assert calls==[]
    with session_factory(world)() as session:
        assert session.get(ProviderAttempt,composed[-1]).dispatch_started_at is None


def test_crash_after_cas_stays_unknown_no_execution(composed,world):
    t,l,s,*_=composed;r,sha=prepared(composed);calls=[]
    def lost():
        assert t.dispatch_prelaunch(l,sha)['code']=='DISPATCH_GRANTED'
        raise RuntimeError('lost committed permission response')
    with pytest.raises(RuntimeError):
        s.controlled_prelaunch(r['reservation_id'],commit_permission=lost,executor=lambda _:calls.append(1))
    assert calls==[]
    with session_factory(world)() as session:
        assert session.get(ProviderAttempt,composed[-1]).dispatch_started_at is not None
    with pytest.raises(RuntimeError):t.dispatch_prelaunch(l,sha)


def test_actual_worker_entry_reaches_prelaunch_and_does_not_read_token(composed,tmp_path,monkeypatch):
    from personal_agent_dal.worker.poll_once import run_poll_once
    t,l,*_=composed
    # Claim itself was authenticated above; re-use that one claimed job to
    # exercise the Worker orchestration without manufacturing a second lease.
    monkeypatch.setattr(t,'claim',lambda:l)
    calls=[]
    monkeypatch.setattr('subprocess.Popen',lambda *a,**kw:calls.append(1))
    config=SimpleNamespace(kill_switch_path=tmp_path/'disabled',supervisor_config_path=None)
    result=run_poll_once(t,config)
    assert result.error=='SUPERVISOR_CONFIG_REQUIRED' and result.state is None
    assert calls==[]


def test_lost_manifest_ack_can_replay_same_inventory_without_dispatch(composed,world,monkeypatch):
    t,l,s,*_=composed
    original=t.acknowledge_prelaunch
    calls=[]
    def lost(*args):
        original(*args);calls.append(1);raise ValueError('lost')
    monkeypatch.setattr(t,'acknowledge_prelaunch',lost)
    with pytest.raises(ValueError):prepared(composed)
    monkeypatch.setattr(t,'acknowledge_prelaunch',original)
    r,sha=prepared(composed)
    with session_factory(world)() as session:
        assert session.get(ProviderAttempt,composed[-1]).dispatch_started_at is None
    assert calls==[1]


def test_policy_issuance_concurrent_replay_has_one_binding(composed,world):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import func
    from personal_agent_dal.machine.resume_dispatch import prelaunch_context
    from personal_agent_dal.storage.machine_models import ResumeLeaseIssuance, Approval
    from personal_agent_dal.storage.worker_models import WorkerJob
    t,l,*_=composed
    with ThreadPoolExecutor(max_workers=2) as pool:
        contexts=list(pool.map(lambda _:prelaunch_context(world,job_id=l.job_id,worker_id='w',job_lease_epoch=l.lease_epoch),range(2)))
    assert contexts[0]==contexts[1]
    world.dispose()
    assert t.prelaunch_context(l)==contexts[0]
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(ResumeLeaseIssuance))==1
        lease=s.get(Lease,contexts[0]['lease_id'])
        job=s.get(WorkerJob,l.job_id)
        approval=s.scalar(select(Approval).where(Approval.action=='resume'))
        assert lease.expires_at<=min(job.lease_expires_at,approval.expires_at,lease.created_at+timedelta(minutes=15))


@pytest.mark.parametrize('kind',['expired','revoked','worker_revoked','approval_revoked','consumption_missing','wrong_worker'])
def test_policy_issuance_never_revives_authority(composed,world,kind):
    from personal_agent_dal.storage.machine_models import Approval, ResumeApprovalBinding
    from personal_agent_dal.machine.resume_authority import revoke_decision
    from personal_agent_dal.machine.resume_dispatch import prelaunch_context
    from personal_agent_dal.storage.worker_models import WorkerJob
    t,l,*_=composed
    context=t.prelaunch_context(l)
    with session_factory(world)() as s,s.begin():
        lease=s.get(Lease,context['lease_id'])
        if kind=='expired':lease.expires_at=utc_now()-timedelta(seconds=1)
        elif kind=='revoked':lease.revoked_at=utc_now()
        elif kind=='worker_revoked':s.get(WorkerEnrollment,'w').revoked_at=utc_now()
        elif kind=='consumption_missing':
            approval=s.scalar(select(Approval).where(Approval.action=='resume'));approval.consumed_at=None;approval.consumed_by_command_id=None
        elif kind=='approval_revoked':decision_id=s.scalar(select(ResumeApprovalBinding.decision_id))
    if kind=='approval_revoked':revoke_decision(world,decision_id=decision_id)
    with pytest.raises(ValueError):
        prelaunch_context(world,job_id=l.job_id,worker_id='other' if kind=='wrong_worker' else 'w',job_lease_epoch=l.lease_epoch)
    if kind=='revoked':
        with session_factory(world)() as s,s.begin():s.get(WorkerJob,l.job_id).lease_epoch+=1
        with pytest.raises(ValueError,match='POLICY_LEASE_STALE'):
            prelaunch_context(world,job_id=l.job_id,worker_id='w',job_lease_epoch=l.lease_epoch+1)


def test_fresh_reclaim_epoch_is_explicit_and_bounded(composed,world,monkeypatch):
    from personal_agent_dal.machine import resume_dispatch
    from personal_agent_dal.worker import queue
    from personal_agent_dal.storage.worker_models import WorkerJob
    from personal_agent_dal.storage.machine_models import Approval
    t,l,*_=composed
    old=t.prelaunch_context(l)
    with session_factory(world)() as s:
        lease=s.get(Lease,old['lease_id'])
        old_expiry=lease.expires_at
        first_ceiling=lease.created_at+timedelta(minutes=15)
        after=s.get(WorkerJob,l.job_id).lease_expires_at+timedelta(seconds=1)
    monkeypatch.setattr(queue,'utc_now',lambda:after)
    monkeypatch.setattr(resume_dispatch,'utc_now',lambda:after)
    # The actual service reclaim/claim flow increments the Job epoch.
    fresh=t.claim()
    assert fresh.job_id==l.job_id and fresh.lease_epoch==l.lease_epoch+1
    with pytest.raises((ValueError,RuntimeError)):
        t.prelaunch_context(l)
    new=t.prelaunch_context(fresh)
    assert new['lease_id']!=old['lease_id'] and new['policy_lease_epoch']==fresh.lease_epoch
    with session_factory(world)() as s:
        assert s.get(Lease,old['lease_id']).expires_at==old_expiry
        approval=s.scalar(select(Approval).where(Approval.action=='resume'))
        assert s.get(Lease,new['lease_id']).expires_at<=min(first_ceiling,approval.expires_at)


def test_revoke_does_not_claim_to_undo_consumed_dispatch(composed,world):
    from personal_agent_dal.machine.resume_authority import revoke_decision
    from personal_agent_dal.storage.machine_models import ResumeApprovalBinding
    t,l,s,*_=composed
    reservation,sha=prepared(composed)
    assert t.dispatch_prelaunch(l,sha)['code']=='DISPATCH_GRANTED'
    with session_factory(world)() as session:decision=session.scalar(select(ResumeApprovalBinding.decision_id))
    result=revoke_decision(world,decision_id=decision)
    assert result['status']=='revoked_for_future_use'
    with session_factory(world)() as session:
        assert session.get(ProviderAttempt,composed[-1]).state=='dispatching'
    with pytest.raises((ValueError,RuntimeError)):t.dispatch_prelaunch(l,sha)


def test_new_control_records_have_migrated_foreign_keys(composed,world):
    from sqlalchemy import inspect,text
    from personal_agent_dal.machine.resume_authority import revoke_decision
    t,l,*_=composed
    t.prelaunch_context(l)
    revoke_decision(world,decision_id='future-decision')
    expected={'resume_revoke_receipts':{'decision_id':'resume_revocations.decision_id'},
        'resume_lease_issuances':{'intent_id':'resume_episodes.intent_id','lease_id':'leases.lease_id'}}
    for table,columns in expected.items():
        actual={fk['constrained_columns'][0]:fk['referred_table']+'.'+fk['referred_columns'][0] for fk in inspect(world).get_foreign_keys(table)}
        assert actual==columns
    with world.connect() as c:
        for table in expected:assert c.execute(text(f'SELECT count(*) FROM {table}')).scalar()==1
        assert c.execute(text('PRAGMA foreign_key_check')).all()==[]


@pytest.mark.parametrize('shape', ['os','sqlite','timeout','subprocess','key','value','refusal'])
def test_prelaunch_errors_are_redacted_without_dispatch(composed, world, tmp_path, monkeypatch, shape):
    import sqlite3
    import subprocess
    from personal_agent_dal.worker.poll_once import run_poll_once
    t,l,*_=composed
    monkeypatch.setattr(t,'claim',lambda:l)
    errors={'os':OSError('/private/synthetic'), 'sqlite':sqlite3.OperationalError('/private/synthetic'),
            'timeout':subprocess.TimeoutExpired('/private/synthetic',1),
            'subprocess':subprocess.CalledProcessError(1,'/private/synthetic'),
            'key':KeyError('/private/synthetic'), 'value':ValueError('/private/synthetic'),
            'refusal':SupervisorRefusal('WORKSPACE_ORPHANED')}
    def fail(*args): raise errors[shape]
    monkeypatch.setattr('personal_agent_dal.worker.prelaunch.worker_prelaunch',fail)
    calls=[]
    monkeypatch.setattr('personal_agent_dal.worker.poll_once._execute_job',lambda *a,**kw:calls.append(1))
    result=run_poll_once(t,SimpleNamespace(kill_switch_path=tmp_path/'disabled'))
    assert result.error==('WORKSPACE_ORPHANED' if shape=='refusal' else 'PRELAUNCH_ERROR')
    assert result.state is None and calls==[]
    with session_factory(world)() as session:
        assert session.get(ProviderAttempt,composed[-1]).dispatch_started_at is None


@pytest.mark.parametrize('offset', [1,899,900,901])
def test_lost_ack_replay_keeps_original_reservation_window(composed, world, monkeypatch, offset):
    import json
    from personal_agent_dal.storage.transport_models import SupervisorLaunchManifest
    t,l,s,*_=composed
    original=t.acknowledge_prelaunch
    def lost(*args):
        original(*args)
        raise OSError('synthetic lost acknowledgement')
    monkeypatch.setattr(t,'acknowledge_prelaunch',lost)
    with pytest.raises(OSError):prepared(composed)
    with session_factory(world)() as session:
        record=session.get(SupervisorLaunchManifest,composed[-1])
        old_sha,old_body=record.sha256,record.body
    issued=json.loads(old_body)['issued_at']
    assert json.loads(old_body)['expires_at']==issued+900
    monkeypatch.setattr('personal_agent_dal.worker.prelaunch.time',SimpleNamespace(time=lambda:issued+offset))
    acknowledgements=[]
    def ack(*args):
        acknowledgements.append(1)
        return original(*args)
    monkeypatch.setattr(t,'acknowledge_prelaunch',ack)
    if offset>=900:
        with pytest.raises(SupervisorRefusal,match='RESERVATION_EXPIRED'):prepared(composed)
        assert acknowledgements==[]
    else:
        _,sha=prepared(composed)
        assert sha==old_sha and acknowledgements==[1]
    with session_factory(world)() as session:
        record=session.get(SupervisorLaunchManifest,composed[-1])
        assert (record.sha256,record.body)==(old_sha,old_body)
        assert session.get(ProviderAttempt,composed[-1]).dispatch_started_at is None
