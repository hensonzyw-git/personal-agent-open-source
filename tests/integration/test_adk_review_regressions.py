"""Review counterexamples: real API/ADK and SQLite, synthetic HTTP/business only."""
import copy
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from test_agent_api import engine, token_ring, keyring, _auth, NOW, FakeDispatcher, _query_total_result, decode_finance_query_projection
from test_runtime_v2_api import client_for, answer
from test_adk_runtime import fc
from test_run_budget_store import setup
from test_task_control import prepared, state
from personal_agent.api.intent import WriteIntent
from personal_agent.api.orchestrator import Resolved, Written, PossibleDuplicate, CommitUnknown, ReadCompleted, ResolveFailedSafe
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.run_repository import RunRepository
from personal_agent.storage.models import Base, Operation, ConversationEvent


INCOME = [VisibleTool('finance.log_income', 'income', {'type': 'object'}, 'R2', ())]
QUERY = [VisibleTool('finance.query_expenses', 'query', {'type': 'object'}, 'R1', ())]


def send(client, ring, text, **extra):
    return client.post('/v1/chat/messages', headers={**_auth(ring), 'X-Client-Wire-Version': '4', 'Idempotency-Key': str(uuid4())},
                       json={'conversation_id': 'c1', 'text': text, **extra})


def test_new_write_cannot_borrow_old_task_sources(engine, token_ring, keyring):
    sources = []
    def first(c, m):
        sources.extend(m['source_refs'])
        return [fc('agent_finish', 'first', task=m, answer=answer('金额？', 'clarification'))]
    def write(c, m):
        m['source_refs'] = sources
        return [fc('finance_log_income', 'write', arguments={}, task=m)]
    dispatcher = FakeDispatcher(resolve=Resolved(WriteIntent('finance.log_income', {})), commit=Written('synthetic'))
    client, _, _ = client_for(engine, token_ring, keyring, [first, write], tools=INCOME, dispatcher=dispatcher)
    with client:
        old = send(client, token_ring, '记录收入').json()
        response = send(client, token_ring, '先不记，解释收入和利润').json()
        assert not dispatcher.commit_calls
        assert response['result_envelope']['kind'] == 'limitation'
        assert client.get('/v1/operations/' + old['operation_id'], headers={**_auth(token_ring), 'X-Client-Wire-Version': '4'}).json()['state'] == 'waiting_for_clarification'


@pytest.mark.parametrize('clarified', [False, True])
def test_duplicate_unknown_keeps_task_slot_through_recovery(engine, token_ring, keyring, clarified):
    def write(c, m):
        if clarified:m['task_ref'] = c['candidates'][0]['task_ref']
        return [fc('finance_log_income', 'write', arguments={}, task=m)]
    dispatcher = FakeDispatcher(resolve=PossibleDuplicate('dup-review', WriteIntent('finance.log_income', {}), 'synthetic'), commit=CommitUnknown('synthetic_timeout'))
    responses = [answer('金额？', 'clarification'), write] if clarified else [write]
    client, calls, deps = client_for(engine, token_ring, keyring, responses, tools=INCOME, dispatcher=dispatcher)
    with client:
        if clarified:send(client, token_ring, '记收入')
        original = send(client, token_ring, '收入').json()
        headers = {**_auth(token_ring), 'X-Client-Wire-Version': '4', 'Idempotency-Key': str(uuid4())}
        result = client.post('/v1/duplicate-checks/dup-review/decision', headers=headers, json={'decision': 'write_anyway'}).json()
        assert result['state'] == 'source_in_progress'
        repo = RunRepository(deps.session_factory, keyring)
        repo.sweep(now_ms=round(NOW.timestamp()*1000))
        task_id = repo.snapshot(original['operation_id'])['task_id']
        task = repo.task_snapshot(task_id)
        assert task['status'] == 'waiting' and task['write_slot'] is not None
        poll = client.get('/v1/operations/'+original['operation_id'], headers=headers).json()
        assert poll['result_envelope']['task_status'] == 'waiting'
        assert poll['result_envelope']['evidence'][0]['state'] == 'source_in_progress'
        client.delete('/v1/operations/'+original['operation_id'], headers=headers)
        assert repo.task_snapshot(task_id)['write_slot'] == task['write_slot']
        assert len(calls) == 1 + int(clarified) and len(dispatcher.commit_calls) == 1
        # A late authoritative receipt must settle that same task exactly once.
        from personal_agent.api.operation_store import transition_operation
        with deps.session_factory() as s:
            op = s.get(Operation, result['operation_id'])
            v = transition_operation(s, operation_id=op.operation_id, current_state=op.state, current_version=op.state_version, target_state='verifying', now=NOW)
            transition_operation(s, operation_id=op.operation_id, current_state='verifying', current_version=v, target_state='succeeded', safe_result='synthetic', now=NOW)
            s.commit()
        repo.sweep(now_ms=round(NOW.timestamp()*1000))
        assert repo.task_snapshot(task_id)['status'] == 'completed'
        assert repo.task_snapshot(task_id)['write_slot'] is None
        poll = client.get('/v1/operations/'+original['operation_id'], headers=headers).json()
        assert poll['result_envelope']['task_status'] == 'completed'
        assert poll['result_envelope']['evidence'][0]['record_id'] == 'synthetic'


