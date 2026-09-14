"""Comparison protocol regressions through real ADK/API, synthetic HTTP only."""
import copy

import pytest
from jsonschema import Draft202012Validator
from test_agent_api import engine, token_ring, keyring, _query_total_result, decode_finance_query_projection
from test_runtime_v2_api import client_for, answer
from test_adk_review_regressions import send
from test_adk_runtime import fc
from personal_agent.api.orchestrator import ReadCompleted
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.run_catalog import TASK_VALIDATION
from personal_agent.runtime.run_repository import RunRepository
from personal_agent_core.tool_ir import QUERY_EXPENSES


TOOLS = [VisibleTool(QUERY_EXPENSES.name, QUERY_EXPENSES.summary, QUERY_EXPENSES.model_input_schema, 'R1', ())]


def test_first_outbound_request_explains_comparison_contract(engine, token_ring, keyring):
    client, calls, _ = client_for(engine, token_ring, keyring, [answer()], tools=TOOLS)
    with client:
        send(client, token_ring, '比较两个时间段')
    system = '\n'.join(m['content'] for m in calls[0]['messages'] if m['role'] == 'system')
    comparison = TASK_VALIDATION['properties']['comparisons']['items']
    for kind in comparison['properties']['metric_kind']['enum']:
        assert kind in system
    for field in comparison['required']:
        assert field in system
    assert 'filters' in system and 'view/cursor' in system
    assert 'category_total' in system and 'category' in system


@pytest.mark.parametrize('metric_kind', ['total', 'count', 'category_total'])
@pytest.mark.parametrize('sparse', [True, False])
def test_comparison_with_default_filters_completes_without_rewriting_contract(engine, token_ring, keyring, metric_kind, sparse):
    dates = [{'start':'2026-09-01', 'end':'2026-09-30'}, {'start':'2026-08-01', 'end':'2026-08-31'}]
    projections = []
    for i, date_range in enumerate(dates):
        data = _query_total_result()
        data['filters_applied'].update(date_range=date_range, categories=[])
        data['personal_spend_total_cny'] = ['120.00', '100.00'][i]
        data['record_count'] = [3, 2][i]
        if metric_kind == 'category_total':
            data.update(view='by_category', by_category=[{'category':'餐饮', 'personal_spend_total_cny':data['personal_spend_total_cny'], 'record_count':data['record_count'], 'share_of_total':'1'}])
        projections.append(data)
    reads, requested, frozen = [], [], []

    class Dispatcher:
        def resolve(self, **kwargs):
            args = kwargs['model_args']
            reads.append(args)
            return ReadCompleted(result='synthetic comparison', projection=decode_finance_query_projection(projections[dates.index(args['date_range'])]))

    def read(context, metadata):
        filters = [dict(date_range=d) for d in dates] if sparse else [copy.deepcopy(p['filters_applied']) for p in projections]
        if metric_kind == 'category_total':
            for f in filters: f['category'] = '餐饮'
        metadata['comparisons'] = [{'metric_kind':metric_kind, 'current':filters[0], 'baseline':filters[1], 'source_refs':metadata['source_refs']}]
        assert Draft202012Validator(TASK_VALIDATION).is_valid(metadata)
        requested.append(copy.deepcopy(metadata['comparisons'][0]))
        return [fc('finance_query_expenses', 'q'+str(i), arguments={'view':projections[i]['view'], 'date_range':d}, task=metadata) for i, d in enumerate(dates)]

    def finish(context, metadata):
        metadata = copy.deepcopy(context['bound_task'])
        frozen.append(copy.deepcopy(metadata['comparisons'][0]))
        metrics = {m['evidence_ref']:m for m in context['metrics'] if m['metric_kind'] == metric_kind}
        node = {'kind':'comparison', 'comparison_ref':frozen[0]['comparison_ref'],
                'current_metric_ref':metrics['query_q0']['metric_ref'], 'baseline_metric_ref':metrics['query_q1']['metric_ref']}
        return [fc('agent_finish', 'finish', task=metadata, answer={'kind':'analysis', 'analysis_nodes':[node], 'commentary':'', 'coverage':'complete', 'evidence_refs':context['tool_evidence_refs']})]

    client, calls, deps = client_for(engine, token_ring, keyring, [read, finish], tools=TOOLS, dispatcher=Dispatcher())
    with client:
        response = send(client, token_ring, '比较九月与八月支出').json()
    result = response['result_envelope']
    assert result['kind'] == 'analysis', result
    assert result['coverage'] == 'complete' and not result.get('failure')
    assert result['analysis_nodes'][0]['difference_decimal'] == ('1' if metric_kind == 'count' else '20.00')
    assert len(reads) == len(calls) == 2
    assert {k:v for k,v in frozen[0].items() if k != 'comparison_ref'} == requested[0]
    repo = RunRepository(deps.session_factory, keyring)
    snapshot = repo.snapshot(response['operation_id'])
    metadata = repo.open('agent_runs', 'sealed_input_snapshot', response['operation_id'], snapshot['sealed_input_snapshot'])['task_metadata']
    assert metadata['comparisons'] == frozen
    assert snapshot['llm_used'] == snapshot['read_used'] == 2


@pytest.mark.parametrize('bad', ['unknown_metric', 'null_categories', 'null_inclusive', 'unknown_filter'])
def test_invalid_comparison_metadata_still_rejects_entire_read_batch(engine, token_ring, keyring, bad):
    executed = []

    class Dispatcher:
        def resolve(self, **kwargs):
            executed.append(kwargs)
            raise AssertionError('invalid batch must not dispatch')

    def read(context, metadata):
        date_range = {'start':'2026-09-01','end':'2026-09-30'}
        request = {'metric_kind':'total','current':{'date_range':date_range},'baseline':{'date_range':date_range},'source_refs':metadata['source_refs']}
        if bad == 'unknown_metric': request['metric_kind'] = 'sum'
        if bad == 'null_categories': request['current']['categories'] = None
        if bad == 'null_inclusive': request['current']['personal_amount_cny'] = {'min':'10.00','min_inclusive':None}
        if bad == 'unknown_filter': request['current']['guessed_filter'] = 'synthetic'
        metadata['comparisons'] = [request]
        return [fc('finance_query_expenses', 'q'+str(i), arguments={'view':'total','date_range':date_range}, task=metadata) for i in range(2)]

    client, calls, _ = client_for(engine, token_ring, keyring, [read], tools=TOOLS, dispatcher=Dispatcher())
    with client:
        result = send(client, token_ring, '比较九月与八月').json()['result_envelope']
    assert result['failure']['code'] == 'invalid_task_metadata'
    assert executed == [] and len(calls) == 1
