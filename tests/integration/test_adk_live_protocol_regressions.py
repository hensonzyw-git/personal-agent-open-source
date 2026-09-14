"""Production request projection and original live failure shapes, offline."""
import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from test_agent_api import engine, token_ring, keyring, FakeDispatcher, _query_total_result, decode_finance_query_projection
from test_runtime_v2_api import client_for, answer
from test_adk_review_regressions import send, QUERY
from test_adk_runtime import fc
from personal_agent.api.orchestrator import ReadCompleted
from personal_agent.runtime.run_repository import RunRepository
from personal_agent.runtime.run_catalog import catalog


def test_finish_declaration_rejects_missing_required_task_fields():
    spec=next(s for s in catalog([]) if s.name=='agent_finish')
    validator=Draft202012Validator(spec.schema)
    meta={'goal':'解释幂等','source_refs':['synthetic-user'],'constraints':[]}
    assert validator.is_valid({'answer':answer(),'task':meta})
    for field in meta:
        missing={k:v for k,v in meta.items() if k!=field}
        assert not validator.is_valid({'answer':answer(),'task':missing}),field


def test_read_followup_exposes_real_task_handle_and_tool_evidence_namespace(engine,token_ring,keyring):
    observed={}
    def read(c,m):
        assert c['tool_evidence_refs']==[]
        return [fc('finance_query_expenses','read',arguments={},task=m)]
    def finish(c,m):
        observed.update(c)
        task=c['bound_task']
        assert task['task_ref'].startswith('task_')
        assert c['tool_evidence_refs']==[c['completed_results'][0]['ref']]
        m['task_ref']=task['task_ref']
        return [fc('agent_finish','finish',task=m,answer=answer('查询结果已返回'))]
    client,_,deps=client_for(engine,token_ring,keyring,[read,finish],tools=QUERY,
        dispatcher=FakeDispatcher(resolve=ReadCompleted(result='synthetic',projection=decode_finance_query_projection(_query_total_result()))))
    with client:
        response=send(client,token_ring,'查询').json()
        assert response['result_envelope']['kind']=='conversation'
        repo=RunRepository(deps.session_factory,keyring)
        assert observed['bound_task']['task_ref']==repo.snapshot(response['operation_id'])['task_id']


@pytest.mark.parametrize('shape',['user_ref_as_evidence','missing_constraints','invented_task'])
def test_original_invalid_shapes_still_fail_without_dispatch(engine,token_ring,keyring,shape):
    def bad(c,m):
        a=answer('请提供日期','clarification')
        if shape=='user_ref_as_evidence':a['evidence_refs']=m['source_refs']
        if shape=='missing_constraints':m.pop('constraints')
        if shape=='invented_task':m['task_ref']='bound_task'
        return [fc('agent_finish','bad',task=m,answer=a)]
    class Spy(FakeDispatcher):
        resolve_calls=[]
        def resolve(self,**kwargs):
            self.resolve_calls.append(kwargs)
            return super().resolve(**kwargs)
    dispatcher=Spy()
    client,_,_=client_for(engine,token_ring,keyring,[bad],dispatcher=dispatcher)
    with client:
        result=send(client,token_ring,'请继续').json()
        assert result['result_envelope']['kind']=='limitation'
        assert result['result_envelope'].get('failure')
        assert dispatcher.resolve_calls==[] and dispatcher.commit_calls==[]
