import copy
import pytest
from personal_agent_dal.machine.workflow_selection import digest
from personal_agent_dal.worker.runtime_mapping import resolve_snapshot


def snapshot():
    return dict(revision_id='B-1', profile='B', revision=1, input_sha256='0'*64, fallback=None,
        roles={role:dict(runtime='codex_cli',provider='openai',model=model,reasoning=effort,
            placement='home_mac',permission=permission,billing='subscription')
        for role,model,effort,permission in [('coder','gpt-5.6-sol','high','workspace_write'),
        ('planner','gpt-6-astra','medium','read_only'),('reviewer','gpt-6-astra','medium','read_only')]})


def pins(body):
    return [dict(role=role, configuration=config, executable='/opt/pinned/cli', version='explicit-1',
                 executable_sha256='a'*64) for role,config in body['roles'].items()]


def test_exact_b_no_mutable_reference():
    body=snapshot()
    result=resolve_snapshot(body,digest(body),pins(body))
    body['roles']['coder']['model']='changed'
    assert result['coder'].configuration.model == 'gpt-5.6-sol'


@pytest.mark.parametrize('change', ['digest','missing','permission','provider','model','fallback','relative'])
def test_no_fallback(change):
    body=snapshot(); registered=pins(copy.deepcopy(body)); sha=digest(body)
    if change=='digest': sha='f'*64
    elif change=='missing': registered=[]
    elif change=='relative': registered[0]['executable']='codex'
    elif change=='fallback': body['fallback']='A';sha=digest(body)
    else: body['roles']['coder'][change]='unsupported';sha=digest(body)
    with pytest.raises(ValueError): resolve_snapshot(body,sha,registered)


def test_a_requires_explicit_claude_coder_and_preserves_other_roles():
    body=snapshot();body['profile']='A'
    body['roles']['coder'].update(runtime='claude_code',provider='explicit',model='registered-model',reasoning='none',billing='api')
    result=resolve_snapshot(body,digest(body),pins(body))
    assert result['coder'].configuration.model=='registered-model'
    assert result['reviewer'].configuration.model=='gpt-6-astra'
