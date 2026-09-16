"""Exercise omitted model dates through the API, SDK and durable Host."""
import pytest
from datetime import datetime, timezone
from sqlalchemy import update
from personal_agent.storage.models import ApiRequest

from test_runtime_v2_api import client_for, fc
from test_agent_api import engine, token_ring, keyring, _auth, FakeDispatcher
from personal_agent.api.intent import WriteIntent
from personal_agent.api.orchestrator import Resolved, Written
from personal_agent.policy.bridge import VisibleTool


@pytest.mark.parametrize('previous_receipt', [False, True])
@pytest.mark.parametrize('tool,text', [
    ('finance.log_expense', '午饭 20 个人支出'),
    ('finance.log_income', '工资收入 100'),
])
@pytest.mark.parametrize('date_args,dated,expected', [
    ({}, False, '2026-07-24'),
    ({'occurred_on': '2026-07-23'}, False, '2026-07-23'),
    ({}, True, None),
    ({'occurred_on': None}, False, None),
    ({'occurred_on': ''}, False, ''),
])
def test_host_date_before_authorization(engine, token_ring, keyring, tool, text,
                                        date_args, dated, expected, previous_receipt):
    seen = []
    class Dispatcher(FakeDispatcher):
        def resolve(self, *, tool, model_args, idempotency_key=None):
            assert model_args == seen[-1]
            return Resolved(WriteIntent(tool, model_args))

    dispatcher = Dispatcher(commit=Written('synthetic-record'))
    arguments = {'name': 'synthetic', 'input_amount': '20', **date_args}
    def write(c, m):
        if previous_receipt:
            # Persisted receipt is 23:59 Shanghai on the previous day, while
            # the worker clock remains July 24. Defaults must use the receipt.
            with deps.session_factory() as session:
                session.execute(update(ApiRequest).values(
                    received_at=datetime(2026, 7, 23, 15, 59, tzinfo=timezone.utc)))
                session.commit()
        return [fc(tool.replace('.', '_'), 'w', arguments=arguments, task=m)]
    client, _, deps = client_for(engine, token_ring, keyring, [write],
        tools=[VisibleTool(tool, 'write', {'type': 'object'}, 'R2', ())], dispatcher=dispatcher)
    def authorize(**kw):
        seen.append(dict(kw['model_args']))
        return kw['model_args']
    deps.build_authorizer = lambda auth: authorize
    with client:
        response = client.post('/v1/chat/messages',
            headers={**_auth(token_ring), 'X-Client-Wire-Version': '4'},
            json={'conversation_id': 'c1', 'text': ('昨天' if dated else '') + text})
        assert response.status_code == 200, response.text
        assert seen, response.text
        if previous_receipt and not date_args and not dated:
            expected = '2026-07-23'
        assert seen[-1].get('occurred_on') == expected
        assert ('occurred_on' in seen[-1]) == (bool(date_args) or not dated)
        assert dispatcher.commit_calls[0]['intent'].model_args == seen[-1]
    assert arguments == {'name': 'synthetic', 'input_amount': '20', **date_args}
