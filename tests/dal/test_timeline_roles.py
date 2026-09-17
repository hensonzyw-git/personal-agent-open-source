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