def test_active_amend_reserves_and_atomically_replaces_old_proposal(setup, keyring):
    controls, proposal, lease, factory = prepared(setup)
    candidates = controls.reserve_prebind(lease.operation_id, ['task'], now_ms=4, lease=lease)
    assert not candidates[0].resumable
    assert candidates[0].amendable
    repo = RunRepository(factory, keyring)
    with factory() as s:
        s.execute(update(repo.runs).where(repo.runs.c.operation_id == lease.operation_id).values(
            sealed_input_snapshot=repo.seal('agent_runs','sealed_input_snapshot',lease.operation_id,{})))
        s.commit()
    assert repo.amend_and_bind(lease, task_id='task', expected_revision=1, control_id='amend', metadata={'goal':'changed', 'constraints':[]}, now_ms=5) == 'applied'
    assert state(factory, proposal.lease.operation_id) == 'cancelled_pre_submit'
    assert repo.task_snapshot('task')['active_operation_id'] == lease.operation_id
    assert repo.task_snapshot('task')['llm_used'] == 2
    with pytest.raises(ValueError): controls.claim_submit(proposal, now_ms=6)


@pytest.mark.parametrize("mode", ["swap_contract", "swap_metrics", "omit_node", "valid"])
@pytest.mark.parametrize('metric_kind', ['total', 'category_total'])
def test_comparison_contract_cannot_be_redefined_at_finish(engine, token_ring, keyring, mode, metric_kind):
    current = _query_total_result()
    current['filters_applied']['categories'] = []
    baseline = copy.deepcopy(current)
    current['filters_applied']['date_range'] = {'start':'2026-09-01', 'end':'2026-09-30'}
    baseline['filters_applied']['date_range'] = {'start':'2026-08-01', 'end':'2026-08-31'}
    current['personal_spend_total_cny'] = '120.00'
    baseline['personal_spend_total_cny'] = '100.00'
    current_filters, baseline_filters = copy.deepcopy(current['filters_applied']), copy.deepcopy(baseline['filters_applied'])
    if metric_kind == 'category_total':
        current_filters['category'] = baseline_filters['category'] = '餐饮'
        for data in (current, baseline):
            data['view'] = 'by_category'
            data['by_category'] = [{'category':'餐饮','personal_spend_total_cny':data['personal_spend_total_cny'],'record_count':data['record_count'],'share_of_total':'1'}]
    class Dispatcher:
        def resolve(self, **kw):
            return ReadCompleted(result='query', projection=decode_finance_query_projection(current if kw['model_args']['month'] == 9 else baseline))
    def read(c, m):
        m['comparisons'] = [{'metric_kind':metric_kind, 'current':current_filters, 'baseline':baseline_filters, 'source_refs':m['source_refs']}]
        return [fc('finance_query_expenses', 'q9', arguments={'month':9}, task=m), fc('finance_query_expenses', 'q8', arguments={'month':8}, task=m)]
    def finish(c, m):
        req = copy.deepcopy(c['bound_task']['comparisons'][0])
        if mode == 'swap_contract':
            req['current'], req['baseline'] = req['baseline'], req['current']
        m['comparisons'] = [req]
        metrics = {x['evidence_ref']:x for x in c['metrics'] if x['metric_kind'] == metric_kind}
        current_ref, baseline_ref = ('query_q9', 'query_q8') if mode == 'valid' else ('query_q8', 'query_q9')
        node = {'kind':'comparison', 'current_metric_ref':metrics[current_ref]['metric_ref'], 'baseline_metric_ref':metrics[baseline_ref]['metric_ref'], 'comparison_ref':req['comparison_ref']}
        if mode == 'omit_node':node = {'kind':'metric', 'metric_ref':metrics[current_ref]['metric_ref']}
        return [fc('agent_finish', 'finish', task=m, answer={'kind':'analysis', 'coverage':'complete', 'evidence_refs':list(metrics),
            'analysis_nodes':[node], 'commentary':''})]
    client, calls, deps = client_for(engine, token_ring, keyring, [read, finish], tools=QUERY, dispatcher=Dispatcher())
    with client:
        result = send(client, token_ring, '比较九月对八月的增减').json()['result_envelope']
        assert len(calls) == 2  # The valid initial comparison request must be admitted.
        if mode == 'valid':
            assert result['kind'] == 'analysis' and result['coverage'] == 'complete'
            assert result['analysis_nodes'][0]['difference_decimal'] == '20.00'
        else:
            assert result['kind'] == 'limitation' and result['coverage'] == 'partial'
            pending, _ = RunRepository(deps.session_factory, keyring).pending('c1')
            assert pending[0]['metadata']['comparisons'][0]['current'] == current_filters
        assert len(result['evidence']) == 2


