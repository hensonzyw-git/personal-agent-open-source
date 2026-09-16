"""Replay pagination through API/ADK and the real Finance query implementation."""
import json

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from test_agent_api import engine, token_ring, keyring, _auth, FakeDispatcher
from test_runtime_v2_api import client_for
from test_adk_runtime import fc
from unit.test_query_expenses import query_with, record
from personal_agent.api.orchestrator import ReadCompleted, ResolveFailedSafe
from personal_agent.api.finance_query_projection import decode_finance_query_projection
from personal_agent.policy.bridge import VisibleTool
from personal_agent_core.tool_ir import QUERY_EXPENSES
from personal_agent.runtime.run_repository import RunRepository

FILTERS = {'date_range': {'start': '2026-01-01', 'end': '2026-12-31'}, 'categories': ['旅行']}
TOOLS = [VisibleTool('finance.query_expenses', QUERY_EXPENSES.summary, QUERY_EXPENSES.model_input_schema, 'R1', ())]


class QueryDispatcher:
    def __init__(self):
        self.calls = []

    def resolve(self, *, tool, model_args, idempotency_key):
        self.calls.append(model_args)
        rows = [record(f'r{i:03}', category='旅行') for i in range(51)]
        result, _ = query_with([{'items': rows, 'has_more': False}], model_args)
        return ReadCompleted(result='query', projection=decode_finance_query_projection(result))


