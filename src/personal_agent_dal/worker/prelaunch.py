"""Connected Worker prelaunch. No credential-bearing provider implementation."""
import json
from pathlib import Path
import stat
import time

from personal_agent_dal.worker.runtime_mapping import resolve_snapshot
from personal_agent_dal.worker.supervisor import Supervisor, SupervisorRefusal, signed_manifest, verify_executable


CONFIG_FIELDS={'root','boot_id','supervisor_epoch','identity','read_roots','runtime_pins','signing_key_path','git_pin'}


def load_supervisor_config(path):
    body=json.loads(Path(path).read_text())
    if not isinstance(body,dict) or set(body)!=CONFIG_FIELDS:
        raise SupervisorRefusal('SUPERVISOR_CONFIG_INVALID')
    if set(body['identity'])!={'kid','worker_id','machine_id','registration_epoch','boot_id','supervisor_epoch'}:
        raise SupervisorRefusal('SUPERVISOR_IDENTITY_INVALID')
    for name in ('root','signing_key_path'):
        if not Path(body[name]).is_absolute():raise SupervisorRefusal('ABSOLUTE_PATH_REQUIRED')
    if not isinstance(body['runtime_pins'],list) or not isinstance(body['read_roots'],list):
        raise SupervisorRefusal('SUPERVISOR_CONFIG_INVALID')
    return body


def validate_transport_identity(identity, *, worker_id, machine_id):
    fields = {'kid','worker_id','machine_id','registration_epoch','boot_id','supervisor_epoch'}
    if not isinstance(identity, dict) or set(identity) != fields:
        raise SupervisorRefusal('SUPERVISOR_IDENTITY_INVALID')
    if any(type(identity[k]) is not int or identity[k] < 1 for k in ('registration_epoch','supervisor_epoch')):
        raise SupervisorRefusal('SUPERVISOR_IDENTITY_INVALID')
    if any(not isinstance(identity[k], str) or not identity[k] for k in fields - {'registration_epoch','supervisor_epoch'}):
        raise SupervisorRefusal('SUPERVISOR_IDENTITY_INVALID')
    if (identity['worker_id'],identity['machine_id']) != (worker_id,machine_id):
        raise SupervisorRefusal('SIGNER_IDENTITY_STALE')


def transport_identity(config):
    """Load public expected identity from the existing production configuration.

    No signing key, provider credential or runtime admission is read here.
    """
    if config.supervisor_config_path is None:
        if config.schema_version in ('dal.worker-config/2.0','dal.worker-config/2.1'):
            raise SupervisorRefusal('SUPERVISOR_CONFIG_REQUIRED')
        return None
    body = load_supervisor_config(config.supervisor_config_path)
    identity = body['identity']
    validate_transport_identity(identity, worker_id=config.worker_id, machine_id=config.transport.machine_id)
    if (identity['boot_id'],identity['supervisor_epoch']) != (body['boot_id'],body['supervisor_epoch']):
        raise SupervisorRefusal('SIGNER_IDENTITY_STALE')
    return dict(identity)


def prepare(transport,lease,context,*,supervisor,pins,read_roots,identity,key,repository=None):
    """Reserve and acknowledge the concrete inventory, without consuming dispatch."""
    if context['job_id']!=lease.job_id or context['job_lease_epoch']!=lease.lease_epoch:
        raise SupervisorRefusal('CLAIM_CONTEXT_MISMATCH')
    if context['worker_id']!=identity['worker_id']:raise SupervisorRefusal('WORKER_IDENTITY_MISMATCH')
    mapped=resolve_snapshot(context['snapshot'],context['snapshot_sha256'],pins)
    isolation=context['isolation']
    if isolation:
        # An imported proof must name an existing supervisor-owned reservation.
        # Never allocate fresh space to retroactively substantiate that proof.
        raise SupervisorRefusal('ISOLATION_RESERVATION_REBINDING_REQUIRES_MINI_PROOF')
    authority={k:context[k] for k in ('intent_id','attempt_id','attempt_version','feature_id','action_id',
        'job_id','worker_id','job_lease_epoch','lease_id','policy_lease_epoch','snapshot_sha256','selection_id')}
    # All roles are inventoried; no ad-hoc interpretation of action/Stage as coder.
    authority['runtime_pins']={role:pin.model_dump() for role,pin in mapped.items()}
    authority['isolation_id']=isolation['isolation_id'] if isolation else None
    r=supervisor.reserve(attempt_id=context['attempt_id'],
        workspace_id=isolation['new_workspace_id'] if isolation else context['attempt_id'],
        generation=isolation['workspace_generation'] if isolation else 1,authority=authority,read_roots=read_roots)
    if repository is not None:
        from personal_agent_dal.worker.supervisor import provision_repository
        r=provision_repository(supervisor,r['reservation_id'],**repository)
    assertion,sha=signed_manifest(supervisor,r,identity=identity,key=key,now=int(time.time()))
    acknowledgement=transport.acknowledge_prelaunch(lease,assertion)
    if acknowledgement!={'manifest_sha256':sha}:
        raise SupervisorRefusal('MANIFEST_ACKNOWLEDGEMENT_REQUIRED')
    return r,sha


