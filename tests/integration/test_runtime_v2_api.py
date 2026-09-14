"""Real API -> durable Host -> real ADK/SDK, with synthetic HTTP only."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_api import engine,token_ring,keyring,_auth,NOW,FakeInterpreter,FakeDispatcher,DirectAnswer
from cap001_fixtures import CURSOR_KEY,IDENTIFIER_KEY
from envelope_factory import envelope_factory
from personal_agent.api.app import AgentApiDeps,build_app
from personal_agent.storage.engine import session_factory
from personal_agent.runtime.witnessed_model import WitnessedLiteLlm
from test_witnessed_model import wire
from test_adk_runtime import fc


def client_for(engine,token_ring,keyring,answers, *, tools=(), dispatcher=None):
    calls=[]
    def model(prepared):
        async def http(request):
            body=json.loads(request.content);calls.append(body)
            texts=[m['content'] for m in body['messages'] if m['role']=='user']
            context=json.loads(texts[-1].split('\n',1)[1])['task_context']
            answer=answers.pop(0)
            if isinstance(answer,str):
                data=wire(calls=[]);data['choices'][0]['finish_reason']='stop';data['choices'][0]['message']['content']=answer
                return httpx.Response(200,json=data)
            meta={'goal':context['current_input'],'source_refs':[context['current_user_source_ref']],'constraints':[]}
            batch=answer(context,meta) if callable(answer) else [fc('agent_finish','finish'+str(len(calls)),answer=answer,task=meta)]
            return httpx.Response(200,json=wire(calls=batch))
        return WitnessedLiteLlm(model='openai/synthetic',provider_name='zhipu',api_key='synthetic-key',binding=prepared.binding,transport=httpx.MockTransport(http))
    deps=AgentApiDeps(session_factory=session_factory(engine),token_ring=token_ring,keyring=keyring,
        identifier_key=IDENTIFIER_KEY,cursor_key=CURSOR_KEY,build_interpreter=lambda a:FakeInterpreter(DirectAnswer('legacy')),
        build_envelope=envelope_factory(keyring,tools=tools),build_dispatcher=lambda a,t:dispatcher or FakeDispatcher(),build_authorizer=lambda a:lambda **kw:kw['model_args'],
        capabilities=lambda a:[],now=lambda:NOW,v2_device_ids=frozenset({'dev-1'}),v2_model_factory=model)
    return TestClient(build_app(deps)),calls,deps


def answer(text='你好',kind='conversation'):
    return {'kind':kind,'text':text,'coverage':'complete','evidence_refs':[]}


def test_real_composition_conversation_and_idempotent_poll(engine,token_ring,keyring):
    client,calls,deps=client_for(engine,token_ring,keyring,[answer()])
    with client:
        headers={**_auth(token_ring),'X-Client-Wire-Version':'4'}
        response=client.post('/v1/chat/messages',headers=headers,json={'conversation_id':'c1','text':'你好'})
        assert response.status_code==200,response.text
        result=response.json();assert result['result_envelope']['text']=='你好'
        replay=client.post('/v1/chat/messages',headers=headers,json={'conversation_id':'c1','text':'你好'})
        assert replay.json()['operation_id']==result['operation_id'] and len(calls)==1


def test_v2_clarification_candidate_does_not_cancel_old_task_on_new_topic(engine,token_ring,keyring):
    from uuid import uuid4
    client,calls,deps=client_for(engine,token_ring,keyring,[answer('需要哪一天？','clarification'),answer('聊天')])
    with client:
        headers={**_auth(token_ring),'X-Client-Wire-Version':'4'}
        first=client.post('/v1/chat/messages',headers=headers,json={'conversation_id':'c1','text':'查询账目'}).json()
        source=first['operation_id']
        headers['Idempotency-Key']=str(uuid4())
        second=client.post('/v1/chat/messages',headers=headers,json={'conversation_id':'c1','text':'换个话题','clarification_of':source})
        assert second.status_code==200,second.text
        polled=client.get('/v1/operations/'+source,headers=headers)
        assert polled.json()['state']=='waiting_for_clarification'
        headers['X-Client-Wire-Version']='3';headers['Idempotency-Key']=str(uuid4())
        refused=client.post('/v1/chat/messages',headers=headers,json={'conversation_id':'c1','text':'继续','clarification_of':source})
        assert refused.status_code>=400


def test_sdk_read_then_analysis_uses_trusted_metric(engine,token_ring,keyring):
    from personal_agent.policy.bridge import VisibleTool
    from personal_agent.api.orchestrator import ReadCompleted
    from test_agent_api import _query_total_result,decode_finance_query_projection
    projection=decode_finance_query_projection(_query_total_result())
    tools=[VisibleTool('finance.query_expenses','query',{'type':'object'},'R1',())]
    def read(c,m):return [fc('finance_query_expenses','q',arguments={},task=m)]
    def finish(c,m):
        metric=c['metrics'][0]
        return [fc('agent_finish','end',task=m,answer={'kind':'analysis','coverage':'complete','evidence_refs':[metric['evidence_ref']],
            'analysis_nodes':[{'kind':'metric','metric_ref':metric['metric_ref']}],'commentary':''})]
    client,calls,_=client_for(engine,token_ring,keyring,[read,finish],tools=tools,
        dispatcher=FakeDispatcher(resolve=ReadCompleted(result='query',projection=projection)))
    with client:
        r=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'4'},json={'conversation_id':'c1','text':'查一下'})
        assert r.status_code==200,r.text
        assert '1200.00' in r.json()['result_envelope']['text'] and len(calls)==2


def test_sdk_write_flows_through_claim_and_existing_dispatcher(engine,token_ring,keyring):
    from personal_agent.policy.bridge import VisibleTool
    from personal_agent.api.orchestrator import Resolved,Written
    from personal_agent.api.intent import WriteIntent
    from sqlalchemy import select
    from personal_agent.storage.models import Base
    tools=[VisibleTool('finance.log_income','income',{'type':'object'},'R2',())]
    dispatcher=FakeDispatcher(resolve=Resolved(WriteIntent('finance.log_income',{})),commit=Written('synthetic-record'))
    def write(c,m):return [fc('finance_log_income','w',arguments={},task=m)]
    client,calls,deps=client_for(engine,token_ring,keyring,[write],tools=tools,dispatcher=dispatcher)
    with client:
        r=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'4'},json={'conversation_id':'c1','text':'收入工资'})
        assert r.status_code==200,r.text
        assert r.json()['state']=='succeeded',r.text
        assert len(dispatcher.commit_calls)==1 and len(calls)==1
        with deps.session_factory() as s:
            step=Base.metadata.tables['agent_run_steps']
            assert s.execute(select(step.c.status).where(step.c.kind=='write')).scalar_one()=='sent'


def test_cancel_clarification_revokes_task_and_late_authority(engine,token_ring,keyring):
    from sqlalchemy import select
    from personal_agent.storage.models import Base
    client,_,deps=client_for(engine,token_ring,keyring,[answer('哪一天？','clarification')])
    with client:
        headers={**_auth(token_ring),'X-Client-Wire-Version':'4'}
        first=client.post('/v1/chat/messages',headers=headers,json={'conversation_id':'c1','text':'查询'}).json()
        cancelled=client.delete('/v1/operations/'+first['operation_id'],headers=headers)
        assert cancelled.status_code==200,cancelled.text
        assert cancelled.json()['state']=='cancelled_pre_submit'
        with deps.session_factory() as s:
            tasks=Base.metadata.tables['agent_tasks']
            assert s.execute(select(tasks.c.status)).scalar_one()=='cancelled'


def test_sdk_calendar_plan_uses_frozen_existing_actions(engine,token_ring,keyring):
    from personal_agent.policy.bridge import VisibleTool
    from personal_agent_core.tool_ir import CALENDAR_CREATE_EVENT
    from test_calendar_device_action import cal_dispatcher,SpyBridge,CAL_ARGS
    from sqlalchemy import select
    from personal_agent.storage.models import Operation
    tools=[VisibleTool('calendar.create_event','calendar',CALENDAR_CREATE_EVENT.model_input_schema,'R2',())]
    def write(c,m):return [fc('calendar_create_event','plan',items=[CAL_ARGS,{**CAL_ARGS,'title':'第二场'}],task=m)]
    client,calls,deps=client_for(engine,token_ring,keyring,[write],tools=tools,dispatcher=cal_dispatcher(SpyBridge(),client_wire_version=4))
    deps.action_keyring=keyring
    with client:
        r=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'4'},json={'conversation_id':'c1','text':'两个日程'})
        assert r.status_code==202,r.text
        assert r.json()['state']=='source_in_progress',r.text
        with deps.session_factory() as s:
            states=s.execute(select(Operation.state)).scalars().all()
            assert states==['source_in_progress','source_in_progress']
        assert len(calls)==1


def test_full_catalog_leaves_room_for_context():
    from personal_agent_core.tool_ir import TOOL_CONTRACTS
    from personal_agent.runtime.run_catalog import catalog
    from personal_agent.runtime.prompt import build_system_prompt,business_rules
    from personal_agent.runtime.answers import canonical
    declarations=[{'function':{'name':t.name,'description':t.summary,'parameters':t.model_input_schema}} for t in TOOL_CONTRACTS if t.enabled and t.model_callable]
    specs=catalog(declarations)
    from google.genai import types
    tools=[types.Tool(function_declarations=[types.FunctionDeclaration(name=s.name,description=s.description,parameters_json_schema=s.schema) for s in specs])]
    available={s.business_name for s in specs if not s.business_name.startswith('agent.')}
    capability_line='\n本轮工具清单：'+','.join(sorted(available))+'\n'
    size=len((capability_line+canonical([t.model_dump(mode='json',exclude_none=True) for t in tools])+build_system_prompt(today='2026-09-14',runtime_v2=True)+business_rules('finance.',today='2026-09-14')+business_rules('calendar.',today='2026-09-14')).encode())
    assert size+4000<=24000,size
    # A real follow-up must fit search results too, not only the empty catalog.
    from personal_agent.runtime.web_projection import model_results
    sources = [{'kind':'web_source','ref':'web_'+str(i)*64,'source_ref':'web_'+str(i)*64,
                'provider_request_id':'11111111-1111-4111-8111-111111111111',
                'title':'Official documentation', 'url':f'https://docs.example.org/library/{i}',
                'snippet':'s'*2000,'content':'c'*8000,'content_present':True,'truncated':False}
               for i in range(5)]
    raw = len(canonical([{'sources':sources}]).encode())
    bounded = len(canonical(model_results([{'sources':sources}])).encode())
    assert size + 512 + raw > 24000
    assert size + 512 + bounded + 1000 <= 24000



def test_automatic_task_selection_closes_only_selected_source(engine,token_ring,keyring):
    from uuid import uuid4
    def resume(c,m):
        old=c['candidates'][0]
        m.update(task_ref=old['task_ref'],constraints=old['constraints'])
        return [fc('agent_finish','continued',task=m,answer=answer('接续完成'))]
    client,calls,_=client_for(engine,token_ring,keyring,[answer('日期？','clarification'),resume])
    with client:
        h={**_auth(token_ring),'X-Client-Wire-Version':'4'}
        first=client.post('/v1/chat/messages',headers=h,json={'conversation_id':'c1','text':'查账'}).json()
        h['Idempotency-Key']=str(uuid4())
        second=client.post('/v1/chat/messages',headers=h,json={'conversation_id':'c1','text':'九月'})
        assert second.status_code==200,second.text
        assert client.get('/v1/operations/'+first['operation_id'],headers=h).json()['state']=='cancelled_pre_submit'


def test_by_category_metrics_do_not_parse_missing_total():
    from personal_agent.api.finance_query_projection import FinanceQueryProjection,QueryCategoryBucket
    from personal_agent.runtime.answers import EvidenceCatalog
    p=FinanceQueryProjection(view='by_category',metric='personal_spend_cny',record_count=1,filters_applied={'category':'餐饮'},source_system='synthetic',evidence={},by_category=(QueryCategoryBucket('餐饮','12',1,None),))
    c=EvidenceCatalog();c.add_query('q',p)
    assert any(m.metric_kind=='category_total' and m.value_decimal=='12' for m in c.metrics.values())


def test_model_cancel_closes_waiting_operation_without_charging_user_wait(engine,token_ring,keyring):
    from uuid import uuid4
    def cancel(c,m):return [fc('agent_task_control','cancel',task_ref=c['candidates'][0]['task_ref'],action='cancel',source_refs=[c['current_user_source_ref']])]
    client,_,deps=client_for(engine,token_ring,keyring,[answer('哪天？','clarification'),cancel,answer('已取消待办')])
    with client:
        h={**_auth(token_ring),'X-Client-Wire-Version':'4'}
        first=client.post('/v1/chat/messages',headers=h,json={'conversation_id':'c1','text':'查询'}).json()
        h['Idempotency-Key']=str(uuid4())
        r=client.post('/v1/chat/messages',headers=h,json={'conversation_id':'c1','text':'取消刚才的查询'})
        assert r.status_code==200,r.text
        assert client.get('/v1/operations/'+first['operation_id'],headers=h).json()['state']=='cancelled_pre_submit'


@pytest.mark.parametrize('full_catalog', [False, True])
def test_search_is_wired_through_sdk_host_and_sealed_audit(engine,token_ring,keyring,full_catalog):
    from personal_agent.search.adapter import SearchAdapter,SearchConfig
    from personal_agent.storage.models import Device,Base
    from sqlalchemy import select
    from uuid import uuid4
    def search(c,m):return [fc('search_web','search',arguments={'query':'public documentation'},task=m)]
    def finish(c,m):
        ref=c['completed_results'][0]['sources'][0]['source_ref']
        return [fc('agent_finish','finish',task=m,answer={'kind':'analysis','coverage':'complete','evidence_refs':[ref],'analysis_nodes':[{'kind':'web_claim','text':'合成公开信息','source_refs':[ref]}],'commentary':''})]
    from personal_agent_core.tool_ir import TOOL_CONTRACTS
    from personal_agent.policy.bridge import VisibleTool
    tools = [VisibleTool(t.name,t.summary,t.model_input_schema,t.risk_level,t.required_scopes)
             for t in TOOL_CONTRACTS if t.enabled and t.model_callable and not t.name.startswith('search.')] if full_catalog else []
    client,calls,deps=client_for(engine,token_ring,keyring,[search,finish],tools=tools)
    with deps.session_factory() as s:
        d=s.get(Device,'dev-1');scopes=json.loads(d.scopes);scopes.append('public_web.read');d.scopes=json.dumps(scopes);s.commit()
    requests=[]
    def transport(req):
        requests.append(req)
        return httpx.Response(200,json={'code':0,'request_id':str(uuid4()),'data':{'results':[{'title':'Public','url':'https://example.org/','snippet':'Synthetic '*200,'content':'Public content '*500} for _ in range(5 if full_catalog else 1)]}})
    deps.v2_search_allowed=lambda auth,tool:True
    deps.v2_search_adapter=SearchAdapter(SearchConfig(enabled=True,auth_mode='anonymous'),transport=httpx.MockTransport(transport),resolve=lambda host:['8.8.8.8'])
    with client:
        r=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'4'},json={'conversation_id':'c1','text':'搜索公开文档'})
        assert r.status_code==200,r.text
        assert r.json()['result_envelope']['analysis_nodes'][0]['sources'][0]['url']=='https://example.org/'
        assert len(requests)==1 and len(calls)==2
        with deps.session_factory() as s:
            steps=Base.metadata.tables['agent_run_steps']
            assert s.execute(select(steps.c.sealed_args).where(steps.c.call_id=='search')).scalar_one() is not None


def test_plain_model_reply_gets_only_one_bounded_format_retry(engine,token_ring,keyring):
    client,calls,deps=client_for(engine,token_ring,keyring,['untrusted prose',answer('合法结束')])
    with client:
        r=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'4'},json={'conversation_id':'c1','text':'你好'})
        assert r.status_code==200,r.text
        assert r.json()['result_envelope']['text']=='合法结束' and len(calls)==2


def test_pending_duplicate_holds_task_write_slot_until_human_decision(engine,token_ring,keyring):
    from personal_agent.policy.bridge import VisibleTool
    from personal_agent.api.orchestrator import PossibleDuplicate,Written
    from personal_agent.api.intent import WriteIntent
    from personal_agent.storage.models import Base
    from sqlalchemy import select
    from uuid import uuid4
    from personal_agent.runtime.run_repository import RunRepository
    dispatcher=FakeDispatcher(resolve=PossibleDuplicate('v2-dup',WriteIntent('finance.log_income',{}),'合成重复记录'),commit=Written('synthetic'))
    def write(c,m):return [fc('finance_log_income','write',arguments={},task=m)]
    client,calls,deps=client_for(engine,token_ring,keyring,[write],tools=[VisibleTool('finance.log_income','income',{'type':'object'},'R2',())],dispatcher=dispatcher)
    with client:
        h={**_auth(token_ring),'X-Client-Wire-Version':'4'}
        r=client.post('/v1/chat/messages',headers=h,json={'conversation_id':'c1','text':'收入'})
        assert r.status_code==202,r.text
        with deps.session_factory() as s:
            tasks=Base.metadata.tables['agent_tasks']
            assert s.execute(select(tasks.c.write_slot)).scalar_one() is not None
        h['Idempotency-Key']=str(uuid4())
        result=client.post('/v1/duplicate-checks/v2-dup/decision',headers=h,json={'decision':'write_anyway'})
        assert result.status_code==200,result.text
        assert len(calls)==1 and len(dispatcher.commit_calls)==1
        RunRepository(deps.session_factory,keyring).sweep(now_ms=round(NOW.timestamp()*1000))
        with deps.session_factory() as s:
            assert s.execute(select(tasks.c.write_slot)).scalar_one() is None


def test_completed_read_is_reused_instead_of_dispatched_again(engine,token_ring,keyring):
    from personal_agent.policy.bridge import VisibleTool
    from personal_agent.api.orchestrator import ReadCompleted
    from test_agent_api import _query_total_result,decode_finance_query_projection
    class CountedDispatcher(FakeDispatcher):
        reads=0
        def resolve(self, *, tool,model_args,idempotency_key):
            self.reads+=1
            return super().resolve(tool=tool,model_args=model_args,idempotency_key=idempotency_key)
    dispatcher=CountedDispatcher(resolve=ReadCompleted(result='query',projection=decode_finance_query_projection(_query_total_result())))
    def read(c,m):return [fc('finance_query_expenses','q'+str(len(c['completed_results'])),arguments={},task=m)]
    client,calls,_=client_for(engine,token_ring,keyring,[read,read,answer('已取得结果')],tools=[VisibleTool('finance.query_expenses','query',{'type':'object'},'R1',())],dispatcher=dispatcher)
    with client:
        r=client.post('/v1/chat/messages',headers={**_auth(token_ring),'X-Client-Wire-Version':'4'},json={'conversation_id':'c1','text':'查询'})
        assert r.status_code==200,r.text
        assert dispatcher.reads==1


def test_business_clarification_and_source_replacement_rollback_together(engine,token_ring,keyring,monkeypatch):
    from personal_agent.policy.bridge import VisibleTool
    from personal_agent.api.orchestrator import NeedsClarification
    from personal_agent.api import events
    from uuid import uuid4
    def write(c,m):
        m['task_ref']=c['candidates'][0]['task_ref']
        return [fc('finance_log_income','write',task=m,arguments={})]
    client,_,deps=client_for(engine,token_ring,keyring,[answer('事项？','clarification'),write],tools=[VisibleTool('finance.log_income','income',{'type':'object'},'R2',())],dispatcher=FakeDispatcher(resolve=NeedsClarification('金额？')))
    with client:
        h={**_auth(token_ring),'X-Client-Wire-Version':'4'}
        first=client.post('/v1/chat/messages',headers=h,json={'conversation_id':'c1','text':'记收入'}).json()
        original=events.append_event
        def fail_new_clarification(*args,**kwargs):
            envelope=kwargs.get('content',{}).get('result_envelope',{})
            if envelope.get('kind')=='clarification':raise RuntimeError('synthetic result transaction crash')
            return original(*args,**kwargs)
        monkeypatch.setattr(events,'append_event',fail_new_clarification)
        h['Idempotency-Key']=str(uuid4())
        result=client.post('/v1/chat/messages',headers=h,json={'conversation_id':'c1','text':'工资','clarification_of':first['operation_id']})
        assert result.status_code==200,result.text
        assert result.json()['result_envelope']['kind']=='limitation'
        assert client.get('/v1/operations/'+first['operation_id'],headers=h).json()['state']=='waiting_for_clarification'
