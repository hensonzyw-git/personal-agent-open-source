"""Operator-owned observed request budget; includes unresolved reservations.

A two-request reserve covers one serial in-flight request and delayed telemetry.
This is not a provider billing receipt and does not infer zero for missing data.
"""
import sqlite3
from pathlib import Path
from personal_agent_dal.worker.runtime_admission import private_json


def available(path):
    value=private_json(path)
    for key in ('limit','baseline_usage_id','independent_agent_reviews','unreconciled_request_reservations'):
        if type(value.get(key)) is not int or value[key]<0:raise ValueError('PROVIDER_BUDGET_INVALID')
    database=Path(value['usage_database']).resolve(strict=True)
    pattern=value['model_pattern']
    if not isinstance(pattern,str) or not pattern:raise ValueError('PROVIDER_BUDGET_INVALID')
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=1) as db:
        used=db.execute('SELECT COUNT(*) FROM usage_events WHERE id>? AND model LIKE ?',
            (value['baseline_usage_id'],pattern)).fetchone()[0]
    return used+value['independent_agent_reviews']+value['unreconciled_request_reservations']<value['limit']-2
