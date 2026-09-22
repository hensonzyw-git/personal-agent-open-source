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
    with pytest.raises(ValueError):available(path)
