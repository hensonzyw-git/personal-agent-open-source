"""Ad-hoc investigation: which us-gaap tags carry recent fundamentals for the 6
AI companies, and what the latest annual values look like.

This is a discovery script, not shipped code — it exists to pin down the tag
alias map before the extraction adapter is written (see the NVDA capex quirk:
`PaymentsToAcquirePropertyPlantAndEquipment` goes cumulative-YTD and appears to
stop ~2020, so a naive tag map would silently under-report capex).
"""

from __future__ import annotations

import json
import time
import urllib.request

COMPANIES = {
    "NVDA": "0001045810",
    "ORCL": "0001341439",
    "MSFT": "0000789019",
    "META": "0001326801",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
}

# Candidate tags for each fundamental, most-common first.
TAGS = {
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalExpenditures",
    ],
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "ar": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
}

UA = {"User-Agent": "risk-monitor maintainer@example.invalid"}


def fetch(cik: str) -> dict:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def latest_annual(obs: list[dict]) -> dict | None:
    """The most recent fact whose duration spans ~1 fiscal year (start->end)."""
    annual = [
        o for o in obs
        if o.get("start") and o.get("end") and o.get("val") is not None
    ]
    if not annual:
        return None
    annual.sort(key=lambda o: o.get("end", ""))
    return annual[-1]


def main() -> None:
    for ticker, cik in COMPANIES.items():
        try:
            data = fetch(cik)
        except Exception as exc:  # noqa: BLE001
            print(f"{ticker}: FETCH FAILED {exc}")
            continue
        usgaap = data["facts"].get("us-gaap", {})
        print(f"=== {ticker} ({cik}) ===")
        for metric, tags in TAGS.items():
            for tag in tags:
                if tag not in usgaap:
                    continue
                units = usgaap[tag]["units"]
                unit = next((u for u in units if u in ("USD", "USD/shares")), None)
                if unit is None:
                    continue
                obs = units[unit]
                a = latest_annual(obs)
                if a is None:
                    continue
                print(f"  {metric:8s} tag={tag}")
                print(f"           end={a['end']} start={a['start']} val={a['val']:,.0f} form={a.get('form')} fp={a.get('fp')}")
                break  # first tag with data wins
        time.sleep(0.5)


if __name__ == "__main__":
    main()