@pytest.mark.parametrize("matching", [False, True])
def test_task_trip_constraint_must_match_trusted_query_filters(engine, token_ring, keyring, matching):
    def read(c, m):
        m['constraints'] = [{'key':'trip_tag', 'value':'合成旅行01', 'source_refs':m['source_refs']}]
        return [fc('finance_query_expenses', 'q', arguments={}, task=m)]
    def finish(c, m):
        m['constraints'] = c['bound_task']['constraints']
        metric = c['metrics'][0]
        return [fc('agent_finish', 'finish', task=m, answer={'kind':'analysis', 'coverage':'complete', 'evidence_refs':[metric['evidence_ref']],
            'analysis_nodes':[{'kind':'metric', 'metric_ref':metric['metric_ref']}], 'commentary':''})]
    data = _query_total_result()
    if matching:data['filters_applied']['name_contains'] = ['#合成旅行01']
    dispatcher = FakeDispatcher(resolve=ReadCompleted(result='query', projection=decode_finance_query_projection(data)))
    client, _, _ = client_for(engine, token_ring, keyring, [read, finish], tools=QUERY, dispatcher=dispatcher)
    with client:
        result = send(client, token_ring, '只查询合成旅行01').json()['result_envelope']
        assert result['coverage'] == ('complete' if matching else 'partial')
        assert result['kind'] == ('analysis' if matching else 'limitation')
        assert len(result['evidence']) == 1


@pytest.mark.parametrize('duplicate', [False, True])
def test_history_retains_business_receipt_fields(engine, token_ring, keyring, duplicate):
    def write(c, m): return [fc('finance_log_income', 'write', arguments={}, task=m)]
    outcome = PossibleDuplicate('dup-history', WriteIntent('finance.log_income', {}), 'synthetic') if duplicate else Resolved(WriteIntent('finance.log_income', {}))
    client, _, deps = client_for(engine, token_ring, keyring, [write], tools=INCOME, dispatcher=FakeDispatcher(resolve=outcome, commit=Written('synthetic-record')))
    with client:
        result = send(client, token_ring, '记录收入').json()
        with deps.session_factory() as s:
            event = s.execute(select(ConversationEvent).where(ConversationEvent.event_type == 'operation_result')).scalar_one()
            body = json.loads(keyring.decrypt(event.encrypted_content, table='conversation_events', column='encrypted_content', row_id=event.event_id))
        assert body['tool'] == 'finance.log_income'
        field = 'duplicate_check_id' if duplicate else 'record_id'
        assert body[field] == result[field]
        assert body['result_envelope']['version'] == 2
        from pathlib import Path
        vectors = json.loads((Path(__file__).parents[2] / 'src/personal_agent/api/vectors/result_envelope_v2.json').read_text())
        expected = vectors['history_cases'][int(duplicate)]['content']
        assert {k:body[k] for k in ('state','tool',field)} == {k:expected[k] for k in ('state','tool',field)}
        assert body['result_envelope']['kind'] == expected['result_envelope']['kind']


