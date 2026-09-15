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
    mapped=resolve_snapshot(context['snapshot'],context['snapshot_sha256'],body['runtime_pins'])
    supervisor=Supervisor(Path(body['root']),boot_id=body['boot_id'],epoch=body['supervisor_epoch'])
    for pin in mapped.values():
        verify_executable({'executable':pin.executable,
            'executable_sha256':pin.executable_sha256,'version':pin.version})
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
    r,sha=prepare(transport,lease,context,supervisor=supervisor,pins=body['runtime_pins'],
        read_roots=body['read_roots'],identity=body['identity'],key=key,
        repository={'source':config.repos[lease.repository_id].local_path,'base_sha':lease.base_sha,'git_pin':body['git_pin']})
    # The inventory is acknowledged, but the provider dispatch marker is not
    # consumed merely to report an unmet machine prerequisite.
    supervisor.launch(r['reservation_id'])
