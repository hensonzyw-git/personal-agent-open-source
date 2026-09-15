#!/usr/bin/env python3
"""Credential-free fixed-child runtime/replacement preflight. No real CLI mode.

Usage: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python
       scripts/dal_runtime_synthetic_preflight.py --output-root /private/tmp/dal-fixture
The output root must not exist. All DBs, generated keys and task spaces are
synthetic. Keys stay in memory. No executable, script, config or provider inputs.
"""
from pathlib import Path
import argparse
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def _child(database, root, boot, lease, attempt):
    from personal_agent_dal.storage.engine import create_database_engine
    from personal_agent_dal.worker.transport import LocalSQLiteAdapter
    from personal_agent_dal.worker.supervisor import Supervisor
    from personal_agent_dal.worker.trusted_runtime import execute_runtime
    engine = create_database_engine(database)
    transport = LocalSQLiteAdapter(engine, worker_id='w', lease_ttl_seconds=720,
        max_attempts=1, checkpoint_root=root/'checkpoints')
    execute_runtime(transport, lease, supervisor=Supervisor(root/'supervisor',boot_id=boot,epoch=1), attempt=attempt)


def run_synthetic(root, *, boot='synthetic-boot'):
    """Run real local authority functions with disposable fixed fixture data."""
    from concurrent.futures import ThreadPoolExecutor
    from cryptography.hazmat.primitives.asymmetric import ec
    from sqlalchemy import select, func
    from personal_agent.auth.device_keys import encode_device_public_key
    from personal_agent.api.dal_client import sign_decision
    from personal_agent_core.timeutil import utc_now
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_database_engine, session_factory
    from personal_agent_dal.storage.models import Feature
    from personal_agent_dal.storage.machine_models import ProviderAttempt, DispatchIntent, ReplacementBudget, ExecutionResultEnvelope
    from personal_agent_dal.storage.worker_models import WorkerResultReceipt
    from personal_agent_dal.storage.transport_models import WorkerEnrollment
    from personal_agent_dal.machine.workflow_selection import register_profile, select_workflow, SelectionRequest
    from personal_agent_dal.machine.execution_start import prepare_execution, start_execution, PrepareExecutionRequest, StartExecutionRequest
    from personal_agent_dal.machine.isolation_evidence import register_supervisor, issue_challenge, import_evidence
    from personal_agent_dal.machine.action_lifecycle import pause_execution
    from personal_agent_dal.machine.action_recovery import recover_attempt
    from personal_agent_dal.machine.resume_authority import propose, import_decision, resume, ResumeRequest
    from personal_agent_dal.machine.resume_dispatch import consume_intent
    from personal_agent_dal.worker.supervisor import Supervisor, SupervisorRefusal, _digest
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime, execute_runtime, reconcile_runtime, prepare_runtime_isolation
    from personal_agent_dal.worker.transport import LocalSQLiteAdapter
    root = Path(root).resolve()
    root.mkdir(mode=0o700)  # refuse reuse, never open a caller's existing DB
    database = root/'synthetic.sqlite3'
    engine = create_database_engine(database)
    db.upgrade(engine)
    now = utc_now()
    roles = {r:dict(runtime='codex_cli',provider='openai',model=m,reasoning=q,
        placement='home_mac',permission=p,billing='subscription') for r,m,q,p in [
        ('planner','gpt-6-astra','medium','read_only'),('coder','gpt-5.6-sol','high','workspace_write'),
        ('reviewer','gpt-6-astra','medium','read_only')]}
    with session_factory(engine)() as s, s.begin():
        s.add(Feature(feature_id='f',schema_version='dal.feature-state/1.0',version=1,state='coding',
            repository_id='repo',base_sha='0'*40,decision_frontier_version=1,policy_version='dal-policy/1.0',
            capability_epoch=1,external_effect_inventory_sha256=hashlib.sha256(b'').hexdigest(),
            artifact_sha256=hashlib.sha256(b'synthetic artifact').hexdigest(),trace_id='synthetic',created_at=now,updated_at=now))
        s.add(WorkerEnrollment(worker_id='w',machine_id='synthetic',capabilities='[]',created_at=now))
    register_profile(engine,revision_id='B-1',profile='B',revision=1,roles=roles)
    inp={'schema':'dal.execution-input/1.0','feature_id':'f','task_source':{'kind':'operator'},
        'task_description':'Fixed synthetic task','task_description_sha256':hashlib.sha256(b'Fixed synthetic task').hexdigest(),
        'repository_id':'repo','base_sha':'0'*40,'branch_name':'task','toolchain_ref':'test','toolchain_manifest_sha256':'a'*64}
    selection = prepare_execution(engine,feature_id='f',actor='synthetic-operator',body=PrepareExecutionRequest(
        request_id='select',action_key='task',execution_role='coder',profile_revision_id='B-1',
        expected_feature_version=1,expected_gate_version=0,execution_input=inp))
    first = start_execution(engine,feature_id='f',actor='synthetic-operator',body=StartExecutionRequest(
        request_id='start',action_id=selection['action_id'],selection_id=selection['selection_id'],
        expected_feature_version=1,expected_gate_version=1,expected_action_version=1,
        confirmed_execution_sha256=selection['confirmed_execution_sha256'],expires_at=int(time.time())+720))
    key = ec.generate_private_key(ec.SECP256R1())
    register_supervisor(engine,kid='k',worker_id='w',machine_id='synthetic',
        public_key=encode_device_public_key(key.public_key()),boot_id=boot,supervisor_epoch=1)
    identity=dict(kid='k',worker_id='w',machine_id='synthetic',registration_epoch=1,boot_id=boot,supervisor_epoch=1)
    t = LocalSQLiteAdapter(engine,worker_id='w',lease_ttl_seconds=720,max_attempts=1,checkpoint_root=root/'checkpoints')
    l=t.claim();c=t.prelaunch_context(l)
    supervisor=Supervisor(root/'supervisor',boot_id=boot,epoch=1)
    kwargs=dict(supervisor=supervisor,identity=identity,key=key,pins=[],read_roots=[],fixture={'mode':'timeout'})
    initial=prepare_runtime(t,l,c,**kwargs)
    # Interrupt the owning supervisor after actual child identity registration.
    engine.dispose()
    owner=multiprocessing.get_context('spawn').Process(target=_child,args=(database,root,boot,l,c['attempt_id']))
    owner.start()
    inv=RuntimeInventory(supervisor)
    try:
        end=time.monotonic()+15
        while time.monotonic()<end:
            row=inv.get(c['attempt_id'])
            if row['state']=='running':break
            if not owner.is_alive():raise RuntimeError('SYNTHETIC_OWNER_EXITED_BEFORE_REGISTRATION')
            time.sleep(.02)
        else:raise RuntimeError('SYNTHETIC_REGISTRATION_TIMEOUT')
        concurrent=reconcile_runtime(supervisor,t)
        assert concurrent['observations'][0]['code']=='RUNTIME_OWNER_ACTIVE'
        owner.terminate();owner.join(5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _:reconcile_runtime(Supervisor(root/'supervisor',boot_id=boot,epoch=1),t),range(2)))
        stopped=inv.get(c['attempt_id'])
        assert stopped['state']=='unknown' and stopped['observation']['reconciliation_stop']['process_exited']
        assert pause_execution(engine,feature_id='f',expected_gate_version=2).code=='PAUSED'
        status=t.execution_status(l)
        recovery=recover_attempt(engine,attempt_id=c['attempt_id'],expected_version=status['attempt_version'],
            command_id='recover',requested_by='synthetic-operator')
        assert recovery.code=='ATTEMPT_UNKNOWN'
        challenge=issue_challenge(engine,attempt_id=c['attempt_id'])
        proof=prepare_runtime_isolation(supervisor,challenge,key=key,read_roots=[])
        original=proof['reservation'];original_digest=_digest(original)
        import_evidence(engine,assertion=proof['assertion'],worker_id='w')
        selection2=select_workflow(engine,feature_id='f',actor='synthetic-operator',body=SelectionRequest(
            request_id='replacement-select',profile_revision_id='B-1',expected_feature_version=1,expected_gate_version=3))
        proposal=propose(engine,feature_id='f',selection_id=selection2['selection_id'],request_id='proposal')
        user_key=ec.generate_private_key(ec.SECP256R1());stamp=int(time.time())
        claims=dict(iss='synthetic-pa',aud='synthetic-dal',jti='jti',iat=stamp,exp=stamp+600,
            decision_id='decision',device_id='fixture-phone',subject_id='device:fixture-phone',key_thumbprint='a'*43,
            decision='approve_once_accept_duplicate_cost',proposal_id=proposal['proposal_id'],binding_sha256=proposal['binding_sha256'])
        approval=import_decision(engine,assertion=sign_decision(claims,key=user_key,kid='resume'),
            keys={'resume':user_key.public_key()},issuer='synthetic-pa',audience='synthetic-dal')
        def contend(request_id):
            try:return resume(engine,feature_id='f',body=ResumeRequest(request_id=request_id,approval_id=approval['approval_id']))
            except ValueError as exc:return {'refused':str(exc)}
        with ThreadPoolExecutor(max_workers=2) as pool: race=list(pool.map(contend,['resume-a','resume-b']))
        winners=[r for r in race if 'new_attempt_id' in r]
        assert len(winners)==1
        replacement=winners[0]
        with session_factory(engine)() as s:
            intent=s.scalar(select(DispatchIntent.intent_id))
            budget=s.get(ReplacementBudget,c['attempt_id'])
            assert budget.new_attempt_id==replacement['new_attempt_id']
        job=consume_intent(engine,intent_id=intent)
        lease=t.claim();assert lease.job_id==job
        context=t.prelaunch_context(lease)
        assert context['execution_input']==c['execution_input']
        kwargs['fixture']={}
        adopted=prepare_runtime(t,lease,context,**kwargs)
        assert adopted['reservation_id']==original['reservation_id']
        assert supervisor.validate(original['reservation_id'])==original
        assert _digest(supervisor.validate(original['reservation_id']))==original_digest
        # Old dispatch output is observed, never authorized against replacement.
        from personal_agent_dal.machine.execution_results import canonical_result
        old_status=t.execution_status(l)
        body={k:c[k] for k in ('feature_id','action_id','attempt_id','job_id','worker_id','job_lease_epoch','lease_id','policy_lease_epoch','snapshot_sha256','execution_role')}
        body.update(schema='dal.execution-result/1.0',request_id='late-old',attempt_version=2,fence=1,
            execution_spec_sha256=c['execution_spec_sha256'],manifest_sha256=initial['observation']['manifest_sha256'],
            outcome='succeeded',reason=None,started_at=stamp,ended_at=stamp,stop={'requested':False,'forced':False,'process_exited':True},
            report='Synthetic late old output',cli_exit_code=0,tool_events=[],tests=[],git_evidence=[],artifacts=[],
            usage={'input_tokens':None,'output_tokens':None,'provider_requests':None},unverified=[],truncated=False,redacted=False)
        body,digest=canonical_result(body)
        late=t.submit_execution_result(l,{'schema':'dal.worker-execution-transport/1.0','result':body,'result_sha256':digest})
        assert not late['accepted']
        with session_factory(engine)() as s:assert s.get(ProviderAttempt,replacement['new_attempt_id']).state=='prepared'
        result=execute_runtime(t,lease,supervisor=supervisor,attempt=context['attempt_id'])
        assert result['accepted'] and result['job_state']=='succeeded'
        reopened=Supervisor(root/'supervisor',boot_id=boot,epoch=1)
        assert execute_runtime(t,lease,supervisor=reopened,attempt=context['attempt_id'])==result
        try:execute_runtime(t,l,supervisor=reopened,attempt=c['attempt_id'])
        except SupervisorRefusal as exc:assert str(exc)=='EXECUTION_EFFECTS_UNKNOWN'
        else:raise AssertionError('old attempt replayed')
        with session_factory(engine)() as s:
            assert s.scalar(select(func.count()).select_from(ReplacementBudget))==1
            assert s.scalar(select(func.count()).select_from(WorkerResultReceipt))==1
            assert s.scalar(select(func.count()).select_from(ExecutionResultEnvelope))==2
        final=inv.get(context['attempt_id'])
        return dict(production_enabled=False,boot_id=boot,root=str(root),database=str(database),
            original_new=original,original_new_sha256=original_digest,initial_attempt=c['attempt_id'],
            replacement_attempt=context['attempt_id'],receipt=result,stop=proof['stop'],
            plan=final['observation']['plan'],race=race,manifest_version='dal.launch-manifest/1.1',
            checks=['initial authority','actual fixed child','active owner preserved','owner-loss bounded stop',
                'signed challenge','signed synthetic user approval','losing resume race','original NEW adoption',
                'old result fenced','atomic result and receipt','restart no replay','one replacement budget'])
    finally:
        if owner.is_alive():owner.terminate();owner.join(5)
        reconcile_runtime(supervisor)
        engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root',type=Path,required=True)
    args=parser.parse_args()
    root=args.output_root.resolve()
    # Code identity is HEAD plus actual source digests; dirty code isn't mislabeled
    # as a reviewed commit. Read only this entrypoint and named runtime sources.
    repo=Path(__file__).resolve().parents[1]
    code=subprocess.run(['/usr/bin/git','-C',str(repo),'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
    from personal_agent_dal.worker.runtime_process import os_boot_id
    boot_error=None
    try:boot=os_boot_id()
    except Exception:boot='synthetic-boot';boot_error='HOST_BOOT_LOOKUP_BLOCKED_REQUIRES_MAIN_HOST_RERUN'
    if root.exists(): parser.error('output root must not exist')
    try:
        result=run_synthetic(root,boot=boot)
    except Exception as exc:
        if root.is_dir():
            with (root/'failure-evidence.json').open('x') as f:
                json.dump(dict(production_enabled=False,code_sha=code,boot_id=boot,
                    boot_verification_error=boot_error,failure_type=type(exc).__name__,
                    status='LOCAL_SYNTHETIC_FAILED',root=str(root)),f,indent=2)
                f.flush();os.fsync(f.fileno())
        raise
    paths=[Path(__file__), *sorted((repo/'src/personal_agent_dal').rglob('*.py'))]
    result.update(code_sha=code,source_sha256={str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        boot_verification_error=boot_error,live_boundary='Real CLI/provider admission refused: separately authorize pinned CLI help/version and sandbox/auth-route probes, then a bounded provider call; no production Worker configuration changed.')
    output=root/'evidence.json'
    with output.open('x') as f:
        json.dump(result,f,indent=2);f.flush();os.fsync(f.fileno())
    print(json.dumps({'evidence':str(output),'production_enabled':False,'checks':len(result['checks']),'boot_verification_error':boot_error}))
    return 0

if __name__=='__main__':raise SystemExit(main())
