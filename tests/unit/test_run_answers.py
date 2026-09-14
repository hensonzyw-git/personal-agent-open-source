import pytest
import copy
from personal_agent.runtime.answers import EvidenceCatalog, AnswerError


def filters(month):
    return {'date_range':{'start':f'2026-{month}-01','end':f'2026-{month}-28'},
            'categories':[], 'name_contains':[], 'is_family_expense':'all', 'personal_amount_cny':None}


def test_comparison_labels_and_numbers_are_rendered_by_host():
    c = EvidenceCatalog()
    a = c.metric('q1', 'total', '120', '元', 'CNY', filters('09'))
    b = c.metric('q2', 'total', '100', '元', 'CNY', filters('08'))
    c.comparisons['cmp'] = {'metric_kind':'total', 'current':filters('09'), 'baseline':filters('08')}
    node = {'kind':'comparison','current_metric_ref':a,'baseline_metric_ref':b,'comparison_ref':'cmp'}
    out = c.answer({'kind':'analysis','coverage':'complete','evidence_refs':['q1','q2'],'analysis_nodes':[node],'commentary':''})
    assert '增加 20 元' in out['text']
    node['current_metric_ref'],node['baseline_metric_ref']=b,a
    with pytest.raises(AnswerError): c.answer({'kind':'analysis','coverage':'complete','evidence_refs':['q1','q2'],'analysis_nodes':[node],'commentary':''})


@pytest.mark.parametrize('bad', [
 {'kind':'metric','metric_ref':'unknown'},
 {'kind':'metric','metric_ref':'known','value':'99'},
 {'kind':'web_claim','text':'claim','source_refs':['unknown']},
])
def test_analysis_rejects_untrusted_facts(bad):
    with pytest.raises(AnswerError):
        EvidenceCatalog().answer({'kind':'analysis','coverage':'complete','evidence_refs':[], 'analysis_nodes':[bad],'commentary':''})


@pytest.mark.parametrize('amount', [None, {'min':'10.00'}, {'max':'90.00'}, {'min':'10.00','min_inclusive':False}])
def test_comparison_defaults_match_real_finance_filter_projection(amount):
    # The existing Finance parser is the counterparty, not the renderer's helper.
    from personal_data_mcp.finance.query_expenses import _parse_filters, _serialise_filters
    requests = [{'date_range':filters(month)['date_range']} for month in ('09','08')]
    if amount is not None:
        for request in requests: request['personal_amount_cny'] = copy.deepcopy(amount)
    original = copy.deepcopy(requests)
    c = EvidenceCatalog()
    refs = [c.metric('q'+str(i), 'total', value, '元', 'CNY', _serialise_filters(_parse_filters(request)))
            for i, (request, value) in enumerate(zip(requests, ('120','100')))]
    c.comparisons['cmp'] = {'metric_kind':'total', 'current':requests[0], 'baseline':requests[1]}
    node = {'kind':'comparison', 'comparison_ref':'cmp', 'current_metric_ref':refs[0], 'baseline_metric_ref':refs[1]}
    assert c.node(node)['difference_decimal'] == '20'
    assert requests == original


@pytest.mark.parametrize('mismatch', ['date_range','categories','name_contains','is_family_expense','amount_boundary','category','missing_evidence_field','partial','unknown_request_field','explicit_null','missing_category'])
def test_comparison_default_expansion_keeps_evidence_strict(mismatch):
    requested = [filters('09'), filters('08')]
    for request in requested:
        request['personal_amount_cny'] = {'min':'10.00','max':None,'min_inclusive':True,'max_inclusive':True}
        request['category'] = '餐饮'
    actual = copy.deepcopy(requested)
    if mismatch == 'date_range': actual[0]['date_range'] = actual[1]['date_range']
    if mismatch == 'categories': actual[0]['categories'] = ['餐饮']
    if mismatch == 'name_contains': actual[0]['name_contains'] = ['#合成旅行']
    if mismatch == 'is_family_expense': actual[0]['is_family_expense'] = 'false'
    if mismatch == 'amount_boundary': actual[0]['personal_amount_cny']['min_inclusive'] = False
    if mismatch == 'category': actual[0]['category'] = '购物'
    if mismatch == 'missing_evidence_field': actual[0].pop('name_contains')
    if mismatch == 'unknown_request_field': requested[0]['unknown'] = 'synthetic'
    if mismatch == 'explicit_null': requested[0]['categories'] = None
    if mismatch == 'missing_category': requested[0].pop('category')
    c = EvidenceCatalog()
    refs = [c.metric('q'+str(i), 'category_total', value, '元', 'CNY', actual[i], 'partial' if mismatch == 'partial' and i == 0 else 'complete')
            for i, value in enumerate(('120','100'))]
    c.comparisons['cmp'] = {'metric_kind':'category_total', 'current':requested[0], 'baseline':requested[1]}
    with pytest.raises(AnswerError, match='evidence_scope_mismatch'):
        c.node({'kind':'comparison','comparison_ref':'cmp','current_metric_ref':refs[0],'baseline_metric_ref':refs[1]})
