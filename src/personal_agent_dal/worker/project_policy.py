"""Protected phone-authorized project policies; no caller supplied root or command."""
from pathlib import Path
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.worker.supervisor import SupervisorRefusal


def directory_identity(root):
    path=Path(root)
    try:
        if not path.is_absolute() or str(path.resolve(strict=True))!=root or not path.is_dir():raise ValueError
        stat=path.stat()
        return digest(dict(root=root,device=stat.st_dev,inode=stat.st_ino))
    except (OSError,ValueError):raise SupervisorRefusal('PROJECT_DIRECTORY_CHANGED') from None


def validate_project_policy(config,inputs):
    policy=inputs.get('project_policy')
    if policy is None:return
    expected={'template_digest','project_id','worker_id','worker_configuration_digest','directory_identity_digest',
        'root','kind','base_sha','base_branch','branch'}
    if not isinstance(policy,dict) or set(policy)!=expected or config['schema']!='dal.workflow-worker/1.1':
        raise SupervisorRefusal('PROJECT_POLICY_REQUIRED')
    project=config['projects'].get(policy['project_id'])
    registered=config['project_policies'].get(policy['project_id'])
    if (project is None or registered is None or registered!={k:policy[k] for k in ('template_digest','directory_identity_digest','base_sha','base_branch')}
        or project['root']!=policy['root'] or project['kind']!=policy['kind']
        or config['identity']['worker_id']!=policy['worker_id']
        or digest({k:config[k] for k in ('git_pin','projects','sandbox_pin')})!=policy['worker_configuration_digest']
        or policy['branch']!='refs/heads/codex/dal-'+inputs['owner']['workflow_id']
        or directory_identity(policy['root'])!=policy['directory_identity_digest']):
        raise SupervisorRefusal('PROJECT_POLICY_CHANGED')