def test_binding_conflict_keeps_charge_on_selected_task(engine, token_ring, keyring):
    ids = []
    def conflict(c, m):
        old = c['candidates'][0]; ids.append(old['task_ref']); m['task_ref'] = old['task_ref']
        with deps.session_factory() as s:
            tasks = Base.metadata.tables['agent_tasks']
            s.execute(update(tasks).where(tasks.c.task_id == old['task_ref']).values(revision=old['revision']+1)); s.commit()
        return [fc('agent_finish', 'conflict', task=m, answer=answer())]
    client, _, deps = client_for(engine, token_ring, keyring, [answer('日期？', 'clarification'), conflict])
    with client:
        send(client, token_ring, '查询')
        result = send(client, token_ring, '继续').json()
        repo = RunRepository(deps.session_factory, keyring)
        assert repo.task_snapshot(ids[0])['llm_used'] == 2
        assert result['result_envelope']['failure']['code'] == 'task_binding_conflict'
        with deps.session_factory() as s:
            assert s.execute(select(repo.reservations.c.charged_llm)).scalar_one() == 1


def test_recovery_visits_runs_after_first_page_of_parked_tasks(setup, keyring):
    _, new_run, factory = setup
    repo = RunRepository(factory, keyring)
    for i in range(100):
        op = new_run('parked'+str(i), now=i)
        with factory() as s:
            s.execute(update(repo.runs).where(repo.runs.c.operation_id == op).values(state='parked'))
            s.execute(update(Operation).where(Operation.operation_id == op).values(state='waiting_for_clarification')); s.commit()
    expired = new_run('expired', now=100)
    repo.sweep(now_ms=100000)
    assert repo.snapshot(expired)['state'] != 'accepted'


def test_failed_batch_does_not_dispatch_remaining_reads(engine, token_ring, keyring):
    class Dispatcher:
        calls = 0
        def resolve(self, **kw):
            self.calls += 1
            return ResolveFailedSafe('policy_denied')
    dispatcher = Dispatcher()
    def read(c, m): return [fc('finance_query_expenses', 'q1', arguments={'view':'total'}, task=m), fc('finance_query_expenses', 'q2', arguments={'view':'by_category'}, task=m)]
    client, calls, deps = client_for(engine, token_ring, keyring, [read, answer('finished')], tools=QUERY, dispatcher=dispatcher)
    with client:
        result = send(client, token_ring, '比较查询').json()['result_envelope']
        assert dispatcher.calls == 1 and len(calls) == 1
        assert result['coverage'] == 'partial' and result['task_status'] == 'waiting'
        with deps.session_factory() as s:
            steps = Base.metadata.tables['agent_run_steps']
            assert s.execute(select(steps.c.status).where(steps.c.call_id == 'q2')).scalar_one() == 'not_executed'


def test_card_cannot_be_mixed_with_another_read(engine, token_ring, keyring):
    def read(c,m):return [fc('finance_query_expenses','q1',arguments={},task=m,response_mode='card'),
                         fc('finance_query_expenses','q2',arguments={},task=m)]
    class Dispatcher(FakeDispatcher):
        resolve_calls = 0
        def resolve(self, **kw):
            self.resolve_calls += 1
            return super().resolve(**kw)
    dispatcher = Dispatcher(resolve=ReadCompleted(result='query',projection=decode_finance_query_projection(_query_total_result())))
    client, calls, _ = client_for(engine, token_ring, keyring, [read],tools=QUERY,dispatcher=dispatcher)
    with client:
        result = send(client,token_ring,'查两次').json()['result_envelope']
        assert result['kind']=='limitation' and not dispatcher.resolve_calls and len(calls)==1


def test_simple_query_card_finishes_without_second_model(engine, token_ring, keyring):
    def read(c, m): return [fc('finance_query_expenses', 'q', arguments={}, task=m, response_mode='card')]
    dispatcher = FakeDispatcher(resolve=ReadCompleted(result='query', projection=decode_finance_query_projection(_query_total_result())))
    client, calls, _ = client_for(engine, token_ring, keyring, [read], tools=QUERY, dispatcher=dispatcher)
    with client:
        result = send(client, token_ring, '查全年总额').json()['result_envelope']
        assert result['kind'] == 'query' and len(calls) == 1
        assert result['evidence'][0]['query_result']['personal_spend_total_cny'] == '1200.00'


