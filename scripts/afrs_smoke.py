"""Live smoke test: pull all six companies from SEC EDGAR and compute AFRS.

Ad-hoc verification script, not shipped code. Run:
    PYTHONPATH=src .venv/bin/python scripts/afrs_smoke.py
"""

from __future__ import annotations

from risk_monitor.daily import collect_afrs
from risk_monitor.ingestion.edgar import EdgarClient
from risk_monitor.scoring import company_afrs, sector_afrs
from risk_monitor.scoring.policy import load_policy

policy = load_policy()
per_company, details = collect_afrs(EdgarClient())
company_scores: dict[str, float] = {}
for eid, values in per_company.items():
    outcome = company_afrs(policy, values)
    if outcome.score is not None:
        company_scores[eid] = outcome.score
    d = details[eid]
    print(
        f"{eid:6s} score={outcome.score!r:>8} "
        f"conf={d.get('confidence')} flags={d.get('flags')} "
        f"values={values}"
    )
afrs = sector_afrs(policy, company_scores)
print(f"=== sector AFRS = {afrs} over {len(company_scores)} companies")
