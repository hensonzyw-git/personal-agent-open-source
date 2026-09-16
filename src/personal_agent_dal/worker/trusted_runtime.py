"""Shared provider-v1 orchestration; synthetic success grants no live acceptance."""
from dataclasses import asdict, replace
import time
import base64
import json
from personal_agent_core.manifest import canonical_json
from personal_agent_dal.machine.execution_protocol import validate_execution_context
from personal_agent_dal.machine.execution_manifest import sign_execution_manifest
from personal_agent_dal.machine.execution_results import canonical_result
from personal_agent_dal.worker.supervisor import SupervisorRefusal, _digest, provision_repository, require_machine_acceptance
from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
from personal_agent_dal.worker.role_adapter import build_plan, parse_events
from personal_agent_dal.worker.runtime_process import fixture_plan, run_process
from personal_agent_dal.worker.runtime_admission import applied_policy


def _prepare_runtime(transport, lease, context, *, supervisor, identity, key, pins, read_roots, repository=None, fixture=None, adapter_config=None, admission=None):
    context=validate_execution_context(context)
    if fixture is None and admission is None: require_machine_acceptance()  # No real signing/provision/auth before admission.
    if (context['job_id'],context['job_lease_epoch'],context['worker_id'])!=(lease.job_id,lease.lease_epoch,identity['worker_id']):
        raise SupervisorRefusal('CLAIM_CONTEXT_MISMATCH')
    if (identity['boot_id'],identity['supervisor_epoch'])!=(supervisor.boot_id,supervisor.epoch):
        raise SupervisorRefusal('SIGNER_IDENTITY_STALE')
    inv=RuntimeInventory(supervisor)
    prior=inv.get(context['attempt_id'])
    if prior:
        if prior['binding']!=context:raise SupervisorRefusal('RUNTIME_BINDING_CONFLICT')
        if prior['state']!='prepared' or prior['observation']:return prior
        raise SupervisorRefusal('PREPARATION_INTERRUPTED_REQUIRES_REVIEW')
    iso=context['isolation']
    if iso:
        # Locate exactly the original directories. No replacement NEW allocation.
        reservation=supervisor.validate(iso['new_reservation_id'])
    else:
        reservation=supervisor.reserve(attempt_id=context['attempt_id'],workspace_id=context['attempt_id'],
            generation=1,authority={'execution_spec_sha256':context['execution_spec_sha256']},read_roots=read_roots)
    inv.adopt(context,reservation)
    if repository:provision_repository(supervisor,reservation['reservation_id'],**repository)
    plan=fixture_plan(reservation,**fixture) if fixture is not None else build_plan(context,reservation,pins,adapter_config)
    if fixture is None:
        sha = require_machine_acceptance(admission,context=context,reservation=reservation,plan=plan)
        plan = replace(plan,admission=admission,admission_sha256=sha,production_enabled=True)
    from pathlib import Path
    for directory in plan.task_directories.values():
        Path(directory).mkdir(mode=0o700,exist_ok=True)
    supervisor.validate(reservation['reservation_id'])
    now=int(time.time()); spec=context['execution_spec']
    if spec['policy_expires_at']-now < plan.wall_seconds+10 or lease.deadline.timestamp()-now < plan.wall_seconds+10:
        inv.transition(context['attempt_id'],'prepared','refused')
        raise SupervisorRefusal('EXECUTION_BUDGET_EXCEEDS_AUTHORITY')
    payload=dict(schema='dal.launch-manifest/1.1',**identity,attempt_id=context['attempt_id'],
        workspace_id=reservation['workspace_id'],workspace_generation=reservation['generation'],
        isolation_policy_sha256=applied_policy(plan),inventory_sha256=_digest(reservation),
        reservation_id=reservation['reservation_id'],job_id=lease.job_id,job_lease_epoch=lease.lease_epoch,
        lease_id=context['lease_id'],policy_lease_epoch=context['policy_lease_epoch'],issued_at=now,
        expires_at=min(now+900,spec['policy_expires_at']),execution_spec=spec,
        execution_spec_sha256=context['execution_spec_sha256'],launcher_plan_sha256=_digest(asdict(plan)),
        source_reservation_sha256=_digest(reservation),isolation_id=iso['isolation_id'] if iso else None,
        isolation_binding_sha256=_digest(iso) if iso else None)
    if fixture is None:
        require_machine_acceptance(admission,context=context,reservation=reservation,plan=plan,expected_digest=plan.admission_sha256)
    assertion,sha=sign_execution_manifest(payload,key=key)
    # Store signed acknowledgement request before transport (stable replay bytes).
    with supervisor._lock(),supervisor._db() as db:
        row=inv._get(db,context['attempt_id'])
        if row['state']!='prepared':raise SupervisorRefusal('RUNTIME_CAS_LOST')
        obs=dict(plan=asdict(plan),assertion=assertion,manifest_sha256=sha,production_enabled=plan.production_enabled)
        if row['observation'] and row['observation']!=obs:raise SupervisorRefusal('PREPARATION_CONFLICT')
        db.execute('UPDATE runtime_inventory SET observation=? WHERE effective_attempt=?',(canonical_json(obs),context['attempt_id']))
    return inv.get(context['attempt_id'])