def test_same_task_write_can_use_its_original_request(engine, token_ring, keyring):
    sources = []
    def first(c, m):
        sources.extend(m['source_refs'])
        return [fc('agent_finish', 'first', task=m, answer=answer('金额？', 'clarification'))]
    def write(c, m):
        m['task_ref'] = c['candidates'][0]['task_ref']
        m['source_refs'] += sources
        return [fc('finance_log_income', 'write', arguments={}, task=m, write_source_refs=sources)]
    dispatcher = FakeDispatcher(resolve=Resolved(WriteIntent('finance.log_income', {})), commit=Written('synthetic'))
    client, _, _ = client_for(engine, token_ring, keyring, [first, write], tools=INCOME, dispatcher=dispatcher)
    with client:
        send(client, token_ring, '记录收入')
        response = send(client, token_ring, '一百元').json()
        assert response['state'] == 'succeeded' and len(dispatcher.commit_calls) == 1


@pytest.mark.parametrize('race', ['revision', 'submit'])
def test_amend_conflict_charges_without_replacing_winner(setup, keyring, race):
    controls, proposal, lease, factory = prepared(setup)
    controls.reserve_prebind(lease.operation_id, ['task'], now_ms=4, lease=lease)
    repo = RunRepository(factory, keyring)
    if race == 'submit':
        controls.claim_submit(proposal, now_ms=5)
    else:
        with factory() as s:
            s.execute(update(repo.tasks).where(repo.tasks.c.task_id=='task').values(revision=2));s.commit()
    before = repo.task_snapshot('task')
    with pytest.raises(ValueError, match='task_binding_conflict|task_amend_too_late'):
        repo.amend_and_bind(lease, task_id='task', expected_revision=1, control_id='amend', metadata={'goal':'changed','constraints':[]}, now_ms=6)
    after = repo.task_snapshot('task')
    assert after['llm_used'] == before['llm_used'] + 1
    assert after['active_operation_id'] == before['active_operation_id']
    assert after['write_slot'] == before['write_slot']


def test_production_amend_revokes_inflight_read_before_old_write(engine, token_ring, keyring):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    class Dispatcher(FakeDispatcher):
        def resolve(self, **kw):
            entered.set()
            assert release.wait(15)
            return ReadCompleted(result='query', projection=decode_finance_query_projection(_query_total_result()))
    def read(c, m):return [fc('finance_query_expenses', 'q', arguments={}, task=m)]
    def amend(c, m):
        old = c['candidates'][0]
        assert old['amendable'] and not old['resumable']
        return [fc('agent_task_control','change',task_ref=old['task_ref'],action='amend',source_refs=m['source_refs'],replacement=m)]
    dispatcher = Dispatcher()
    client, calls, deps = client_for(engine, token_ring, keyring, [read, amend, answer('更正后目标完成')], tools=QUERY+INCOME, dispatcher=dispatcher)
    with client, ThreadPoolExecutor(1) as pool:
        old = pool.submit(send, client, token_ring, '先查询，再记录收入')
        assert entered.wait(15)
        try:
            changed = send(client, token_ring, '不要记录，改成普通解释')
            assert changed.status_code == 200, changed.text
            assert changed.json()['result_envelope']['text'] == '更正后目标完成'
        finally:
            release.set()
        assert old.result().json()['state'] == 'cancelled_pre_submit'
        assert len(calls) == 3 and not dispatcher.commit_calls
        repo = RunRepository(deps.session_factory, keyring)
        task = repo.task_snapshot(repo.snapshot(changed.json()['operation_id'])['task_id'])
        assert task['revision'] == 2 and task['llm_used'] == 3


@pytest.mark.parametrize('flag', ['next_cursor', 'mirror_stale'])
def test_calendar_card_does_not_claim_incomplete_mirror_complete(engine, token_ring, keyring, flag):
    projection = {'status':'ok','events':[],'record_count':0,'next_cursor':None,'mirror_stale':False,'source_system':'apple_calendar_mirror','data_as_of':'2026-09-14T00:00:00Z'}
    projection[flag] = 'synthetic-cursor' if flag == 'next_cursor' else True
    if flag == 'next_cursor':projection['record_count'] = 1
    from personal_agent.api.calendar_query_projection import decode_calendar_query_projection
    projection = decode_calendar_query_projection(projection)
    def read(c,m):return [fc('calendar_query_events','q',arguments={},task=m,response_mode='card')]
    client, calls, _ = client_for(engine, token_ring, keyring, [read],
        tools=[VisibleTool('calendar.query_events','calendar',{'type':'object'},'R1',())],
        dispatcher=FakeDispatcher(resolve=ReadCompleted(result='query',projection=projection)))
    with client:
        result = send(client, token_ring, '查日历').json()['result_envelope']
        assert len(calls) == 1 and result['kind'] == 'query'
        assert result['coverage'] == 'partial' and result['task_status'] == 'waiting'

