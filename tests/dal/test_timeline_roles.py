import pytest
from tests.dal.test_timeline_requests import world
from personal_agent_dal.timeline.roles import RoleService


def config(model='planner-model'):
    roles={name:dict(runtime_ref='codex',provider_ref='openai',model=model if name=='planner' else name+'-model',reasoning='high',placement_ref='home',permission='workspace_write' if name=='coder' else 'read_only',billing='subscription') for name in ('planner','coder','reviewer')}
    return dict(contract_version='dal.role-contract/3.0',configuration_id='config',revision=1,roles=roles)


def registry(body):
    return [dict(**r,adapter='codex_cli') for r in body['roles'].values()]


def test_three_arbitrary_registered_roles_and_scope_cas(world):
    body=config();roles=RoleService(world[2],registry(body))
    rev=roles.register(body)
    roles.bind(scope='system',scope_id='default',revision_id=rev,expected_version=0)
    resolved=roles.resolve()
    assert resolved['roles']==body['roles']
    assert resolved['source']=='system'
    with pytest.raises(ValueError,match='STALE_BINDING'):roles.bind(scope='system',scope_id='default',revision_id=rev,expected_version=0)
    snap=roles.snapshot()
    assert snap['contract_version']=='dal.role-contract/3.0'
    assert snap['roles']['reviewer']['model']=='reviewer-model'


@pytest.mark.parametrize('attack',['self_review','permission','unknown_model','extra_host'])
def test_roles_fail_closed(world,attack):
    body=config();registered=registry(body)
    if attack=='self_review':body['roles']['reviewer']['model']='planner-model'
    elif attack=='permission':body['roles']['planner']['permission']='workspace_write'
    elif attack=='unknown_model':body['roles']['coder']['model']='unregistered'
    else:body['roles']['coder']['host']='https://attacker.invalid'
    with pytest.raises(ValueError):RoleService(world[2],registered).register(body)


def test_saved_config_is_not_runtime_admission(world):
    body=config();service=RoleService(world[2],registry(body))
    id=service.register(body);service.bind(scope='system',scope_id='default',revision_id=id,expected_version=0)
    assert service.resolve()['available'] is False


def test_provider_alias_does_not_make_same_model_independent(world):
    body=config()
    body['roles']['reviewer']['model']=body['roles']['planner']['model']
    body['roles']['reviewer']['provider_ref']='another-gateway'
    with pytest.raises(ValueError,match='REVIEW_NOT_INDEPENDENT'):
        RoleService(world[2],registry(body)).register(body)


def test_workflow_resolution_uses_bound_project_before_system(world):
    from datetime import timedelta
    from tests.dal.test_timeline_requests import submit
    from tests.dal.test_timeline_artifacts import source
    from personal_agent_dal.timeline.artifacts import ArtifactService
    from personal_agent_dal.storage.timeline_models import DevelopmentProjectBinding, DevelopmentProjectAuthorization
    r=world[2];wf=submit(r)['request_id']
    sha=source(r,wf,'Synthetic route')
    artifact=ArtifactService(r).record(step_id='step',expected_result_digest=sha)
    with r.sessions() as s,s.begin():
        s.add(DevelopmentProjectAuthorization(grant_id='grant',version=1,project_id='synthetic-project',
            subject='device:synthetic',sealed_grant=r._seal(DevelopmentProjectAuthorization,'grant','sealed_grant',{}),
            digest='a'*64,expires_at=r.now()+timedelta(hours=1),revoked=0))
        s.flush()
        s.add(DevelopmentProjectBinding(workflow_id=wf,project_id='synthetic-project',grant_id='grant',
            grant_version=1,route_artifact_id=artifact,candidate_digest='b'*64,
            sealed_binding=r._seal(DevelopmentProjectBinding,wf,'sealed_binding',{})))
    body=config();service=RoleService(r,registry(body))
    revision=service.register(body)
    service.bind(scope='project',scope_id='synthetic-project',revision_id=revision,expected_version=0)
    assert service.resolve(workflow_id=wf)['source']=='project'
    with pytest.raises(ValueError,match='PROJECT_BINDING_MISMATCH'):
        service.resolve(workflow_id=wf,project_id='different-project')
