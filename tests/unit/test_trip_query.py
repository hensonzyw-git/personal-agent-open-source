"""Synthetic trip aggregation contracts; never uses the personal ledger."""
import pytest
from test_query_expenses import query_with, record
from personal_agent_core.errors import AppError


def test_trip_aggregation_scans_all_pages_and_keeps_signed_groups():
    rows = [record(f'r{i}', name='hotel #东京01', category='旅行', personal_spend='10') for i in range(51)]
    rows += [record('refund', name='refund #东京01', category='旅行', personal_spend='-510'),
             record('second', name='hotel #东京 02', category='旅行', personal_spend='30'),
             record('fx', name='flight #东京02（10,000 JPY）', category='旅行', personal_spend='20'),
             record('missing', name='hotel', category='旅行', personal_spend='5')]
    result, _ = query_with([{'items':rows[:30], 'has_more':True, 'page_token':'next'}, {'items':rows[30:], 'has_more':False}],
                          {'view':'by_trip', 'date_range':{'start':'2026-01-01','end':'2026-12-31'}})
    assert result['personal_spend_total_cny']=='55.00'
    assert {b['trip_tag']:b['personal_spend_total_cny'] for b in result['by_trip']} == {'东京01':'0.00','东京 02':'30.00','东京02':'20.00',None:'5.00'}
    assert result['coverage']['scope_coverage']=='complete'
    assert not result['coverage']['assignment_complete']
    assert sum(b['record_count'] for b in result['by_trip'])==55


def test_specific_trip_no_date_is_exact_and_has_honest_coverage():
    rows=[record('a',name='hotel #东京01',category='旅行'),record('b',name='hotel #东京010',category='旅行')]
    result,_=query_with([{'items':rows,'has_more':False}], {'view':'total','trip_tag':'东京01'})
    assert result['record_count']==1
    assert result['filters_applied']['date_range'] is None
    assert result['coverage']['scope_coverage']=='unknown'


@pytest.mark.parametrize('args', [{'view':'by_trip'}, {'view':'by_trip','categories':['餐饮']}, {'view':'by_category','trip_tag':'东京01'}])
def test_unbounded_or_wrong_trip_view_refused(args):
    with pytest.raises(AppError):query_with([],args)


def test_trip_parser():
    from personal_data_mcp.finance.trip_query_tags import query_tag
    for name,expected in [('hotel #东京 02','东京 02'),('flight #东京02（10,000 JPY）','东京02'),('hotel #东京01 #东京02',None),('hotel #',None),('flight #东京02 (10000 JPY)',None),('hotel #瑞士法国','瑞士法国')]:
        assert query_tag(name)==expected


def test_dates_cut_trip_and_source_coverage_is_explicit():
    rows=[record('a',name='hotel #东京01',category='旅行',day='2026-01-01'),record('b',name='hotel #东京01',category='旅行',day='2026-02-01')]
    result,_=query_with([{'items':rows,'has_more':False}],{'view':'by_trip','date_range':{'start':'2025-12-01','end':'2026-01-31'}})
    assert result['record_count']==1
    assert result['coverage']['scope_coverage']=='limited'


def test_trip_cursor_is_signed_exact_and_old_cursor_still_works():
    rows=[record(f'r{i:03}',name='hotel #东京01',category='旅行') for i in range(51)]
    first,_=query_with([{'items':rows,'has_more':False}], {'view':'records','trip_tag':'东京01'})
    second,_=query_with([{'items':rows,'has_more':False}], {'view':'records','cursor':first['next_cursor']})
    assert second['filters_applied']['trip_tag']=='东京01'
    assert len(second['records'])==1
    with pytest.raises(AppError):query_with([],{'view':'records','cursor':first['next_cursor'],'trip_tag':'东京01'})


def test_missing_name_is_unassigned_but_duplicate_record_is_fatal():
    row=record('r',category='旅行');row['fields'].pop('名称')
    result,_=query_with([{'items':[row],'has_more':False}],{'view':'by_trip','categories':['旅行']})
    assert result['by_trip'][0]['trip_tag'] is None
    with pytest.raises(AppError):query_with([{'items':[row,row],'has_more':False}],{'view':'by_trip','categories':['旅行']})


@pytest.mark.parametrize('mutation', ['sum','count','duplicate','coverage','unknown','date','infinite'])
def test_projection_refuses_tampered_trip_result(mutation):
    from personal_agent.api.finance_query_projection import decode_finance_query_projection, FinanceQueryProjectionError
    result,_=query_with([{'items':[record('a',name='hotel #东京01',category='旅行')],'has_more':False}],{'view':'by_trip','date_range':{'start':'2026-01-01','end':'2026-12-31'}})
    if mutation=='sum':result['personal_spend_total_cny']='99.00'
    if mutation=='count':result['by_trip'][0]['record_count']=2
    if mutation=='duplicate':result['by_trip'].append(result['by_trip'][0])
    if mutation=='coverage':result['coverage']['scan_complete']=False
    if mutation=='unknown':result['by_trip'][0]['secret']='sensitive'
    if mutation=='date':result['filters_applied']['date_range']=None
    if mutation=='infinite':result['by_trip'][0]['personal_spend_total_cny']='NaN'
    with pytest.raises(FinanceQueryProjectionError):decode_finance_query_projection(result)