def _execute_runtime(transport, lease, *, supervisor, attempt, kill_switch=lambda:False):
    from personal_agent_dal.worker.role_adapter import LaunchPlan
    inv=RuntimeInventory(supervisor);row=inv.get(attempt)
    if not row:raise SupervisorRefusal('RUNTIME_PREPARATION_REQUIRED')
    if row['state']=='reported':return row['response']
    if row['state']=='result_ready':
        response=transport.submit_execution_result(lease,row['result'])
        if response['accepted']:inv.transition(attempt,'result_ready','reported',response=response)
        return response
    if row['state'] in ('dispatch_requested','granted','starting','running','unknown'):
        _reconcile_owned(inv, row, transport, lease)
        raise SupervisorRefusal('EXECUTION_EFFECTS_UNKNOWN')
    if row['state']!='prepared':raise SupervisorRefusal('RUNTIME_REFUSED')
    obs=row['observation'];raw_plan=dict(obs['plan']);raw_plan['argv']=tuple(raw_plan['argv']);raw_plan['read_roots']=tuple(raw_plan.get('read_roots',()));raw_plan['write_roots']=tuple(raw_plan.get('write_roots',()));plan=LaunchPlan(**raw_plan)
    # No dispatch consumption or credential read for an unaccepted real runtime.
    manifest = json.loads(base64.urlsafe_b64decode(obs['assertion'].split('.')[1]+'=='))
    if _digest(manifest) != obs['manifest_sha256'] or _digest(asdict(plan)) != manifest['launcher_plan_sha256']:
        raise SupervisorRefusal('LAUNCH_PLAN_DIGEST_MISMATCH')
    from personal_agent_dal.worker.runtime_admission import revalidate_plan
    if plan.runtime!='synthetic_fixture':revalidate_plan(plan,inv,attempt)
    if kill_switch():raise SupervisorRefusal('KILL_SWITCH_ACTIVE')
    sha=obs['manifest_sha256']
    if transport.acknowledge_prelaunch(lease,obs['assertion'])!={'manifest_sha256':sha}:
        raise SupervisorRefusal('MANIFEST_ACKNOWLEDGEMENT_REQUIRED')
    if plan.runtime!='synthetic_fixture':revalidate_plan(plan,inv,attempt)
    inv.transition(attempt,'prepared','dispatch_requested')
    try:grant=transport.dispatch_prelaunch(lease,sha)
    except Exception:
        inv.transition(attempt,'dispatch_requested','unknown')
        raise SupervisorRefusal('DISPATCH_RESPONSE_UNKNOWN') from None
    if grant!={'code':'DISPATCH_GRANTED'}:
        inv.transition(attempt,'dispatch_requested','refused');raise SupervisorRefusal('DISPATCH_NOT_GRANTED')
    inv.transition(attempt,'dispatch_requested','granted')
    status=transport.execution_status(lease)
    context=row['binding'];spec=context['execution_spec']
    def heartbeat():
        if kill_switch():return False
        if plan.runtime!='synthetic_fixture':revalidate_plan(plan,inv,attempt)
        hb=transport.heartbeat(lease)
        current=transport.execution_status(lease)
        return (hb.alive and not hb.cancel_requested and not current['stop_required'] and
            current['attempt_id']==attempt and current['manifest_sha256']==sha and
            current['fence']==spec['expected_dispatch_fence'] and current['attempt_version']==status['attempt_version'])
    if status['attempt_id']!=attempt or status['manifest_sha256']!=sha or status['fence']!=spec['expected_dispatch_fence']:
        inv.transition(attempt,'granted','unknown');raise SupervisorRefusal('DISPATCH_STATUS_MISMATCH')
    prompt=("Business source: "+supervisor.validate(row['reservation_id'])['workspace']+"\n"+
        "Read-only roles must preserve business source; use scratch for reports, cache and test copies.\n"+
        context['execution_input']['task_description']).encode()
    try:process=run_process(inv,attempt,plan,heartbeat=heartbeat,deadline=spec['policy_expires_at'],prompt=prompt)
    except Exception:
        state=inv.get(attempt)['state']
        if state in ('granted','starting','running'):inv.transition(attempt,state,'unknown')
        raise
    try:
        parsed=parse_events(process['raw'],'codex_cli' if plan.runtime=='synthetic_fixture' else plan.runtime)
    except SupervisorRefusal as exc:
        parsed=dict(report='',tool_events=[],usage=dict(input_tokens=None,output_tokens=None,provider_requests=None),outcome='failed',reason=str(exc))
    if process['reason'] or process['exit_code']!=0:parsed.update(outcome='failed',reason=process['reason'] or 'CLI_EXIT_FAILED')
    if not process['stop']['process_exited']:parsed.update(outcome='unknown',reason='PROCESS_GROUP_STOP_UNPROVEN')
    from personal_agent_dal.worker.runtime_evidence import collect_evidence
    reservation=supervisor.validate(row['reservation_id'])
    evidence=collect_evidence(plan,reservation,process)
    if process['stop']['process_exited']:
        with supervisor._db() as db:
            provisioning=db.execute('SELECT body FROM runtime_provisioning WHERE reservation_id=?',(row['reservation_id'],)).fetchone()
        if provisioning:
            from personal_agent_dal.worker.runtime_evidence import bounded_git_patch
            patch=bounded_git_patch(plan,reservation,json.loads(provisioning[0])['binding']['git_pin'])
            if patch:evidence['git_evidence'].append(patch)
    body={k:context[k] for k in ('feature_id','action_id','attempt_id','job_id','worker_id','job_lease_epoch','lease_id','policy_lease_epoch','snapshot_sha256','execution_role')}
    body.update(schema='dal.execution-result/1.0',request_id='runtime-'+attempt,attempt_version=status['attempt_version'],
        fence=status['fence'],execution_spec_sha256=context['execution_spec_sha256'],manifest_sha256=sha,
        **parsed,started_at=process['started_at'],ended_at=process['ended_at'],stop=process['stop'],cli_exit_code=process['exit_code'],
        **evidence,unverified=[*(['synthetic fixture; production_enabled=false'] if not plan.production_enabled else []),'Provider requests and cost not independently verified','Tests are unverified unless source evidence is listed'],
        truncated=process['truncated'],redacted=False)
    # Scan diagnostic strings before persisting; retain bounded evidence without
    # retaining raw credential-bearing streams. Usage is observable, not inferred.
    from personal_agent_dal.machine.execution_results import _SECRET
    stderr = _SECRET.sub('[REDACTED]', process['stderr'].decode('utf-8', errors='replace'))
    body['redacted'] = stderr != process['stderr'].decode('utf-8', errors='replace')
    body['unverified'].extend(['Observable event steps: '+str(process['event_count']),
        'stdout sha256: '+__import__('hashlib').sha256(process['raw']).hexdigest(),
        'stdout bytes retained: '+str(len(process['raw'])),
        'stderr (bounded): '+stderr[:4096]])
    try:
        body,digest=canonical_result(body)
        if len(canonical_json(body).encode()) > 250*1024: raise ValueError('RESULT_TOO_LARGE')
    except ValueError:
        # JSON escaping/UTF-8 expansion can exceed the envelope even when a CLI
        # report is under its character limit. Persist a failed bounded envelope.
        body.update(report='',tool_events=[],outcome='failed',reason='CLI_RESULT_ENVELOPE_LIMIT',truncated=True)
        body,digest=canonical_result(body)
    request=dict(schema='dal.worker-execution-transport/1.0',result=body,result_sha256=digest)
    inv.transition(attempt,'running','result_ready',result=request)
    response=transport.submit_execution_result(lease,request)
    if response['accepted']:inv.transition(attempt,'result_ready','reported',response=response)
    return response


