import json
import sqlite3
import pytest
from personal_agent_dal.worker.provider_budget import available


def test_real_telemetry_counts_errors_and_preserves_unknown_reserves(tmp_path):
    db=tmp_path/'usage.sqlite'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE usage_events(id INTEGER PRIMARY KEY, model TEXT)')
        c.executemany('INSERT INTO usage_events(model) VALUES (?)',[('synthetic-model',)]*4)
    path=tmp_path/'quota.json'
    body=dict(limit=10,baseline_usage_id=1,independent_agent_reviews=2,unreconciled_request_reservations=2,
        usage_database=str(db),model_pattern='synthetic-model')
    path.write_text(json.dumps(body));path.chmod(0o600)
    assert available(path)
    with sqlite3.connect(db) as c:c.execute("INSERT INTO usage_events(model) VALUES ('synthetic-model')")
    assert not available(path)
    body.pop('baseline_usage_id');path.write_text(json.dumps(body))
    assert not available(path)


def test_missing_corrupt_or_unreadable_budget_fails_closed(tmp_path):
    path=tmp_path/'quota.json'
    assert not available(path)
    for body in ('{', '{}', '[]'):
        path.write_text(body);path.chmod(0o600)
        assert not available(path)


def test_recovered_prepared_attempt_stops_before_dispatch_without_budget(monkeypatch):
    from personal_agent_dal.worker.workflow import WorkflowWorker
    worker=object.__new__(WorkflowWorker)
    worker.config={'provider_budget_file':'synthetic'}
    monkeypatch.setattr('personal_agent_dal.worker.provider_budget.available',lambda _:False)
    assert worker._execute({})=={'status':'provider_budget_exhausted'}