def test_shared_swift_fixture():
    from pathlib import Path
    from personal_agent.api.finance_query_projection import decode_finance_query_projection
    result=decode_finance_query_projection((Path(__file__).parents[1]/'fixtures/trip_query.synthetic.json').read_text())
    assert result.personal_spend_total_cny=='285.00'
    assert [b['trip_tag'] for b in result.by_trip]==['东京02','东京01',None]


@pytest.mark.parametrize('limit', ['MAX_TRIP_GROUPS', 'MAX_TRIP_RESULT_BYTES'])
def test_capacity_refuses_instead_of_truncating(monkeypatch, limit):
    import importlib
    module=importlib.import_module('personal_data_mcp.finance.query_expenses')
    monkeypatch.setattr(module,limit,1)
    rows=[record('a',name='hotel #东京01',category='旅行'),record('b',name='hotel #东京02',category='旅行')]
    with pytest.raises(AppError) as error:
        query_with([{'items':rows,'has_more':False}],{'view':'by_trip','categories':['旅行']})
    assert error.value.code.value=='QUERY_CAPACITY_EXCEEDED'


def test_signed_cursor_with_non_object_filters_is_rejected():
    import base64
    import hashlib
    import hmac
    import json
    from datetime import datetime, timezone
    from personal_data_mcp.finance.query_expenses import _decode_cursor

    secret = b'synthetic-cursor-secret'
    raw = json.dumps({'v': 2, 'filters': []}).encode()
    cursor = '.'.join(base64.urlsafe_b64encode(part).decode().rstrip('=')
                      for part in (raw, hmac.new(secret, raw, hashlib.sha256).digest()))
    with pytest.raises(AppError):
        _decode_cursor(cursor, secret=secret, now=datetime.now(timezone.utc))


@pytest.mark.parametrize('tag', [None, '东京02'])
def test_v2_cursor_rejects_trip_tag_key_even_when_null(tag):
    import base64, hashlib, hmac, json
    from test_query_expenses import CURSOR_SECRET, NOW
    from personal_data_mcp.finance.query_expenses import _decode_cursor
    payload={'v':2,'view':'records','filters':{'trip_tag':tag},'offset':0,
             'exp':int(NOW.timestamp())+600,'config_checksum':'ab'*32,'snapshot_checksum':'cd'*32}
    raw=json.dumps(payload).encode()
    cursor='.'.join(base64.urlsafe_b64encode(x).decode().rstrip('=') for x in
                    (raw,hmac.new(CURSOR_SECRET,raw,hashlib.sha256).digest()))
    with pytest.raises(AppError):_decode_cursor(cursor,secret=CURSOR_SECRET,now=NOW)


@pytest.mark.parametrize('cursor', ['a!!!.sig', 'a.sig', 'W10.sig', 'bnVsbA.sig', '////.sig'])
def test_cursor_admission_hint_never_crashes_on_malformed_payload(cursor):
    from personal_agent_core.trip_query import uses_trip_query
    assert uses_trip_query({'view':'records','cursor':cursor}) is False


def test_actual_writer_suffix_is_recognised_without_duplicate_prefix():
    from types import SimpleNamespace
    from personal_data_mcp.finance.money_resolution import with_currency_suffix
    from personal_data_mcp.finance.trip_query_tags import query_tag
    name=with_currency_suffix('机票 #东京01',SimpleNamespace(currency_suffix='（10,000 JPY）'))
    assert name=='机票 #东京01（10,000 JPY）'
    assert query_tag(name)=='东京01'


def test_trip_metadata_schema_does_not_alias_tool_ir():
    from personal_agent.runtime.run_catalog import TASK_VALIDATION
    from personal_agent_core.tool_ir import QUERY_EXPENSES
    import copy
    old=copy.deepcopy(QUERY_EXPENSES.model_input_schema)
    schema=TASK_VALIDATION['properties']['query_requirement']['properties']['trip_tag']
    original=schema.get('description')
    try:
        schema['description']='synthetic mutation'
        assert QUERY_EXPENSES.model_input_schema==old
    finally:
        if original is None:schema.pop('description',None)
        else:schema['description']=original


def test_old_client_large_trip_summary_keeps_total_and_scope():
    import json
    from pathlib import Path
    from personal_agent.api.runtime_v2 import compatible_answer
    q=json.loads((Path(__file__).parents[1]/'fixtures/trip_query.synthetic.json').read_text())
    answer={'version':2,'kind':'query','task_status':'completed','coverage':'complete','text':'x'*8000,
            'evidence':[{'tool':'finance.query_expenses','query_result':q}]}
    result=compatible_answer(answer,4)
    assert '285.00' in result['text'] and '升级' in result['text']
    assert len(result['text'])<=8000 and result['evidence']==[]
    assert answer['evidence']