def prepare_runtime(transport, lease, context, **kwargs):
    inv = RuntimeInventory(kwargs['supervisor'])
    with inv.owner(context['attempt_id']):
        return _prepare_runtime(transport, lease, context, **kwargs)


def execute_runtime(transport, lease, *, supervisor, attempt, kill_switch=lambda: False):
    inv = RuntimeInventory(supervisor)
    with inv.owner(attempt):
        return _execute_runtime(transport, lease, supervisor=supervisor, attempt=attempt, kill_switch=kill_switch)


def _reconcile_owned(inv, row, transport=None, lease=None):
    from personal_agent_dal.worker.runtime_process import stop_registered
    attempt = row['effective_attempt']
    if row['state'] == 'result_ready':
        if transport is None: return row
        response = transport.submit_execution_result(lease, row['result'])
        if response['accepted']:
            return inv.transition(attempt, 'result_ready', 'reported', response=response)
        return row
    if row['state'] not in ('dispatch_requested', 'granted', 'starting', 'running', 'unknown'):
        return row
    observation = {}
    if row['state'] in ('dispatch_requested','granted'):
        observation['reconciliation_stop'] = dict(requested=False,forced=False,process_exited=True,reason='NO_START_INTENT')
    previous_stop = row['observation'].get('reconciliation_stop')
    if row['state'] in ('starting', 'running', 'unknown') and not (previous_stop or {}).get('process_exited'):
        refreshed = stop_registered(row['observation'], boot_id=inv.supervisor.boot_id)
        if previous_stop:
            # Preserve actual signals from earlier reconciliation attempts.
            for flag in ('requested', 'forced'):
                refreshed[flag] |= previous_stop.get(flag, False)
            if refreshed != previous_stop:
                observation['reconciliation_stop_history'] = [
                    *row['observation'].get('reconciliation_stop_history', []), previous_stop]
        observation['reconciliation_stop'] = refreshed
    if transport is not None:
        try:
            status = transport.execution_status(lease)
            if status['attempt_id'] != attempt: raise SupervisorRefusal('STATUS_IDENTITY_MISMATCH')
            observation['recovered_status'] = status
        except Exception:
            observation['recovered_status_error'] = 'STATUS_UNAVAILABLE'
    if row['state'] != 'unknown':
        return inv.transition(attempt, row['state'], 'unknown', observation=observation)
    if observation:
        with inv.supervisor._lock(), inv.supervisor._db() as db:
            obs = dict(row['observation'], **observation)
            db.execute('UPDATE runtime_inventory SET observation=?,version=version+1 WHERE effective_attempt=? AND version=?',
                       (canonical_json(obs), attempt, row['version']))
    return inv.get(attempt)


