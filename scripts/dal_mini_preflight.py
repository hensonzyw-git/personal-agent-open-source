#!/usr/bin/env python3
"""User-operated synthetic mini prerequisites. Never run automatically.

All modes emit only fixed labels, booleans and approved revision identity.
Exit 0 means this mode passed, never deployment or production enablement.
Runtime remains deliberately blocked until the CLI isolation contract is proven.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import stat
import sys
import time
import uuid

# Execute the reviewed checkout directly; do not resolve an unrelated installed DAL.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from personal_agent_dal.worker.supervisor import (
    Supervisor, SupervisorRefusal, verify_executable, provision_repository, current_boot_id,
)

FIELDS={'reviewed_sha','repository','supervisor_root','boot_id','supervisor_epoch',
        'architecture','dependency_fixture_root','git_pin','python_pin','sandbox_pin','runtime_pins','read_roots','dependency_commands'}


def load_config(path):
    body=json.loads(Path(path).read_text())
    if not isinstance(body,dict) or set(body)!=FIELDS:
        raise SupervisorRefusal('PREFLIGHT_CONFIG_INVALID')
    import re
    if not re.fullmatch('[0-9a-f]{40}',body['reviewed_sha']):
        raise SupervisorRefusal('REVIEWED_SHA_REQUIRED')
    for field in ('repository','supervisor_root'):
        if not Path(body[field]).is_absolute():raise SupervisorRefusal('ABSOLUTE_PATH_REQUIRED')
    if not isinstance(body['runtime_pins'],list) or not body['runtime_pins']:
        raise SupervisorRefusal('RUNTIME_PINS_REQUIRED')
    return body


def inventory(config,supervisor):
    if platform.system()!='Darwin' or platform.machine()!=config['architecture']:
        raise SupervisorRefusal('MAC_MINI_PLATFORM_REQUIRED')
    # Fixed system command, clean environment, no shell/login or credential read.
    observed=current_boot_id()
    if observed!=config['boot_id']:raise SupervisorRefusal('BOOT_ID_MISMATCH')
    git=verify_executable(config['git_pin'])
    env={'PATH':str(git.parent)+':/usr/bin:/bin','GIT_CONFIG_NOSYSTEM':'1',
         'GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0','GIT_NO_REPLACE_OBJECTS':'1'}
    def git_read(*args):
        result=subprocess.run([str(git),'-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null',
            '-C',config['repository'],*args],env=env,stdin=subprocess.DEVNULL,
            capture_output=True,timeout=30,check=True,close_fds=True)
        return result.stdout
    if git_read('rev-parse','HEAD').decode().strip()!=config['reviewed_sha']:
        raise SupervisorRefusal('REVIEWED_SHA_MISMATCH')
    roots=('src','scripts','tests')
    # git status can execute repository-local clean filters on dirty files.
    # The raw tree/byte/mode comparison below is the complete cleanliness gate.
    # Index flags, ignore rules and core.filemode cannot attest actual bytes.
    # Read only implementation paths and raw reviewed blobs (never filters).
    reviewed={}
    for entry in git_read('ls-tree','-r','-z',config['reviewed_sha'],'--',*roots).split(b'\0'):
        if not entry:continue
        meta,name=entry.split(b'\t',1)
        mode,kind,oid=meta.split()
        if kind!=b'blob' or mode not in (b'100644',b'100755'):
            raise SupervisorRefusal('UNREVIEWED_IMPLEMENTATION_CHANGES')
        reviewed[os.fsdecode(name)]=(mode,oid)
    actual=set()
    repository=Path(config['repository'])
    def walk_error(error):raise error
    for root in roots:
        base=repository/root
        if base.is_symlink():raise SupervisorRefusal('UNREVIEWED_IMPLEMENTATION_CHANGES')
        if not base.exists():continue
        if not base.is_dir():raise SupervisorRefusal('UNREVIEWED_IMPLEMENTATION_CHANGES')
        for directory,dirs,files in os.walk(base,followlinks=False,onerror=walk_error):
            if any((Path(directory)/name).is_symlink() for name in dirs):
                raise SupervisorRefusal('UNREVIEWED_IMPLEMENTATION_CHANGES')
            for name in files:
                path=Path(directory)/name
                relative=str(path.relative_to(repository))
                actual.add(relative)
                st=path.lstat()
                if relative not in reviewed or not stat.S_ISREG(st.st_mode):
                    raise SupervisorRefusal('UNREVIEWED_IMPLEMENTATION_CHANGES')
                mode,oid=reviewed[relative]
                if bool(st.st_mode & 0o111)!=(mode==b'100755') or path.read_bytes()!=git_read('cat-file','blob',oid.decode()):
                    raise SupervisorRefusal('UNREVIEWED_IMPLEMENTATION_CHANGES')
    if actual!=set(reviewed):raise SupervisorRefusal('UNREVIEWED_IMPLEMENTATION_CHANGES')
    pins=[config['python_pin'],config['sandbox_pin'],config['git_pin'],*config['runtime_pins']]
    for pin in pins:verify_executable(pin)
    reservation=supervisor.reserve(attempt_id='inventory-'+uuid.uuid4().hex,
        workspace_id=uuid.uuid4().hex,generation=1,authority={'mode':'synthetic_inventory'},read_roots=config['read_roots'])
    # --version is confined by the production sandbox with no login environment.
    # sandbox-exec itself has no supported --version; its explicit binary digest
    # and reviewed OS build/version label are the pin, not a guessed CLI flag.
    for pin in [config['python_pin'],config['git_pin'],*config['runtime_pins']]:
        code,output=supervisor.synthetic_process(reservation['reservation_id'],
            sandbox_pin=config['sandbox_pin'],argv=[pin['executable'],'--version'],
            executable_sha256=pin['executable_sha256'])
        if code!=0 or output.strip()!=pin['version']:
            raise SupervisorRefusal('CLI_VERSION_OR_SANDBOX_MISMATCH')
    return {'reviewed_revision':True,'platform':True,'boot':True,'pinned_binaries':True,'pinned_versions':True}


# Controlled source text, never supplied by a repository/model. It prints no
# paths or canary contents. A detached descendant performs the same attacks.
CANARY_PROGRAM=r'''
import json,os,sys,time
from pathlib import Path
body=json.loads(sys.argv[1])
def check():
    results={}
    for name,path in body['protected'].items():
        try:
            if name=='signing':
                with open(path,'rb') as stream: stream.read(1)
            else:
                with open(Path(path)/'forbidden-write','w') as stream:stream.write('synthetic')
            results[name]=False
        except OSError:results[name]=True
    results['environment']='DAL_SYNTHETIC_PARENT_CANARY' not in os.environ
    try:os.read(body['fd'],1);results['descriptor']=False
    except OSError:results['descriptor']=True
    try:
        (Path(body['own'])/'permitted-write').write_text('synthetic')
        results['own_workspace']=True
    except OSError:results['own_workspace']=False
    return results
if os.fork()==0:
    os.setsid()
    if os.fork()!=0:os._exit(0)
    os.close(1);os.close(2)
    time.sleep(.2)
    Path(body['descendant_result']).write_text(json.dumps(check()))
    os._exit(0)
print(json.dumps(check()),flush=True)
'''


def isolation(config,supervisor):
    run=uuid.uuid4().hex
    old=supervisor.reserve(attempt_id='old-'+run,workspace_id='old-'+run,generation=1,
        authority={'mode':'synthetic_old'},read_roots=config['read_roots'])
    new=supervisor.reserve(attempt_id='new-'+run,workspace_id='new-'+run,generation=2,
        authority={'mode':'synthetic_replacement'},read_roots=config['read_roots'])
    canary=supervisor.root/('synthetic-signing-'+run)
    fd=os.open(canary,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
    os.write(fd,os.urandom(32));os.lseek(fd,0,0)
    os.set_inheritable(fd,True)
    previous=os.environ.get('DAL_SYNTHETIC_PARENT_CANARY')
    os.environ['DAL_SYNTHETIC_PARENT_CANARY']='synthetic-only'
    descendant=Path(old['workspace'])/'descendant.json'
    targets={'replacement_work':new['workspace'],'replacement_git':new['git'],
        'replacement_temp':new['temp'],'replacement_parent':new['parent'],
        'supervisor_parent':str(supervisor.root),'signing':str(canary)}
    try:
        args={'protected':targets,'own':old['workspace'],'descendant_result':str(descendant),'fd':fd}
        rc,output=supervisor.synthetic_process(old['reservation_id'],sandbox_pin=config['sandbox_pin'],
            argv=[config['python_pin']['executable'],'-c',CANARY_PROGRAM,json.dumps(args)],
            executable_sha256=config['python_pin']['executable_sha256'])
    finally:
        os.close(fd)
        if previous is None:os.environ.pop('DAL_SYNTHETIC_PARENT_CANARY',None)
        else:os.environ['DAL_SYNTHETIC_PARENT_CANARY']=previous
    if rc!=0:raise SupervisorRefusal('CANARY_PROCESS_FAILED')
    direct=json.loads(output)
    deadline=time.monotonic()+10
    while not descendant.exists() and time.monotonic()<deadline:time.sleep(.05)
    if not descendant.exists():raise SupervisorRefusal('DETACHED_DESCENDANT_UNOBSERVED')
    detached=json.loads(descendant.read_text())
    expected=set(targets)|{'environment','descriptor','own_workspace'}
    if any(set(result)!=expected or any(value is not True for value in result.values()) for result in (direct,detached)):
        raise SupervisorRefusal('ISOLATION_CANARY_FAILED')
    reopened=Supervisor(supervisor.root,boot_id=config['boot_id'],epoch=config['supervisor_epoch'])
    reopened.validate(new['reservation_id'])
    try:Supervisor(supervisor.root,boot_id='synthetic-other-boot',epoch=config['supervisor_epoch']).validate(new['reservation_id'])
    except SupervisorRefusal:pass
    else:raise SupervisorRefusal('RESTART_DRIFT_NOT_DETECTED')
    work=Path(new['workspace']);work.rename(work.with_name('replaced-original'));work.mkdir(mode=0o700)
    try:reopened.validate(new['reservation_id'])
    except SupervisorRefusal:pass
    else:raise SupervisorRefusal('DIRECTORY_SUBSTITUTION_NOT_DETECTED')
    return {'direct_canaries':True,'detached_descendant_canaries':True,'restart':True,'directory_substitution':True}


def dependencies(config,supervisor):
    if not config['dependency_commands']:raise SupervisorRefusal('DEPENDENCY_COMMANDS_REQUIRED')
    r=supervisor.reserve(attempt_id='dependencies-'+uuid.uuid4().hex,workspace_id=uuid.uuid4().hex,generation=1,
        authority={'mode':'synthetic_dependencies'},read_roots=[*config['read_roots'],config['dependency_fixture_root']])
    provision_repository(supervisor,r['reservation_id'],source=config['repository'],base_sha=config['reviewed_sha'],git_pin=config['git_pin'])
    for command in config['dependency_commands']:
        if set(command)!={'pin','args'} or not isinstance(command['args'],list):raise SupervisorRefusal('DEPENDENCY_COMMAND_INVALID')
        executable=verify_executable(command['pin'])
        rc,_=supervisor.synthetic_process(r['reservation_id'],sandbox_pin=config['sandbox_pin'],
            argv=[str(executable),*command['args']],executable_sha256=command['pin']['executable_sha256'],timeout=60)
        if rc:raise SupervisorRefusal('DEPENDENCY_BUILD_TEST_FAILED')
    fixture=Path(config['dependency_fixture_root'])
    if not fixture.is_absolute() or fixture.is_symlink() or fixture.exists():
        raise SupervisorRefusal('FRESH_DEPENDENCY_FIXTURE_ROOT_REQUIRED')
    fixture.mkdir(mode=0o700)
    canary=fixture/'synthetic-read-only-canary'
    canary.write_bytes(os.urandom(32))
    before=hashlib.sha256(canary.read_bytes()).hexdigest()
    probe="import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.read_bytes();\ntry: p.write_bytes(b'changed')\nexcept OSError: sys.exit(0)\nsys.exit(1)"
    rc,_=supervisor.synthetic_process(r['reservation_id'],sandbox_pin=config['sandbox_pin'],
        argv=[config['python_pin']['executable'],'-c',probe,str(canary)],
        executable_sha256=config['python_pin']['executable_sha256'])
    if rc or hashlib.sha256(canary.read_bytes()).hexdigest()!=before:
        raise SupervisorRefusal('DEPENDENCY_MUTATION_NOT_DENIED')
    return {'repository_provisioned':True,'dependency_build_test':True,'dependency_mutation_denied':True}


def config_template():
    def pin(name):
        return {'executable':'/absolute/reviewed/'+name,'executable_sha256':'REPLACE_WITH_SHA256',
                'version':'REPLACE_WITH_EXACT_VERSION_OUTPUT'}
    return dict(reviewed_sha='REPLACE_WITH_REVIEWED_COMMIT_SHA40',repository='/absolute/reviewed/checkout',
        supervisor_root='/absolute/private/preflight-supervisor',boot_id='REPLACE_WITH_CURRENT_BOOT_UUID',
        supervisor_epoch=1,architecture='arm64',git_pin=pin('git'),python_pin=pin('python'),
        sandbox_pin=pin('sandbox-exec'),runtime_pins=[pin('explicit-cli')],
        read_roots=['/absolute/reviewed/read-only-runtime-root'],
        dependency_fixture_root='/absolute/new-synthetic-dependency-fixture',
        dependency_commands=[{'pin':pin('python'),'args':['-m','pytest','tests/representative']}])


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv==['--print-config-template']:
        print(json.dumps(config_template(),indent=2))
        return 0
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['inventory','isolation','runtime','dependencies'])
    parser.add_argument('--config',required=True)
    parser.add_argument('--allow-real-runtime',action='store_true')
    parser.add_argument('--instruction')
    args=parser.parse_args(argv)
    try:
        config=load_config(args.config)
        supervisor=Supervisor(Path(config['supervisor_root']),boot_id=config['boot_id'],epoch=config['supervisor_epoch'])
        results=inventory(config,supervisor)
        if args.mode=='isolation':results.update(isolation(config,supervisor))
        elif args.mode=='dependencies':results.update(dependencies(config,supervisor))
        elif args.mode=='runtime':
            if not args.allow_real_runtime or not args.instruction:
                raise SupervisorRefusal('EXPLICIT_RUNTIME_AUTHORIZATION_REQUIRED')
            # No login/call is implemented until zero-tool and descendant-key
            # enforcement is independently proven. Flags cannot override this.
            raise SupervisorRefusal('CLI_ZERO_TOOL_AND_CREDENTIAL_SEPARATION_UNIMPLEMENTED')
        print(json.dumps({'mode':args.mode,'status':'passed','reviewed_sha':config['reviewed_sha'],'checks':results,'production_enabled':False}))
        return 0
    except SupervisorRefusal as exc:
        print(json.dumps({'mode':args.mode,'status':'blocked','code':str(exc),'production_enabled':False}))
        return 1
    except Exception:
        # Arbitrary CLI/config/OS errors may contain paths, env or file content.
        print(json.dumps({'mode':args.mode,'status':'blocked','code':'PREFLIGHT_ERROR','production_enabled':False}))
        return 1


if __name__=='__main__':
    sys.exit(main())
