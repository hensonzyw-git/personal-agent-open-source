"""One closed reviewer route. Static hashes are not live gateway evidence.

Only a public metadata document and helper executable bytes are read. The helper
is never executed here and its output is never captured by DAL. Native CLI probe
and owner attestation remain necessary under runtime admission.
"""
import hashlib
import os
from pathlib import Path
import re
import stat

from personal_agent_dal.worker.supervisor import SupervisorRefusal, _absolute, _digest

MODE = 'claude_existing_third_party'
SCHEMA = 'dal.role-adapters/2.0'
ENDPOINT = 'http://127.0.0.1:3456'
MODEL = 'changhe/ch-g/kimi-k3'
PUBLIC_ROUTE = dict(schema='dal.reviewer-route/1.0', route_id='changhe-kimi-k3-loopback',
    revision=1, endpoint=ENDPOINT, provider='changhe', model=MODEL,
    upstream_model='ch-g/kimi-k3', transport='openai_chat_completions',
    reasoning='high', billing='api', proxy=None, redirects=False,
    fallback_mode='off', fallback_models=[])
LIMIT = 1024 * 1024


def reference(value):
    if (not isinstance(value, dict) or set(value) != {'path','sha256'} or
            not isinstance(value['path'], str) or not isinstance(value['sha256'], str) or
            re.fullmatch('[0-9a-f]{64}', value['sha256']) is None):
        raise SupervisorRefusal('ROUTE_REFERENCE_INVALID')
    path = _absolute(value['path'])
    if any(ord(c) < 32 for c in str(path)):
        raise SupervisorRefusal('ROUTE_REFERENCE_INVALID')
    return path


def validate_shape(route):
    if (not isinstance(route, dict) or set(route) !=
            {'mode','route_id','revision','endpoint','helper','config_ref'} or
            route['mode'] != MODE or route['route_id'] != PUBLIC_ROUTE['route_id'] or
            type(route['revision']) is not int or route['revision'] != 1 or
            route['endpoint'] != ENDPOINT):
        raise SupervisorRefusal('REVIEWER_ROUTE_INVALID')
    reference(route['helper']); reference(route['config_ref'])


def verify_helper(ref, roots):
    path = reference(ref)
    if any(path.is_relative_to(Path(root).resolve()) for root in roots):
        raise SupervisorRefusal('AUTH_HELPER_TASK_WRITABLE')
    # Held parent dirfd, no symlinks at any component, nonblocking special-file
    # refusal, bounded read and same-fd mutation detection.
    from personal_agent_dal.worker.runtime_evidence import _open_directory
    parent = fd = None
    try:
        parent = _open_directory(path.parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                stat.S_IMODE(before.st_mode) != 0o700 or before.st_nlink != 1 or
                not 0 < before.st_size <= LIMIT):
            raise SupervisorRefusal('AUTH_HELPER_NOT_PRIVATE_EXECUTABLE')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(LIMIT + 1)
        after = os.fstat(fd)
        if ((before.st_mode, before.st_uid, before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_mode, after.st_uid, after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or
                len(raw) != before.st_size or
                hashlib.sha256(raw).hexdigest() != ref['sha256']):
            raise SupervisorRefusal('AUTH_HELPER_DIGEST_MISMATCH')
    except OSError:
        raise SupervisorRefusal('AUTH_HELPER_UNREADABLE') from None
    finally:
        if fd is not None: os.close(fd)
        if parent is not None: os.close(parent)


def validate_route(role, route, name, reservation, version):
    validate_shape(route)
    if (name != 'reviewer' or
            (role.runtime, role.provider, role.model, role.reasoning, role.billing, role.permission) !=
            ('claude_code','changhe',MODEL,'high','api','read_only') or version != '2.1.231'):
        raise SupervisorRefusal('AUTH_ROUTE_MISMATCH')
    # Include coder-writable roots even though this role is read-only.
    roots = [reservation[k] for k in ('workspace','temp','git')]
    verify_helper(route['helper'], roots)
    public = reference(route['config_ref'])
    if any(public.is_relative_to(Path(root).resolve()) for root in roots):
        raise SupervisorRefusal('ROUTE_METADATA_TASK_WRITABLE')
    from personal_agent_dal.worker.runtime_admission import private_json
    metadata = private_json(public)
    if _digest(metadata) != _digest(PUBLIC_ROUTE) or _digest(metadata) != route['config_ref']['sha256']:
        raise SupervisorRefusal('ROUTE_METADATA_MISMATCH')
    return dict(mode=MODE, home=None, environment={
        'CLAUDE_CONFIG_DIR':str(Path(reservation['temp'])/'claude-isolated'),
        'ANTHROPIC_BASE_URL':ENDPOINT,
        'ANTHROPIC_MODEL':MODEL,
        'ANTHROPIC_DEFAULT_OPUS_MODEL':MODEL,
        'ANTHROPIC_DEFAULT_SONNET_MODEL':MODEL,
        'ANTHROPIC_DEFAULT_HAIKU_MODEL':MODEL,
        'CLAUDE_CODE_SUBAGENT_MODEL':MODEL,
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}, reference=route)