def reconcile_runtime(supervisor, transport=None, *, after='', limit=64):
    """Bounded, cursor-paged startup/poll reconciliation. Never launches a CLI."""
    from datetime import datetime, timezone
    from personal_agent_dal.worker.transport import JobLease
    inv = RuntimeInventory(supervisor)
    rows = inv.page(after=after, limit=limit)
    observations = []
    for row in rows:
        attempt = row['effective_attempt']
        try:
            with inv.owner(attempt):
                row = inv.get(attempt)  # fresh state after acquiring ownership
                c = row['binding']; spec = c['execution_spec']
                lease = JobLease(c['job_id'], c['feature_id'], spec['repository_id'], spec['base_sha'],
                    spec['branch_name'], spec['toolchain_ref'], c['job_lease_epoch'], 1,
                    datetime.fromtimestamp(spec['policy_expires_at'], timezone.utc))
                result = _reconcile_owned(inv, row, transport, lease)
                observations.append({'attempt_id': attempt, 'state': result['state']})
        except SupervisorRefusal as exc:
            observations.append({'attempt_id': attempt, 'code': str(exc)})
        except Exception:
            observations.append({'attempt_id': attempt, 'code': 'RECONCILIATION_TRANSPORT_ERROR'})
    return {'observations': observations, 'next_cursor': rows[-1]['effective_attempt'] if len(rows) == limit else None}


def prepare_runtime_isolation(supervisor, challenge, *, key, read_roots):
    """Sign a challenge only from registered stopped evidence and original NEW.

    This pure-key entrypoint also serves disposable synthetic keys. The Worker
    key loader remains behind real machine admission.
    """
    from personal_agent_dal.machine.execution_manifest import sign_execution_isolation
    inv = RuntimeInventory(supervisor)
    binding = challenge['binding']
    with inv.owner(binding['attempt_id']):
        old = inv.get(binding['attempt_id'])
        if not old or old['state'] != 'unknown': raise SupervisorRefusal('ISOLATION_UNKNOWN_REQUIRED')
        stop = old['observation'].get('reconciliation_stop', {})
        if not stop.get('process_exited'): raise SupervisorRefusal('ISOLATION_STOP_UNPROVEN')
        source = supervisor.validate(old['reservation_id'])
        if source['workspace_id'] != binding['old_workspace_id'] or old['observation']['manifest_sha256'] != binding['launch_manifest_sha256']:
            raise SupervisorRefusal('ISOLATION_SOURCE_MISMATCH')
        new = supervisor.reserve(attempt_id='new-'+binding['isolation_id'],
            workspace_id=binding['new_workspace_id'], generation=binding['workspace_generation'],
            authority={'challenge_sha256':_digest(binding)},read_roots=read_roots)
        from datetime import datetime
        now = int(time.time())
        payload = dict(binding, new_reservation_id=new['reservation_id'], new_inventory_sha256=_digest(new),
            stop_observation_sha256=_digest(stop), issued_at=now,
            expires_at=min(now+600,int(datetime.fromisoformat(challenge['expires_at']).timestamp())))
        assertion, digest = sign_execution_isolation(payload, key=key)
        return {'assertion':assertion,'sha256':digest,'reservation':new,'stop':stop}
