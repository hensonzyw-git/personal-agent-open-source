"""Owner-attested native CLI evidence, not an automatic smoke verifier.

Operators run bounded synthetic native probes *outside* production dispatch,
retain their command/result records, and attest their provenance here. Hashes
prove consistency, not that an operator told the truth. Same-UID hostile writers
are outside the approved trusted-single-user threat model. No fixture issuer or
boolean can grant admission. No auth-store contents are read by this module.
"""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from personal_agent_dal.worker.supervisor import SupervisorRefusal, _digest, _absolute

POLICY = 'trusted-single-user/1.0'
SCHEMA = 'dal.runtime-admission/1.0'


def code_identity():
    root = Path(__file__).parent
    return _digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(root.glob('*.py'))})


def private_json(path):
    path = _absolute(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_nlink != 1 or st.st_size > 1024*1024:
                raise SupervisorRefusal('ADMISSION_FILE_NOT_PRIVATE')
            with os.fdopen(fd, 'rb', closefd=False) as f:
                raw = f.read(1024*1024+1)
            if len(raw)>1024*1024: raise SupervisorRefusal('ADMISSION_SIZE_LIMIT')
            return json.loads(raw)
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        if isinstance(exc, SupervisorRefusal): raise
        raise SupervisorRefusal('ADMISSION_UNREADABLE') from None


def plan_contract(plan, reservation):
    """Normalize only task allocation paths, never CLI/auth/business read roots."""
    body = asdict(plan)
    for name in ('admission', 'admission_sha256', 'production_enabled'):
        body.pop(name, None)
    substitutions = sorted(((reservation[k], '${'+k+'}') for k in ('workspace','temp','git')), key=lambda x: -len(x[0]))
    def normalize(value):
        if isinstance(value, str):
            for path, token in substitutions: value = value.replace(path, token)
            return value
        if isinstance(value, (tuple,list)): return [normalize(v) for v in value]
        if isinstance(value, dict): return {k:normalize(v) for k,v in value.items()}
        return value
    return normalize(body)


def applied_policy(plan):
    return _digest({'argv':plan.argv,'environment':plan.environment,'cwd':plan.cwd,
                    'read_roots':plan.read_roots,'write_roots':plan.write_roots})


def validate_admission(binding, *, context, reservation, plan=None, expected_digest=None):
    if not binding: raise SupervisorRefusal('MINI_ACCEPTANCE_REQUIRED')
    from personal_agent_dal.worker.role_adapter import build_plan
    from personal_agent_dal.worker.runtime_process import os_boot_id
    try:
        if set(binding) != {'path','identity','pins','adapters','config_refs'}: raise ValueError()
        evidence = private_json(binding['path'])
        if set(evidence) != {'schema','code_sha256','policy','identity','roles','config_refs','issued_at','expires_at','revoked_at','scope','provenance'}: raise ValueError()
        if evidence['schema'] != SCHEMA or evidence['policy'] != POLICY or evidence['code_sha256'] != code_identity(): raise ValueError()
        if evidence['identity'] != binding['identity'] or evidence['identity']['boot_id'] != os_boot_id(): raise ValueError()
        if evidence['identity']['worker_id'] != context['worker_id']: raise ValueError()
        now = int(time.time())
        if any(type(evidence[k]) is not int for k in ('issued_at','expires_at')): raise ValueError()
        if not evidence['issued_at'] <= now < evidence['expires_at'] or evidence['revoked_at'] is not None: raise ValueError()
        if evidence['scope'] != 'single-role-report-only' or context['completion_mode'] != 'report_only': raise ValueError()
        if evidence['provenance'] != 'operator-attested-external-native-cli': raise ValueError()
        if evidence['config_refs'] != binding['config_refs'] or not binding['config_refs']: raise ValueError()
        for raw, sha in binding['config_refs'].items():
            if _digest(private_json(raw)) != sha: raise ValueError()
        roots = [reservation[k] for k in ('workspace','temp','git')] + list(reservation.get('read_roots', []))
        for raw in [binding['path'], *binding['config_refs']]:
            if any(Path(raw).is_relative_to(Path(root)) for root in roots): raise ValueError()
        if set(evidence['roles']) != {'planner','coder','reviewer'}: raise ValueError()
        selected = None
        for name in ('planner','coder','reviewer'):
            current = build_plan(dict(context, execution_role=name), reservation, binding['pins'], binding['adapters'])
            record = evidence['roles'][name]
            if set(record) != {'configuration','plan','smoke'}: raise ValueError()
            if record['configuration'] != context['snapshot']['roles'][name] or record['plan'] != plan_contract(current,reservation): raise ValueError()
            smoke = record['smoke']
            if set(smoke) != {'command','command_sha256','result','result_sha256','started_at','ended_at','scope','provenance','task_paths'}: raise ValueError()
            if smoke['provenance'] != 'external-native-cli' or smoke['scope'] != 'synthetic-files-only': raise ValueError()
            if set(smoke['task_paths']) != {'workspace','temp','git'}: raise ValueError()
            if any(not Path(p).is_absolute() or '${' in p for p in smoke['task_paths'].values()): raise ValueError()
            command=list(smoke['command'])
            for i,arg in enumerate(command):
                for key,path in sorted(smoke['task_paths'].items(),key=lambda x:-len(x[1])):
                    arg=arg.replace(path,'${'+key+'}')
                command[i]=arg
            if command != record['plan']['argv'] or _digest(smoke['command']) != smoke['command_sha256']: raise ValueError()
            result = smoke['result']
            if _digest(result) != smoke['result_sha256']: raise ValueError()
            if set(result) != {'exit_code','stdout_sha256','stderr_sha256','observations','cli_version'}: raise ValueError()
            if type(result['exit_code']) is not int or result['exit_code'] != 0 or result['cli_version'] != current.version: raise ValueError()
            import re
            if any(not isinstance(result[k],str) or not re.fullmatch('[0-9a-f]{64}',result[k]) for k in ('stdout_sha256','stderr_sha256')): raise ValueError()
            required = ['report-produced','scratch-write','business-write-denied'] if name != 'coder' else ['report-produced','scratch-write','source-edit','git-add','git-commit']
            if result['observations'] != required: raise ValueError()
            if any(type(smoke[k]) is not int for k in ('started_at','ended_at')) or not evidence['issued_at'] >= smoke['ended_at'] >= smoke['started_at'] or smoke['ended_at']-smoke['started_at'] > 120: raise ValueError()
            if name == context['execution_role']: selected = current
        sha = _digest(evidence)
        if expected_digest is not None and sha != expected_digest: raise ValueError()
        if plan is not None and replace(plan, admission=None, admission_sha256=None, production_enabled=False) != selected: raise ValueError()
        return sha
    except (KeyError, TypeError, ValueError, OSError):
        raise SupervisorRefusal('RUNTIME_ADMISSION_INVALID') from None


def revalidate_plan(plan, inventory, attempt):
    if not plan.admission: raise SupervisorRefusal('MINI_ACCEPTANCE_REQUIRED')
    row = inventory.get(attempt)
    reservation = inventory.supervisor.validate(row['reservation_id'])
    identity = (plan.admission or {}).get('identity', {})
    if (identity.get('boot_id'),identity.get('supervisor_epoch')) != (inventory.supervisor.boot_id,inventory.supervisor.epoch):
        raise SupervisorRefusal('SIGNER_IDENTITY_STALE')
    validate_admission(plan.admission, context=row['binding'], reservation=reservation,
                       plan=plan, expected_digest=plan.admission_sha256)
