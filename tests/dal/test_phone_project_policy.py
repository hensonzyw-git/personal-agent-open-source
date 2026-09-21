"""Directory and configuration substitutions at the native Worker boundary."""
import pytest
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.worker.project_policy import directory_identity,validate_project_policy
from personal_agent_dal.worker.supervisor import SupervisorRefusal


def case(tmp_path):
    root=tmp_path/'project';root.mkdir()
    config=dict(schema='dal.workflow-worker/1.1',identity={'worker_id':'worker'},git_pin={},sandbox_pin={},
        projects={'p':dict(root=str(root),kind='existing')},project_policies={})
    policy=dict(template_digest='a'*64,directory_identity_digest=directory_identity(str(root)),base_sha='b'*40,base_branch='main')
    config['project_policies']['p']=dict(policy)
    policy.update(project_id='p',worker_id='worker',root=str(root),kind='existing',
        worker_configuration_digest=digest({k:config[k] for k in ('git_pin','projects','sandbox_pin')}),branch='refs/heads/codex/dal-w')
    return root,config,dict(owner={'workflow_id':'w'},project_policy=policy)


@pytest.mark.parametrize('mutation',['worker','configuration','template','directory','symlink','branch','legacy'])
def test_policy_substitution_is_refused(tmp_path,mutation):
    root,config,inputs=case(tmp_path)
    validate_project_policy(config,inputs)
    if mutation=='worker':config['identity']['worker_id']='other'
    if mutation=='configuration':config['git_pin']={'changed':True}
    if mutation=='template':config['project_policies']['p']['template_digest']='c'*64
    if mutation in ('directory','symlink'):
        root.rename(tmp_path/'old')
        if mutation=='directory':root.mkdir()
        else:root.symlink_to(tmp_path/'old',target_is_directory=True)
    if mutation=='branch':inputs['project_policy']['branch']='refs/heads/main'
    if mutation=='legacy':config['schema']='dal.workflow-worker/1.0'
    with pytest.raises(SupervisorRefusal):validate_project_policy(config,inputs)