@pytest.mark.parametrize('damaged', [None, [], {}, {'version': 2, 'kind': 'action'}, {'version': 2, 'kind': []}, 'broken-ciphertext'])
def test_corrupt_outcome_is_a_limitation_without_losing_operation(engine, token_ring, keyring, damaged):
    client, _, deps = client_for(engine, token_ring, keyring, [answer()])
    with client:
        original = send(client, token_ring, 'synthetic').json()
        repo = RunRepository(deps.session_factory, keyring)
        sealed = repo.seal('agent_run_outcomes', 'sealed_answer', original['operation_id'], damaged)
        if damaged == 'broken-ciphertext': sealed['tag'] = 'AAAAAAAAAAAAAAAAAAAAAA'
        with deps.session_factory() as s:
            s.execute(update(repo.outcomes).where(repo.outcomes.c.operation_id == original['operation_id']).values(sealed_answer=sealed))
            s.commit()
        polled = client.get('/v1/operations/' + original['operation_id'], headers={**_auth(token_ring), 'X-Client-Wire-Version':'4'})
        assert polled.status_code == 200
        assert polled.json()['state'] == original['state']
        assert polled.json()['result_envelope']['kind'] == 'limitation'
        with deps.session_factory() as s:
            assert s.execute(select(repo.outcomes.c.sealed_answer).where(repo.outcomes.c.operation_id == original['operation_id'])).scalar_one() == sealed


def test_amend_operation_cas_failure_is_binding_conflict(setup, keyring, monkeypatch):
    from personal_agent.api.operation_state import StaleOperationVersionError
    import personal_agent.api.operation_store as control
    controls, proposal, lease, factory = prepared(setup)
    controls.reserve_prebind(lease.operation_id, ['task'], now_ms=4, lease=lease)
    repo = RunRepository(factory, keyring)
    before = repo.task_snapshot('task')
    def stale(*args, **kwargs): raise StaleOperationVersionError('synthetic-cas')
    monkeypatch.setattr(control, 'transition_operation', stale)
    with pytest.raises(ValueError, match='task_binding_conflict'):
        repo.amend_and_bind(lease, task_id='task', expected_revision=1, control_id='cas', metadata={'goal':'changed','constraints':[]}, now_ms=6)
    after = repo.task_snapshot('task')
    assert after['llm_used'] == before['llm_used'] + 1
    assert after['revision'] == before['revision']
    assert after['write_slot'] == before['write_slot']
    assert state(factory, proposal.lease.operation_id) == 'dispatching'


def test_sweep_skips_unchanged_waits_but_selects_old_late_receipt(setup, monkeypatch):
    _, new_run, factory = setup
    repo = RunRepository(factory, None)
    ids = [new_run(str(i)) for i in range(105)]
    with factory() as s:
        s.execute(update(repo.runs).values(state='parked'))
        # Old completed business work still needs settlement; age must not hide it.
        s.execute(update(Operation).where(Operation.operation_id == ids[-1]).values(state='succeeded'))
        s.commit()
    recovered, settled = [], []
    monkeypatch.setattr(repo, 'recover', lambda op, **kw: recovered.append(op))
    monkeypatch.setattr(repo, 'settle_expired_or_business', lambda op, **kw: settled.append(op))
    repo.sweep(now_ms=10**12)
    assert recovered == settled == [ids[-1]]


def test_recovery_startup_preserves_existing_runs_without_device_grants(setup):
    from types import SimpleNamespace
    from personal_agent.api.runtime_v2 import recovery_needed
    _, new_run, factory = setup
    deps = SimpleNamespace(session_factory=factory, v2_execution_enabled=True, v2_device_ids=frozenset())
    assert not recovery_needed(deps)
    new_run('existing')
    assert recovery_needed(deps)
    deps.v2_execution_enabled = False
    assert not recovery_needed(deps)


def test_recovery_startup_without_migration_is_quiet(tmp_path):
    from types import SimpleNamespace
    from personal_agent.storage.engine import create_database_engine, session_factory
    from personal_agent.api.runtime_v2 import recovery_needed
    engine = create_database_engine(tmp_path / 'legacy.sqlite')
    assert not recovery_needed(SimpleNamespace(session_factory=session_factory(engine), v2_execution_enabled=True, v2_device_ids={'synthetic'}))
    engine.dispose()
