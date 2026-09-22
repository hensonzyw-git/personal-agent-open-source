"""Read-only operator preflight; never installs config, templates, or grants.

Prepare prints a protected policy fragment. After the operator installs it and
renews executor admission, attest emits the exact DAL registration envelope.
"""
import argparse
import json
import subprocess
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from personal_agent_dal.timeline.authorization_contracts import ProjectTemplate, LOCAL_BOOTSTRAP_SHA
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.worker.project_policy import directory_identity, validate_project_policy
from personal_agent_dal.worker.supervisor import verify_executable, SupervisorRefusal
from personal_agent_dal.worker.workflow import load_config
from personal_agent_dal.worker.runtime_admission import private_json
from personal_agent_dal.worker.workflow_process import validate_executor


def inspect_project(config, template):
    template=ProjectTemplate.model_validate(template)
    project=config['projects'].get(template.project_id)
    if (project is None or project['root']!=template.root or project['kind']!=template.kind
        or config['identity']['worker_id']!=template.worker_id
        or directory_identity(template.root)!=template.directory_identity_digest
        or digest({k:config[k] for k in ('git_pin','projects','sandbox_pin')})!=template.worker_configuration_digest):
        raise SupervisorRefusal('PROJECT_POLICY_CHANGED')
    git=str(verify_executable(config['git_pin']))
    verify_executable(config['sandbox_pin'])
    for command in project['verification_commands']:verify_executable(command['pin'])
    if template.kind=='existing':
        environment={'PATH':'/usr/bin:/bin','HOME':'/var/empty','GIT_CONFIG_NOSYSTEM':'1',
            'GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0','GIT_NO_REPLACE_OBJECTS':'1','GIT_OPTIONAL_LOCKS':'0'}
        result=subprocess.run([git,'-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null',
            'rev-parse','--verify',template.base_sha+'^{commit}'],cwd=template.root,env=environment,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=False)
        if result.returncode or result.stdout.decode().strip()!=template.base_sha:raise SupervisorRefusal('PROJECT_BASE_CHANGED')
    elif template.base_sha!=LOCAL_BOOTSTRAP_SHA:raise SupervisorRefusal('PROJECT_BASE_CHANGED')
    value=template.model_dump(mode='json')
    policy=dict(template_digest=digest(value),directory_identity_digest=template.directory_identity_digest,
        base_sha=template.base_sha,base_branch=template.base_branch)
    return template,policy


def attest(config,template,*,now=None,until_revoked=False):
    template,policy=inspect_project(config,template)
    authority=dict(policy,project_id=template.project_id,root=template.root,kind=template.kind,
        worker_id=template.worker_id,worker_configuration_digest=template.worker_configuration_digest,
        branch='refs/heads/codex/dal-registration-check')
    validate_project_policy(config,dict(project_policy=authority,owner={'workflow_id':'registration-check'}))
    validate_executor(config,SimpleNamespace(boot_id=config['identity']['boot_id'],epoch=config['identity']['supervisor_epoch']))
    now=now or datetime.now(timezone.utc)
    value=template.model_dump(mode='json')
    evidence=dict(template_digest=digest(value),worker_policy=policy,
        executor_evidence_digest=digest(private_json(config['executor_admission_file'])),observed_at=now.isoformat())
    return dict(template=value,observed_at=now.isoformat(),expires_at=None if until_revoked else (now+timedelta(hours=1)).isoformat(),evidence_digest=digest(evidence))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','attest'])
    parser.add_argument('--config',required=True)
    parser.add_argument('--template',required=True,help='Private operator template file; no credentials')
    parser.add_argument('--until-revoked',action='store_true',help='explicit durable operator registration')
    args=parser.parse_args()
    try:
        config=load_config(args.config);template=private_json(args.template)
        if args.mode=='prepare':
            model,policy=inspect_project(config,template)
            result=dict(schema='dal.project-registration-preflight/1.0',registration_ready=False,
                project_id=model.project_id,worker_policy=policy)
        else:result=attest(config,template,until_revoked=args.until_revoked)
    except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError):
        parser.exit(1,'Project registration preflight refused; check protected configuration and admission.\n')
    print(json.dumps(result,sort_keys=True,ensure_ascii=False))


if __name__=='__main__':main()
