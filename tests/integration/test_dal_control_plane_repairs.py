"""Synthetic regressions for the approved control-plane repair only."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from tests.integration.test_dal_resume_bridge import bridge_world, click, token_ring, keyring
from tests.dal.test_p0_02_review_regressions import world

DAL_FKS = {
    'execution_snapshots': {'revision_id': 'workflow_profile_revisions.revision_id'},
    'workflow_selections': {'feature_id': 'features.feature_id', 'snapshot_sha256': 'execution_snapshots.sha256'},
    'resume_approval_bindings': {'approval_id': 'approvals.approval_id', 'proposal_id': 'resume_proposals.proposal_id'},
    'replacement_budgets': {'old_attempt_id': 'provider_attempts.attempt_id', 'new_attempt_id': 'provider_attempts.attempt_id',
        'old_action_id': 'workflow_actions.action_id', 'new_action_id': 'workflow_actions.action_id',
        'approval_id': 'approvals.approval_id', 'receipt_id': 'resume_receipts.receipt_id'},
    'dispatch_intents': {'attempt_id': 'provider_attempts.attempt_id', 'receipt_id': 'resume_receipts.receipt_id',
        'selection_id': 'workflow_selections.selection_id'},
    'supervisor_identities': {'worker_id': 'worker_enrollments.worker_id'},
    'supervisor_launch_manifests': {'attempt_id': 'provider_attempts.attempt_id'},
    'isolation_evidence': {'challenge_id': 'isolation_challenges.challenge_id', 'reserved_by': 'provider_attempts.attempt_id'},
}
PA_FKS = {
    'dal_resume_proposals': {'device_id': 'devices.device_id'},
    'dal_resume_decisions': {'proposal_id': 'dal_resume_proposals.proposal_id', 'device_id': 'devices.device_id'},
    'dal_resume_deliveries': {'decision_id': 'dal_resume_decisions.decision_id'},
}


def check_graph(engine, metadata, graph):
    for table, edges in graph.items():
        actual = {fk['constrained_columns'][0]: fk['referred_table']+'.'+fk['referred_columns'][0]
                  for fk in inspect(engine).get_foreign_keys(table)}
        assert actual == edges
        assert {fk.parent.name: fk.target_fullname for fk in metadata.tables[table].foreign_keys} == edges
        for column, target in edges.items():
            # Each populated edge independently refuses orphaning and deletion of its parent.
            with engine.connect() as c:
                assert c.execute(text(f'SELECT count(*) FROM {table}')).scalar() > 0
            with pytest.raises(IntegrityError), engine.begin() as c:
                c.execute(text(f"UPDATE {table} SET {column} = :missing"), {'missing': '0'*64})
            parent, pk = target.split('.')
            with pytest.raises(IntegrityError), engine.begin() as c:
                c.execute(text(f'DELETE FROM {parent} WHERE {pk} IN (SELECT {column} FROM {table})'))
    with engine.connect() as c:
        assert not c.execute(text('PRAGMA foreign_key_check')).all()


def test_pa_fk_graph(bridge_world):
    from personal_agent.storage.models import Base
    engine, bridge, auth, p, transport = bridge_world
    bridge.decide(auth, click(p))
    check_graph(engine, Base.metadata, PA_FKS)


def test_dal_fk_graph(world):
    from tests.dal.test_p0_02_isolation_evidence import isolation_world
    from tests.dal.test_p0_02_resume_authority import setup_resume, approve
    from personal_agent.api.dal_client import sign_decision
    from personal_agent_dal.machine.isolation_evidence import import_evidence
    from personal_agent_dal.machine.resume_authority import resume, ResumeRequest
    from personal_agent_dal.storage.models import Base
    _, key, body = isolation_world(world)
    import_evidence(world, assertion=sign_decision(body,key=key,kid='supervisor'), worker_id='w')
    _, p, key, claims = setup_resume(world)
    claims['decision'] = 'approve_once_accept_duplicate_cost'
    approved = approve(world,key,claims)
    resume(world,feature_id='f',body=ResumeRequest(request_id='repair',approval_id=approved['approval_id']))
    check_graph(world, Base.metadata, DAL_FKS)


def test_failed_deliveries_count_actual_attempts(bridge_world, caplog):
    engine, bridge, auth, p, transport = bridge_world
    decision = bridge.decide(auth, click(p))
    def refuse(_): raise ValueError('synthetic-private-payload')
    bridge.transport.deliver = refuse
    bridge.deliver_pending()
    bridge.deliver_pending()
    with engine.connect() as c:
        assert c.execute(text('SELECT attempts FROM dal_resume_deliveries')).scalar() == 2
        assert c.execute(text('SELECT status FROM dal_resume_deliveries')).scalar() == 'queued'
    assert 'synthetic-private-payload' not in caplog.text
    assert 'refused' in caplog.text


def test_composed_lifespan_recovers_read_error(bridge_world, token_ring, keyring, monkeypatch, caplog):
    from personal_agent.api.app import AgentApiDeps, build_app
    from tests.integration.test_dal_resume_bridge import IDENTIFIER_KEY, CURSOR_KEY
    from personal_agent.storage.engine import session_factory
    from personal_agent_core.timeutil import utc_now
    engine, bridge, auth, p, transport = bridge_world
    bridge.decide(auth, click(p))
    transport.lose_response = False
    original_sessions = bridge.sessions
    calls = 0
    def sessions():
        nonlocal calls
        calls += 1
        if calls == 1: raise OperationalError('synthetic-private-sql', {}, Exception('read failed'))
        return original_sessions()
    bridge.sessions = sessions
    deps = AgentApiDeps(session_factory=session_factory(engine),token_ring=token_ring,keyring=keyring,
        identifier_key=IDENTIFIER_KEY,cursor_key=CURSOR_KEY,build_interpreter=lambda *a: None,
        build_envelope=lambda *a: None,build_dispatcher=lambda *a: None,build_authorizer=lambda *a: None,
        capabilities=lambda a: [],now=utc_now,dal_resume=bridge)
    app = build_app(deps)
    async def run():
        async with app.router.lifespan_context(app):
            for _ in range(400):
                with engine.connect() as c:
                    if c.execute(text('SELECT status FROM dal_resume_deliveries')).scalar() == 'accepted': return
                await asyncio.sleep(.01)
            pytest.fail('lifespan delivery did not recover')
    asyncio.run(run())
    assert transport.sent == 1
    assert 'synthetic-private-sql' not in caplog.text
    assert 'retry' in caplog.text


@pytest.mark.parametrize('problem', ['owner','symlink','mode','oversize','directory','fifo'])
def test_bridge_file_boundary(tmp_path, monkeypatch, problem, token_ring):
    import os
    import stat
    from personal_agent.api.dal_client import load_bridge
    path = tmp_path/'synthetic-config'
    path.write_text('{}')
    path.chmod(0o600)
    if problem == 'owner': monkeypatch.setattr(os, 'getuid', lambda: -1)
    elif problem == 'symlink':
        link = tmp_path/'link'; link.symlink_to(path); path = link
    elif problem == 'mode': path.chmod(0o644)
    elif problem == 'oversize': path.write_bytes(b' '*65537)
    elif problem == 'directory': path.unlink(); path.mkdir(mode=0o600)
    elif problem == 'fifo': path.unlink(); os.mkfifo(path,0o600)
    with pytest.raises(ValueError, match='DAL_RESUME_CONFIG_FILE_INVALID'):
        load_bridge(path,session_factory=None,token_ring=token_ring)


def composed_app(bridge_world, token_ring, keyring):
    from personal_agent.api.app import AgentApiDeps, build_app
    from tests.integration.test_agent_api import IDENTIFIER_KEY, CURSOR_KEY
    from personal_agent.storage.engine import session_factory
    from personal_agent_core.timeutil import utc_now
    engine, bridge, *_ = bridge_world
    return build_app(AgentApiDeps(session_factory=session_factory(engine),token_ring=token_ring,keyring=keyring,
        identifier_key=IDENTIFIER_KEY,cursor_key=CURSOR_KEY,build_interpreter=lambda *a: None,
        build_envelope=lambda *a: None,build_dispatcher=lambda *a: None,build_authorizer=lambda *a: None,
        capabilities=lambda a: [],now=utc_now,dal_resume=bridge))


@pytest.mark.parametrize('failure,expected_calls', [('transient',3),('unexpected',1)])
def test_lifespan_failures_are_bounded_and_observed(bridge_world, token_ring, keyring, caplog, failure, expected_calls):
    bridge = bridge_world[1]
    calls = 0
    def fail(**kwargs):
        nonlocal calls
        calls += 1
        if failure == 'transient': raise OperationalError('synthetic-private-sql', {}, Exception('detail'))
        raise RuntimeError('synthetic-private-detail')
    bridge.deliver_pending = fail
    app = composed_app(bridge_world,token_ring,keyring)
    async def run():
        async with app.router.lifespan_context(app):
            task = app.state.dal_resume_delivery_task
            with pytest.raises((OperationalError, RuntimeError)):
                await asyncio.wait_for(asyncio.shield(task), 2)
            assert task.done()
    asyncio.run(run())
    assert calls == expected_calls
    assert 'stopped' in caplog.text
    assert 'synthetic-private' not in caplog.text


def test_lifespan_shutdown_drains_inflight_thread(bridge_world, token_ring, keyring):
    import threading
    bridge = bridge_world[1]
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = 0
    def blocked(*, stop_event):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        assert stop_event.is_set()
        finished.set()
    bridge.deliver_pending = blocked
    app = composed_app(bridge_world,token_ring,keyring)
    async def run():
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            shutdown = asyncio.create_task(context.__aexit__(None,None,None))
            await asyncio.sleep(.02)
            assert not shutdown.done()
        finally:
            release.set()
        await asyncio.wait_for(shutdown, 1)
        assert app.state.dal_resume_delivery_task.cancelled()
        assert finished.is_set()
    asyncio.run(run())
    assert calls == 1


def test_expiry_without_dispatch_does_not_increment_attempts(bridge_world):
    from datetime import timedelta
    from personal_agent_core.timeutil import utc_now
    engine, bridge, auth, p, transport = bridge_world
    bridge.decide(auth,click(p))
    bridge.now=lambda: utc_now()+timedelta(hours=1)
    bridge.deliver_pending()
    with engine.connect() as c:
        assert c.execute(text('SELECT status, attempts FROM dal_resume_deliveries')).one() == ('expired',0)
    assert transport.sent == 0


@pytest.mark.parametrize('status,category', [(403,'refused'),(429,'transient'),(503,'transient')])
def test_delivery_http_classification_is_payload_free(bridge_world,caplog,status,category):
    import httpx
    engine,bridge,auth,p,_=bridge_world
    bridge.decide(auth,click(p))
    def failed(_):
        response=httpx.Response(status,request=httpx.Request('POST','https://synthetic.invalid/private'),text='private-body')
        response.raise_for_status()
    bridge.transport.deliver=failed
    bridge.deliver_pending()
    assert category in caplog.text
    assert 'private' not in caplog.text
    with engine.connect() as c:
        assert c.execute(text('SELECT status,attempts FROM dal_resume_deliveries')).one()==('queued',1)


def test_bridge_file_read_uses_verified_fd_and_closes_it(tmp_path,monkeypatch):
    import os
    from personal_agent.api.dal_client import _read_bridge_file
    path=tmp_path/'original'; path.write_bytes(b'synthetic-original'); path.chmod(0o600)
    replacement=tmp_path/'replacement'; replacement.write_bytes(b'synthetic-replacement'); replacement.chmod(0o600)
    fstat=os.fstat
    seen=[]
    def swapped(fd):
        info=fstat(fd); seen.append(fd)
        replacement.replace(path)
        return info
    monkeypatch.setattr(os,'fstat',swapped)
    assert _read_bridge_file(path,kind='KEY')==b'synthetic-original'
    with pytest.raises(OSError): fstat(seen[0])


def test_bridge_bounded_read_even_if_size_changes(tmp_path,monkeypatch):
    import os
    from personal_agent.api.dal_client import _read_bridge_file
    path=tmp_path/'growing'; path.write_bytes(b'x'*100); path.chmod(0o600)
    fstat=os.fstat
    def small(fd):
        info=fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode,st_uid=info.st_uid,st_size=0)
    monkeypatch.setattr(os,'fstat',small)
    with pytest.raises(ValueError,match='KEY_FILE_INVALID'): _read_bridge_file(path,kind='KEY',limit=32)


def test_load_bridge_reads_dedicated_synthetic_key(tmp_path,monkeypatch,token_ring):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption
    from personal_agent.api.dal_client import load_bridge
    key=ec.generate_private_key(ec.SECP256R1())
    path=tmp_path/'synthetic.pem'
    path.write_bytes(key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption())); path.chmod(0o600)
    config=tmp_path/'config.json'
    config.write_text(json.dumps(dict(base_url='https://synthetic.invalid',signing_key_file=str(path),kid='synthetic',issuer='pa-resume',audience='dal-resume')))
    config.chmod(0o600)
    bridge=load_bridge(config,session_factory=None,token_ring=token_ring)
    assert bridge.key.public_key().public_numbers()==key.public_key().public_numbers()
    path.chmod(0o644)
    with pytest.raises(ValueError,match='KEY_FILE_INVALID'):
        load_bridge(config,session_factory=None,token_ring=token_ring)


@pytest.mark.parametrize('database', ['pa','dal'])
def test_empty_authority_downgrade_restores_fk_and_preserves_legacy(tmp_path,database):
    if database=='pa':
        from personal_agent.storage import db
        from personal_agent.storage.engine import create_database_engine
        from personal_agent.storage.models import Device
        from personal_agent_core.timeutil import utc_now
        previous='0005_finance_safe_retry'
    else:
        from personal_agent_dal.storage import db
        from personal_agent_dal.storage.engine import create_database_engine
        previous='0014'
    engine=create_database_engine(tmp_path/'migration.db')
    try:
        db.upgrade(engine,previous)
        if database=='dal':
            from tests.dal.factories import feature_row
            from personal_agent_core.timeutil import utc_now
            from personal_agent_dal.storage.engine import session_factory
            with session_factory(engine)() as s,s.begin(): s.add(feature_row(feature_id='legacy',version=1,state='coding',now=utc_now()))
        db.upgrade(engine)
        db.downgrade(engine,previous)
        with engine.connect() as c:
            assert c.execute(text('PRAGMA foreign_keys')).scalar()==1
            assert not c.execute(text('PRAGMA foreign_key_check')).all()
            if database=='dal': assert c.execute(text('SELECT feature_id FROM features')).scalar()=='legacy'
        db.upgrade(engine)
        with engine.connect() as c: assert c.execute(text('PRAGMA foreign_keys')).scalar()==1
    finally:
        engine.dispose()


def test_delivery_cancellation_stops_batch_after_current_outcome(bridge_world):
    import threading
    from personal_agent.api.dal_resume import DecisionRequest
    engine,bridge,auth,p,transport=bridge_world
    first=bridge.decide(auth,click(p))
    second=bridge.decide(auth,DecisionRequest(**{**click(p).model_dump(),'request_id':'second-click'}))
    stop=threading.Event()
    transport.lose_response=False
    original=transport.deliver
    def deliver(assertion):
        result=original(assertion)
        stop.set()
        return result
    transport.deliver=deliver
    bridge.deliver_pending(stop_event=stop)
    assert transport.sent==1
    with engine.connect() as c:
        rows=c.execute(text('SELECT status,attempts FROM dal_resume_deliveries ORDER BY status')).all()
        assert rows==[('accepted',1),('queued',0)]