def test_valid_cursor_continuation_returns_second_page(engine, token_ring, keyring):
    dispatcher = QueryDispatcher()
    def first(c, m):
        return [fc('finance_query_expenses', 'first', arguments={'view': 'records', **FILTERS}, task=m, response_mode='analyze')]
    def second(c, m):
        cursor = c['completed_results'][0]['query_result']['next_cursor']
        return [fc('finance_query_expenses', 'second', arguments={'view': 'records', 'cursor': cursor}, task=m, response_mode='card')]
    client, calls, deps = client_for(engine, token_ring, keyring, [first, second], tools=TOOLS, dispatcher=dispatcher)
    with client:
        response = client.post('/v1/chat/messages', headers={**_auth(token_ring), 'X-Client-Wire-Version': '4'}, json={'conversation_id': 'c1', 'text': '查询旅行明细下一页'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['state'] == 'succeeded'
        assert len(dispatcher.calls) == 2
        assert set(dispatcher.calls[1]) == {'view', 'cursor'}
        assert result['result_envelope']['evidence'][-1]['query_result']['records'][0]['record_id'] == 'r000'


@pytest.mark.parametrize('extra', [{'categories': ['旅行']}, {'date_range': FILTERS['date_range']}, {'name_contains': []}, {'is_family_expense': 'all'}, {'personal_amount_cny': None}])
def test_cursor_filters_rejected_before_dispatch(engine, token_ring, keyring, extra):
    dispatcher = QueryDispatcher()
    def bad(c, m):
        return [fc('finance_query_expenses', 'bad', arguments={'view': 'records', 'cursor': 'synthetic', **extra}, task=m)]
    client, _, deps = client_for(engine, token_ring, keyring, [bad], tools=TOOLS, dispatcher=dispatcher)
    with client:
        response = client.post('/v1/chat/messages', headers={**_auth(token_ring), 'X-Client-Wire-Version': '4'}, json={'conversation_id': 'c1', 'text': '继续查询'})
        result = response.json()
        assert response.status_code == 200, response.text
        assert dispatcher.calls == []
        assert result['state'] == 'failed_safe'
        assert result['result_envelope']['failure']['code'] == 'finance_cursor_filter_conflict'
        assert '分页' in result['result_envelope']['text']


@pytest.mark.parametrize('reason,expected', [('INVALID_ARGUMENT', 'INVALID_ARGUMENT'), ('source_unavailable', 'source_unavailable'), ('secret raw provider body', 'query_incomplete')])
def test_read_failure_preserves_safe_code_and_persisted_terminal_state(engine, token_ring, keyring, reason, expected):
    def read(c, m):
        return [fc('finance_query_expenses', 'q', arguments={'view': 'total', **FILTERS}, task=m)]
    client, _, deps = client_for(engine, token_ring, keyring, [read], tools=TOOLS, dispatcher=FakeDispatcher(resolve=ResolveFailedSafe(reason=reason)))
    with client:
        headers = {**_auth(token_ring), 'X-Client-Wire-Version': '4'}
        response = client.post('/v1/chat/messages', headers=headers, json={'conversation_id': 'c1', 'text': '查询旅行总额'})
        result = response.json()
        assert response.status_code == 200, response.text
        assert result['state'] == 'failed_safe'
        assert result['failure_reason'] == expected
        assert result['result_envelope']['failure']['code'] == expected
        assert 'secret raw' not in json.dumps(result)
        repo = RunRepository(deps.session_factory, keyring)
        with deps.session_factory() as session:
            run = session.execute(select(repo.runs).where(repo.runs.c.operation_id == result['operation_id'])).mappings().one()
            outcome = session.execute(select(repo.outcomes).where(repo.outcomes.c.operation_id == result['operation_id'])).mappings().one()
            assert run['state'] == 'partial'
            assert outcome['failure_code'] == expected
        polled = client.get('/v1/operations/' + result['operation_id'], headers=headers).json()
        assert polled['state'] == 'failed_safe'
        assert polled['result_envelope'] == result['result_envelope']


def test_model_schema_cursor_contract():
    from personal_agent.runtime.run_catalog import catalog
    spec = catalog([{'function': {'name': QUERY_EXPENSES.name, 'description': QUERY_EXPENSES.summary, 'parameters': QUERY_EXPENSES.model_input_schema}}])[0]
    validator = Draft202012Validator(spec.schema['properties']['arguments'])
    assert validator.is_valid({'view': 'records', 'cursor': 'synthetic'})
    assert validator.is_valid({'view': 'records', **FILTERS})
    assert not validator.is_valid({'view': 'records', 'cursor': 'synthetic', **FILTERS})
    assert not validator.is_valid({'view': 'total', 'cursor': 'synthetic'})
    assert not validator.is_valid({'view': 'records', 'cursor': ''})


def test_failed_second_page_keeps_first_page_evidence(engine, token_ring, keyring):
    dispatcher = QueryDispatcher()
    def first(c, m):
        return [fc('finance_query_expenses', 'first', arguments={'view': 'records', **FILTERS}, task=m, response_mode='analyze')]
    def bad_second(c, m):
        cursor = c['completed_results'][0]['query_result']['next_cursor']
        return [fc('finance_query_expenses', 'second', arguments={'view': 'records', 'cursor': cursor, **FILTERS}, task=m)]
    client, _, deps = client_for(engine, token_ring, keyring, [first, bad_second], tools=TOOLS, dispatcher=dispatcher)
    with client:
        headers = {**_auth(token_ring), 'X-Client-Wire-Version': '4'}
        result = client.post('/v1/chat/messages', headers=headers, json={'conversation_id': 'c1', 'text': '旅行明细'}).json()
        assert result['state'] == 'failed_safe'
        assert len(dispatcher.calls) == 1
        envelope = result['result_envelope']
        assert envelope['failure']['code'] == 'finance_cursor_filter_conflict'
        assert len(envelope['evidence'][0]['query_result']['records']) == 50
        assert envelope['task_status'] == 'waiting'
        repo = RunRepository(deps.session_factory, keyring)
        assert repo.snapshot(result['operation_id'])['state'] == 'partial'
        # Restart recovery must not replace the failed partial result with success.
        repo.settle_expired_or_business(result['operation_id'], now_ms=10**13)
        assert client.get('/v1/operations/' + result['operation_id'], headers=headers).json()['result_envelope'] == envelope


def test_conflicting_cursor_rejects_entire_read_batch(engine, token_ring, keyring):
    dispatcher = QueryDispatcher()
    def batch(c, m):
        return [
            fc('finance_query_expenses', 'valid', arguments={'view': 'total', **FILTERS}, task=m),
            fc('finance_query_expenses', 'invalid', arguments={'view': 'records', 'cursor': 'synthetic', **FILTERS}, task=m),
        ]
    client, _, _ = client_for(engine, token_ring, keyring, [batch], tools=TOOLS, dispatcher=dispatcher)
    with client:
        result = client.post('/v1/chat/messages', headers={**_auth(token_ring), 'X-Client-Wire-Version': '4'}, json={'conversation_id': 'c1', 'text': '旅行总额和明细'}).json()
        assert result['state'] == 'failed_safe'
        assert result['result_envelope']['failure']['code'] == 'finance_cursor_filter_conflict'
        assert dispatcher.calls == []
