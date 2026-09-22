"""Read-only source observation after a proven process stop; never retries writes."""
import json
from pathlib import Path
from personal_agent_dal.worker.supervisor import Supervisor,SupervisorRefusal,verify_executable
from personal_agent_dal.worker.workflow_process import run_owned,validate_executor
from personal_agent_dal.worker.toolchain import _sandboxed_argv,_child_environment


def observe(worker,row):
    result=dict(process_exited=True,head_sha=None,tree_sha=None)
    inputs=row['binding']['execution_input']
    if inputs['phase'] not in ('coding','fix','stage_commit','workspace_prepare','delivery_prepare','delivery_probe'):return result
    validate_executor(worker.config,worker.supervisor)
    with worker.supervisor._db() as db:
        saved=db.execute('SELECT body FROM reservations WHERE id=?',(row['reservation_id'],)).fetchone()
    if saved is None:raise SupervisorRefusal('RESERVATION_MISSING')
    original=json.loads(saved[0])
    # Historical identity validation permits observation after reboot, not a new
    # launch under the old epoch or a rewritten reservation.
    historical=Supervisor(worker.supervisor.root,boot_id=original['boot_id'],epoch=original['supervisor_epoch'])
    reservation=historical.validate(row['reservation_id'])
    work=Path(reservation['workspace']);gitdir=Path(reservation['git'])/'repository'
    pointer=work/'.git'
    if pointer.is_symlink() or not pointer.is_file() or pointer.read_text().strip()!='gitdir: '+str(gitdir):
        raise SupervisorRefusal('GIT_DIRECTORY_SUBSTITUTED')
    git=str(verify_executable(worker.config['git_pin']));sandbox=str(verify_executable(worker.config['sandbox_pin']))
    scratch=Path(reservation['temp'])/('observe-'+row['effective_attempt']);scratch.mkdir(mode=0o700,exist_ok=True)
    command=(git,'--git-dir='+str(gitdir),'--work-tree='+str(work),'-c','core.hooksPath=/dev/null','-c','protocol.allow=never',
        '-c','core.fsmonitor=false','show','-s','--format=%H %T','HEAD')
    argv=_sandboxed_argv(command,scratch,scratch,(),(work,Path(reservation['git'])))
    if argv[0]!=sandbox:raise SupervisorRefusal('SANDBOX_PIN_MISMATCH')
    environment=_child_environment(scratch)
    environment.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_TERMINAL_PROMPT='0',GIT_NO_LAZY_FETCH='1',GIT_NO_REPLACE_OBJECTS='1')
    output,code=run_owned(argv,cwd=scratch,environment=environment,timeout=15,inventory=worker.inventory,
        attempt=row['effective_attempt'],heartbeat=lambda:True,revalidate=lambda:validate_executor(worker.config,worker.supervisor))
    import re
    match=re.fullmatch(rb'([a-f0-9]{40}) ([a-f0-9]{40})\n',output)
    if code or match is None:raise SupervisorRefusal('SOURCE_OBSERVATION_UNPROVEN')
    result.update(head_sha=match[1].decode(),tree_sha=match[2].decode())
    return result
