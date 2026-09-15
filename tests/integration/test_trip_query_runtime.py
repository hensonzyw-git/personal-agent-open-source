import pytest
from dataclasses import replace
from test_agent_api import engine, token_ring, keyring, _auth
from test_runtime_v2_api import client_for, answer
from test_adk_runtime import fc
from test_finance_query_pagination_v2 import QueryDispatcher
from personal_agent_core.tool_ir import QUERY_EXPENSES
from personal_agent.policy.bridge import VisibleTool
from personal_agent.api.app import build_app
from fastapi.testclient import TestClient
from personal_agent.runtime.run_repository import RunRepository
from personal_agent.runtime.trip_queries import explicit_breakdown

TOOLS=[VisibleTool('finance.query_expenses',QUERY_EXPENSES.summary,QUERY_EXPENSES.model_input_schema,'R1',())]
DATES={'start':'2026-01-01','end':'2026-12-31'}
TEXT='查询一下我今年所有旅行支出，按照目的地做聚合统计，告诉我不同目的地分别花了多少钱'


def test_trip_is_completed_without_second_model_and_v4_history_downgrades(engine,token_ring,keyring):
    def query(c,m):
        m['query_requirement']={'domain':'finance','result_kind':'trip_breakdown','trip_tag':None,'date_range':DATES,'source_refs':m['source_refs']}
        return [fc('finance_query_expenses','q',arguments={'view':'by_trip','date_range':DATES},task=m)]
    _,calls,deps=client_for(engine,token_ring,keyring,[query],tools=TOOLS,dispatcher=QueryDispatcher())
    deps=replace(deps,trip_query_enabled=True)
    with TestClient(build_app(deps)) as client:
        result=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'5'},json={'conversation_id':'c1','text':TEXT}).json()
        assert result['state']=='succeeded',result
        a=result['result_envelope']
        assert a['kind']=='query' and a['coverage']=='complete'
        assert a['evidence'][0]['query_result']['view']=='by_trip'
        assert len(calls)==1
        polled=client.get('/v1/operations/'+result['operation_id'],headers={**_auth(token_ring),'X-Client-Wire-Version':'4'}).json()['result_envelope']
        assert polled['evidence']==[] and '旅行场次' in polled['text']


@pytest.mark.parametrize('omit_requirement',[False,True])
def test_original_request_cannot_finish_with_category_or_plain_prose(engine,token_ring,keyring,omit_requirement):
    def wrong(c,m):
        if not omit_requirement:m['query_requirement']={'domain':'finance','result_kind':'trip_breakdown','trip_tag':None,'date_range':DATES,'source_refs':m['source_refs']}
        return [fc('agent_finish','q',answer=answer('统计完成'),task=m)]
    _,_,deps=client_for(engine,token_ring,keyring,[wrong],tools=TOOLS)
    with TestClient(build_app(replace(deps,trip_query_enabled=True))) as client:
        result=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'5'},json={'conversation_id':'c1','text':TEXT}).json()
        assert result['state']=='failed_safe',result
        assert result['result_envelope']['failure']['code'] in {'trip_requirement_missing','trip_summary_required'}


def test_no_date_trip_total_is_partial_and_uses_one_model(engine,token_ring,keyring):
    def query(c,m):
        m['query_requirement']={'domain':'finance','result_kind':'trip_total','trip_tag':'东京01','date_range':None,'source_refs':m['source_refs']}
        return [fc('finance_query_expenses','q',arguments={'view':'total','trip_tag':'东京01'},task=m)]
    _,calls,deps=client_for(engine,token_ring,keyring,[query],tools=TOOLS,dispatcher=QueryDispatcher())
    with TestClient(build_app(replace(deps,trip_query_enabled=True))) as client:
        result=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'5'},json={'conversation_id':'c1','text':'东京01总共花了多少钱'}).json()
        assert result['result_envelope']['coverage']=='partial',result
        assert '小计' in result['result_envelope']['text']
        assert len(calls)==1


@pytest.mark.parametrize('wire,enabled', [('4',True),('5',False)])
def test_new_trip_call_cannot_bypass_admission(engine,token_ring,keyring,wire,enabled):
    def query(c,m):return [fc('finance_query_expenses','q',arguments={'view':'by_trip','date_range':DATES},task=m)]
    spy=QueryDispatcher()
    _,_,deps=client_for(engine,token_ring,keyring,[query],tools=TOOLS,dispatcher=spy)
    with TestClient(build_app(replace(deps,trip_query_enabled=enabled))) as client:
        result=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':wire},json={'conversation_id':'c1','text':TEXT}).json()
        assert result['state']=='failed_safe'
        assert spy.calls==[]


def test_requirement_cannot_be_dropped_or_changed():
    from personal_agent.runtime.trip_queries import freeze_requirement
    from personal_agent.runtime.run_store import RunStateError
    old={'query_requirement':{'result_kind':'trip_breakdown'}}
    assert freeze_requirement({},old,'继续')['query_requirement']==old['query_requirement']
    with pytest.raises(RunStateError):freeze_requirement({'query_requirement':{'result_kind':'trip_total'}},old,'继续')


def test_partial_trip_result_still_must_match_family_filter():
    from personal_agent.runtime.trip_queries import check_completion
    from personal_agent.runtime.answers import AnswerError
    req={'domain':'finance','result_kind':'trip_total','trip_tag':'东京01','date_range':None,'source_refs':['synthetic']}
    metadata={'query_requirement':req,'constraints':[{'key':'is_family_expense','value':True,'source_refs':['synthetic']}]}
    card={'tool':'finance.query_expenses','query_result':{'view':'total','filters_applied':{'trip_tag':'东京01','date_range':None,'is_family_expense':'false'},'coverage':{'scan_complete':True,'scope_coverage':'unknown'}}}
    with pytest.raises(AnswerError):check_completion({'kind':'query','coverage':'partial','evidence':[card]},metadata)


def test_multi_read_batch_keeps_all_results_before_finish(engine,token_ring,keyring):
    def queries(c,m):
        m['query_requirement']={'domain':'finance','result_kind':'trip_breakdown','trip_tag':None,'date_range':DATES,'source_refs':m['source_refs']}
        return [fc('finance_query_expenses','a',arguments={'view':'by_trip','date_range':DATES},task=m),
                fc('finance_query_expenses','b',arguments={'view':'total','date_range':DATES,'categories':['旅行']},task=m)]
    def finish(c,m):
        m.update(c['bound_task'])
        return [fc('agent_finish','end',task=m,answer={'kind':'limitation','text':'两次查询结果保留。','coverage':'partial','evidence_refs':c['tool_evidence_refs']})]
    spy=QueryDispatcher()
    _,calls,deps=client_for(engine,token_ring,keyring,[queries,finish],tools=TOOLS,dispatcher=spy)
    with TestClient(build_app(replace(deps,trip_query_enabled=True))) as client:
        r=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'5'},json={'conversation_id':'c1','text':TEXT}).json()
        assert len(spy.calls)==2,r
        assert len(calls)==2
        assert len(r['result_envelope']['evidence'])==2
