"""Durable local ownership and prelaunch checks; no machine acceptance shortcut.

The local reservation is not a lease. Server leases and dispatch CAS remain
mandatory. The production credential-bearing launcher is deliberately blocked.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import uuid

from personal_agent_core.manifest import canonical_json


class SupervisorRefusal(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _identity(path):
    st=path.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise SupervisorRefusal('DIRECTORY_REQUIRED')
    return [st.st_dev,st.st_ino]


def _tree(path):
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs+files:
            st=(Path(root)/name).lstat()
            if stat.S_ISLNK(st.st_mode) or (stat.S_ISREG(st.st_mode) and st.st_nlink!=1):
                raise SupervisorRefusal('LINKED_WRITABLE_CONTENT')
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise SupervisorRefusal('SPECIAL_WRITABLE_CONTENT')


def _absolute(path):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise SupervisorRefusal('ABSOLUTE_PATH_REQUIRED')
    # /tmp can be a platform alias; callers must supply its canonical path.
    for part in (path,*path.parents):
        if part.is_symlink(): raise SupervisorRefusal('SYMLINK_PATH')
    return path


class Supervisor:
    def __init__(self, root, *, boot_id, epoch):
        self.root=_absolute(root)
        if not boot_id or type(epoch) is not int or epoch<1:
            raise SupervisorRefusal('SUPERVISOR_IDENTITY_REQUIRED')
        self.boot_id,self.epoch=boot_id,epoch
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self._private(self.root)
        with self._lock():
            with self._db() as db:
                db.execute('CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, attempt TEXT UNIQUE NOT NULL, workspace TEXT UNIQUE NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, process_id INTEGER)')
                db.execute('CREATE TABLE IF NOT EXISTS launch_inventory (id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL REFERENCES reservations(id), body TEXT NOT NULL, pid INTEGER, pgid INTEGER)')

    @staticmethod
    def _private(path):
        _identity(path)
        st=path.stat()
        if st.st_uid!=os.getuid() or st.st_mode & 0o077:
            raise SupervisorRefusal('SHARED_WRITABLE_PARENT')

    @contextmanager
    def _lock(self):
        self._private(self.root)
        fd=os.open(self.root/'lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            st=os.fstat(fd)
            if st.st_nlink!=1 or st.st_mode & 0o077:raise SupervisorRefusal('LOCK_IDENTITY_INVALID')
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:os.close(fd)

    @contextmanager
    def _db(self):
        path=self.root/'reservations.sqlite3'
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            st=os.fstat(fd)
            if st.st_nlink!=1 or st.st_mode & 0o077:raise SupervisorRefusal('STORE_IDENTITY_INVALID')
        finally:os.close(fd)
        db=sqlite3.connect(path)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            with db:yield db
        finally:db.close()

    def reserve(self, *, attempt_id, workspace_id, generation, authority, read_roots):
        for value in (attempt_id,workspace_id):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+',value) or value in ('.','..'):
                raise SupervisorRefusal('RESERVATION_ID_INVALID')
        if type(generation) is not int or generation<1:raise SupervisorRefusal('GENERATION_INVALID')
        reads=[str(_absolute(p)) for p in read_roots]
        for p in map(Path,reads):
            if self.root.is_relative_to(p) or p.is_relative_to(self.root):
                raise SupervisorRefusal('ROOT_OVERLAP')
        with self._lock(),self._db() as db:
            prior=db.execute('SELECT body FROM reservations WHERE attempt=? OR workspace=?',(attempt_id,workspace_id)).fetchone()
            if prior:
                body=json.loads(prior[0])
                if (body['attempt_id'],body['workspace_id'],body['generation'],body['authority'],body['read_roots'])!=(attempt_id,workspace_id,generation,authority,reads):
                    raise SupervisorRefusal('RESERVATION_CONFLICT')
                return self._validate(db,body['reservation_id'])
            run=self.root/('workspace-'+workspace_id)
            # Never adopt a directory left without a committed reservation.
            try:run.mkdir(mode=0o700)
            except FileExistsError:
                raise SupervisorRefusal('WORKSPACE_ORPHANED') from None
            for name in ('work','tmp','git'): (run/name).mkdir(mode=0o700)
            body=dict(created_at_epoch=int(time.time()),reservation_id=str(uuid.uuid4()),attempt_id=attempt_id,workspace_id=workspace_id,
                generation=generation,authority=authority,read_roots=reads,
                boot_id=self.boot_id,supervisor_epoch=self.epoch,workspace=str(run/'work'),
                temp=str(run/'tmp'),git=str(run/'git'),parent=str(run),
                identities={str(p):_identity(p) for p in (self.root,run,run/'work',run/'tmp',run/'git')},
                restrictions={'network':False,'inherit_environment':False,'inherit_fds':False,
                              'descendants':'same-sandbox','production_launch':'disabled_pending_mini'})
            body['sandbox_policy_sha256']=hashlib.sha256(self._profile(body).encode()).hexdigest()
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,NULL)',
                (body['reservation_id'],attempt_id,workspace_id,canonical_json(body),'reserved'))
            return body

    def _validate(self,db,reservation_id):
        row=db.execute('SELECT body,state FROM reservations WHERE id=?',(reservation_id,)).fetchone()
        if not row:raise SupervisorRefusal('HISTORICAL_LAUNCH_MANIFEST_MISSING')
        body=json.loads(row[0])
        if row[1]!='reserved':raise SupervisorRefusal('RESERVATION_ALREADY_DISPATCHED')
        if (body['boot_id'],body['supervisor_epoch'])!=(self.boot_id,self.epoch):
            raise SupervisorRefusal('BOOT_EPOCH_STALE')
        for raw,identity in body['identities'].items():
            path=_absolute(raw)
            self._private(path)
            if _identity(path)!=identity:raise SupervisorRefusal('DIRECTORY_SUBSTITUTED')
        _tree(Path(body['parent']))
        return body

    def validate(self,reservation_id):
        with self._lock(),self._db() as db:return self._validate(db,reservation_id)

    def launch(self,reservation_id,*,executor=None):
        with self._lock(),self._db() as db:self._validate(db,reservation_id)
        # No boolean config, callable, signature or offline test can turn this on.
        raise SupervisorRefusal('MINI_ACCEPTANCE_REQUIRED')

    def controlled_prelaunch(self,reservation_id,*,commit_permission,executor):
        """Synthetic control-flow probe only. Never used by the provider launcher.

        The caller supplies an in-process canary, not argv or credentials. A
        committed marker remains unknown on any subsequent exception.
        """
        with self._lock(),self._db() as db:
            body=self._validate(db,reservation_id)
            if commit_permission()!='DISPATCH_GRANTED':raise SupervisorRefusal('DISPATCH_NOT_GRANTED')
            db.execute('UPDATE reservations SET state=? WHERE id=?',('dispatch_committed',reservation_id))
        return executor(body)

    def sandbox_profile(self,reservation_id):
        body=self.validate(reservation_id)
        return self._profile(body)

    @staticmethod
    def _profile(body):
        literal=lambda value: json.dumps(str(value))
        reads=body['read_roots']+[body['workspace'],body['temp'],body['git']]
        writes=[body['workspace'],body['temp'],body['git']]
        return '\n'.join(['(version 1)','(deny default)','(allow process*)','(allow sysctl-read)',
            '(allow file-read-metadata)',
            *['(allow file-read* (subpath '+literal(p)+'))' for p in reads],
            *['(allow file-write* (subpath '+literal(p)+'))' for p in writes]])

    def synthetic_process(self,reservation_id,*,sandbox_pin,argv,executable_sha256,timeout=30):
        """Preflight only: no inherited env, credentials, descriptors or network.

        killpg targets the original process group only. Descendants that call
        setsid escape termination; pid/pgid inventory is not full tree coverage.
        Their sandbox confinement still requires actual mini evidence.
        """
        with self._lock():
            with self._db() as db:
                body=self._validate(db,reservation_id)
                executable=_absolute(argv[0])
                if hashlib.sha256(executable.read_bytes()).hexdigest()!=executable_sha256:
                    raise SupervisorRefusal('EXECUTABLE_IDENTITY_CHANGED')
                if not any(executable.is_relative_to(Path(p)) for p in body['read_roots']):
                    raise SupervisorRefusal('EXECUTABLE_NOT_READABLE')
                policy=self._profile(body)
                inventory_id=str(uuid.uuid4())
                inventory=dict(executable_sha256=executable_sha256,argv_sha256=_digest(argv),
                    policy_sha256=hashlib.sha256(policy.encode()).hexdigest(),
                    reservation_sha256=_digest(body),boot_id=self.boot_id,supervisor_epoch=self.epoch,
                    environment='synthetic_allowlist_only',descriptors='closed',network=False)
                db.execute('INSERT INTO launch_inventory VALUES (?,?,?,NULL,NULL)',
                    (inventory_id,reservation_id,canonical_json(inventory)))
            # The concrete inventory is durable before Popen; keep the local
            # lock until the actual process identity is appended.
            with self._db() as db:self._validate(db,reservation_id)
            sandbox_executable=verify_executable(sandbox_pin)
            process=subprocess.Popen([str(sandbox_executable),'-p',policy,*argv],
                cwd=body['workspace'],env={'HOME':body['temp'],'TMPDIR':body['temp'],'PATH':'/usr/bin:/bin'},
                stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                close_fds=True,start_new_session=True,text=True)
            try:
                with self._db() as db:
                    db.execute('UPDATE reservations SET process_id=? WHERE id=?',(process.pid,reservation_id))
                    db.execute('UPDATE launch_inventory SET pid=?,pgid=? WHERE id=?',(process.pid,process.pid,inventory_id))
            except BaseException:
                import signal
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
                raise
        try:
            output,_=process.communicate(timeout=timeout)
            return process.returncode,output
        except BaseException:
            import signal
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait()
            raise


def spawn_unaccepted(*args, **kwargs):
    """Shared gate for legacy coder/toolchain subprocess entrypoints."""
    raise SupervisorRefusal('SUPERVISOR_MACHINE_PROOF_REQUIRED')


def signed_manifest(supervisor, reservation, *, identity, key, now):
    """Sign within the fixed reservation window; replay preserves payload and SHA."""
    from personal_agent.api.dal_client import sign_decision
    body=supervisor.validate(reservation['reservation_id'])
    if now < body['created_at_epoch']:raise SupervisorRefusal('RESERVATION_FUTURE')
    if now >= body['created_at_epoch']+900:raise SupervisorRefusal('RESERVATION_EXPIRED')
    authority=body['authority']
    payload=dict(schema='dal.launch-manifest/1.0',**identity,
        attempt_id=body['attempt_id'],workspace_id=body['workspace_id'],workspace_generation=body['generation'],
        isolation_policy_sha256=hashlib.sha256(supervisor._profile(body).encode()).hexdigest(),
        inventory_sha256=_digest(body),reservation_id=body['reservation_id'],
        job_id=authority['job_id'],job_lease_epoch=authority['job_lease_epoch'],
        lease_id=authority['lease_id'],policy_lease_epoch=authority['policy_lease_epoch'],
        issued_at=body['created_at_epoch'],expires_at=body['created_at_epoch']+900)
    if identity['boot_id']!=supervisor.boot_id or identity['supervisor_epoch']!=supervisor.epoch:
        raise SupervisorRefusal('SIGNER_IDENTITY_STALE')
    return sign_decision(payload,key=key,kid=identity['kid']),_digest(payload)


def verify_executable(pin):
    """Identity comes exclusively from explicit configuration; no PATH lookup."""
    if set(pin)!={'executable','executable_sha256','version'} or not pin['version']:
        raise SupervisorRefusal('EXECUTABLE_PIN_REQUIRED')
    path=_absolute(pin['executable'])
    st=path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022 or not os.access(path,os.X_OK):
        raise SupervisorRefusal('EXECUTABLE_NOT_PROTECTED')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=pin['executable_sha256']:
        raise SupervisorRefusal('EXECUTABLE_IDENTITY_CHANGED')
    return path


def provision_repository(supervisor,reservation_id,*,source,base_sha,git_pin):
    """Trusted local Git provisioning; no shell, hooks, credentials or remotes.

    Clone copies objects rather than sharing Git metadata or hard links. It
    performs no provider execution and is never placed in a retryable DB unit.
    """
    if not re.fullmatch('[0-9a-f]{40}',base_sha):raise SupervisorRefusal('BASE_SHA_INVALID')
    git=verify_executable(git_pin)
    source=_absolute(source)
    if not (source/'.git').exists():raise SupervisorRefusal('SOURCE_REPOSITORY_REQUIRED')
    with supervisor._lock(),supervisor._db() as db:
        body=supervisor._validate(db,reservation_id)
        work=Path(body['workspace']);metadata=Path(body['git'])
        if body.get('repository')=={'base_sha':base_sha,'git_pin':git_pin}:
            return body
        if any(work.iterdir()) or any(metadata.iterdir()):
            raise SupervisorRefusal('PROVISIONING_REPLAY_REQUIRES_REVIEW')
        env={'PATH':str(git.parent)+':/usr/bin:/bin','HOME':body['temp'],'TMPDIR':body['temp'],
             'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0','GIT_NO_REPLACE_OBJECTS':'1'}
        commands=[['clone','--no-hardlinks','--no-checkout','--separate-git-dir',str(metadata/'repository'),'--',str(source),str(work)],
                  ['-C',str(work),'checkout','--detach',base_sha]]
        for args in commands:
            result=subprocess.run([str(git),'-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null',*args],env=env,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                close_fds=True,timeout=60)
            if result.returncode:raise SupervisorRefusal('REPOSITORY_PROVISIONING_FAILED')
        if (metadata/'repository'/'objects'/'info'/'alternates').exists():raise SupervisorRefusal('SHARED_GIT_OBJECTS')
        _tree(work);_tree(metadata)
        # Git may recreate a target directory. Capture its actual post-provision
        # identity before signing, while the local resource lock is held.
        body['identities']={p:_identity(Path(p)) for p in body['identities']}
        body['repository']={'base_sha':base_sha,'git_pin':git_pin}
        db.execute('UPDATE reservations SET body=? WHERE id=?',(canonical_json(body),reservation_id))
        return body


def require_machine_acceptance():
    """Fail before any credential read on the historical Worker route."""
    raise SupervisorRefusal('MINI_ACCEPTANCE_REQUIRED')


def current_boot_id():
    """Trusted platform identity read, before signing material is accessed."""
    import platform
    if platform.system()!='Darwin':raise SupervisorRefusal('MACOS_CAPABILITY_REQUIRED')
    result=subprocess.run(['/usr/sbin/sysctl','-n','kern.bootsessionuuid'],
        env={'PATH':'/usr/bin:/bin'},capture_output=True,text=True,timeout=5)
    if result.returncode or not result.stdout.strip():raise SupervisorRefusal('BOOT_ID_UNAVAILABLE')
    return result.stdout.strip()