def worker_prelaunch(transport,config,lease,context):
    """Production composition records prelaunch; CLI processes require mini proof.

    Inventory reservation is available via the same prepare() implementation in
    the synthetic preflight. No config flag can enable provider launch here.
    """
    if config.supervisor_config_path is None:raise SupervisorRefusal('SUPERVISOR_CONFIG_REQUIRED')
    body=load_supervisor_config(config.supervisor_config_path)
    from personal_agent_dal.worker.supervisor import current_boot_id
    if body['boot_id']!=current_boot_id():raise SupervisorRefusal('BOOT_ID_MISMATCH')
    if (body['identity']['boot_id'],body['identity']['supervisor_epoch'],body['identity']['worker_id']) != (body['boot_id'],body['supervisor_epoch'],config.worker_id):
        raise SupervisorRefusal('SIGNER_IDENTITY_STALE')
    mapped=resolve_snapshot(context['snapshot'],context['snapshot_sha256'],body['runtime_pins'])
    supervisor=Supervisor(Path(body['root']),boot_id=body['boot_id'],epoch=body['supervisor_epoch'])
    for pin in mapped.values():
        verify_executable({'executable':pin.executable,
            'executable_sha256':pin.executable_sha256,'version':pin.version})
    if getattr(config,'schema_version',None) not in ('dal.worker-config/2.0','dal.worker-config/2.1'):raise SupervisorRefusal('WORKER_CONFIG_V2_REQUIRED')
    from personal_agent_dal.worker.role_adapter import load_adapter_config, build_plan
    adapters = load_adapter_config(config.adapter_config_ref)
    # Pure preparation uses declared task paths; it creates no reservation or
    # signing material and cannot run a CLI.
    root = Path(body['root']) / ('workspace-'+context['attempt_id'])
    plan = build_plan(context, {'workspace':str(root/'work'),'temp':str(root/'tmp'),
        'git':str(root/'git'),'read_roots':body['read_roots']}, body['runtime_pins'], adapters)
    from personal_agent_dal.worker.supervisor import require_machine_acceptance
    if config.schema_version != 'dal.worker-config/2.1' or not config.admission_ref:
        require_machine_acceptance()
    from personal_agent_dal.worker.runtime_admission import private_json
    from personal_agent_dal.worker.supervisor import _digest
    admission = dict(path=str(config.admission_ref),identity=body['identity'],
        pins=body['runtime_pins'],adapters=adapters,
        config_refs={str(p):_digest(private_json(p)) for p in
            (config.config_ref,config.supervisor_config_path,config.adapter_config_ref)})
    from personal_agent_dal.worker.reviewer_route import SCHEMA as ROUTE_ADAPTER_SCHEMA
    if adapters['schema'] == ROUTE_ADAPTER_SCHEMA:
        ref = adapters['roles']['reviewer']['config_ref']
        admission['config_refs'][ref['path']] = _digest(private_json(ref['path']))
    declared = {'workspace':str(root/'work'),'temp':str(root/'tmp'),
        'git':str(root/'git'),'read_roots':body['read_roots']}
    require_machine_acceptance(admission,context=context,reservation=declared,plan=plan)
    # Signing material is read only when the Worker is explicitly run with this
    # configuration, never by inventory or offline tests.
    key_path=Path(body['signing_key_path'])
    if key_path.resolve()!=key_path:raise SupervisorRefusal('SIGNING_KEY_SYMLINK_PATH')
    for raw in body['read_roots']:
        if key_path.is_relative_to(Path(raw)):
            raise SupervisorRefusal('SIGNING_KEY_CHILD_READABLE')
    import os
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    fd=os.open(key_path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode & 0o077 or st.st_nlink!=1:
            raise SupervisorRefusal('SIGNING_KEY_NOT_PRIVATE')
        with os.fdopen(fd,'rb',closefd=False) as stream:
            key=load_pem_private_key(stream.read(),password=None)
    finally:os.close(fd)
    from personal_agent_dal.worker.trusted_runtime import prepare_runtime, execute_runtime
    prepare_runtime(transport,lease,context,supervisor=supervisor,pins=body['runtime_pins'],
        read_roots=body['read_roots'],identity=body['identity'],key=key,adapter_config=adapters,admission=admission,
        repository={'source':config.repos[lease.repository_id].local_path,'base_sha':lease.base_sha,'git_pin':body['git_pin']})
    return execute_runtime(transport,lease,supervisor=supervisor,attempt=context['attempt_id'],
        kill_switch=config.kill_switch_path.exists)


def worker_reconcile(transport, config):
    """Public local inventory reconciliation precedes claim, including idle polls."""
    if getattr(config, 'schema_version', None) not in ('dal.worker-config/2.0','dal.worker-config/2.1'): return None
    body = load_supervisor_config(config.supervisor_config_path)
    from personal_agent_dal.worker.supervisor import current_boot_id
    from personal_agent_dal.worker.trusted_runtime import reconcile_runtime
    supervisor = Supervisor(Path(body['root']), boot_id=current_boot_id(), epoch=body['supervisor_epoch'])
    # Persist a cursor so inventories larger than one page cannot starve.
    from personal_agent_dal.worker.runtime_inventory import RuntimeInventory
    RuntimeInventory(supervisor)
    with supervisor._lock(), supervisor._db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS runtime_reconcile_cursor (id INTEGER PRIMARY KEY, cursor TEXT NOT NULL)')
        row = db.execute('SELECT cursor FROM runtime_reconcile_cursor WHERE id=1').fetchone()
    result = reconcile_runtime(supervisor, transport, after=row[0] if row else '')
    with supervisor._lock(), supervisor._db() as db:
        db.execute('INSERT OR REPLACE INTO runtime_reconcile_cursor VALUES (1,?)', (result['next_cursor'] or '',))
    return result
