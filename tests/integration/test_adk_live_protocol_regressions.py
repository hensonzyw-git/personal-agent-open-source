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


def test_readonly_prompt_matches_admitted_catalog(engine,token_ring,keyring):
    client,calls,_=client_for(engine,token_ring,keyring,[answer()],tools=QUERY)
    with client:
        send(client,token_ring,'你好')
    system='\n'.join(m['content'] for m in calls[0]['messages'] if m['role']=='system')
    assert '本轮工具清单：finance.query_expenses\n' in system
    names={t['function']['name'] for t in calls[0]['tools']}
    assert 'finance_query_expenses' in names
    assert not any(n.startswith(('finance_log','calendar_')) for n in names)
    assert 'finance.log_expense：' not in system
    assert 'finance.log_income：' not in system
    assert 'finance.update_family_fund：' not in system
    assert '日期缺失默认今天' not in system
    assert 'view/cursor 只放在 arguments' in system


def test_original_view_constraint_is_still_rejected():
    from personal_agent.runtime.task_contracts import validate_evidence_scope
    from personal_agent.runtime.answers import AnswerError
    data=json.loads((Path(__file__).resolve().parents[2]/'docs/evidence/adk_supplement_live_20260914.json').read_text())
    row=next(r for r in data['rows'] if r['case']=='analysis' and r['repeat']==3)
    metadata=json.loads(row['trace'][0]['tool_calls'][0][0]['function']['arguments'])['task']
    with pytest.raises(AnswerError,match='evidence_scope_mismatch'):
        validate_evidence_scope(row['response']['result_envelope']['evidence'][0],metadata)


def test_batch_only_keeps_expense_rules_without_income_rules():
    from personal_agent.runtime.prompt import business_rules
    rules=business_rules('finance.',today='2026-09-14',available_tools={'finance.log_expense_batch'})
    assert 'finance.log_expense：' in rules
    assert '不默认、不继承历史' in rules
    assert 'finance.log_income：' not in rules
    assert 'finance.update_family_fund：' not in rules
