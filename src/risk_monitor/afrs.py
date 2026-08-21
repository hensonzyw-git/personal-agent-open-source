"""AI Fundamental Risk Score (AFRS) extraction from EDGAR fundamentals.

Deterministic extraction of the three auto-extractable numeric indicators
(``capex_ocf_pct``, ``fcf_ocf_pct``, ``ar_dso_concentration``) from a company's
``companyfacts`` XBRL JSON, plus a set of hard validation checks.

The four qualitative indicators (leverage financing, commitments, RPO quality,
linked financing) are **not** auto-derived: they need reading the MD&A and
footnotes, and inventing a number for them would be exactly the kind of
plausible-but-wrong figure this project refuses to ship. They are therefore
left ``unavailable`` and their weight renormalised, until a human- or
AI-reviewed import supplies them.

The "AI verification" Henson asked for is encoded as *hard validators* — the
checks a reviewer would apply are written as deterministic rules that attach a
``confidence`` (high/medium/low) and ``flags`` to the extraction, rather than a
live model call whose output a deterministic scoring pipeline could never fully
trust. A flagged extraction lowers confidence and is surfaced, never silently
corrected.
"""

from __future__ import annotations

from typing import Optional

CAPEX_TAGS = [
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForCapitalExpenditures",
]
OCF_TAGS = ["NetCashProvidedByUsedInOperatingActivities"]
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]
AR_TAGS = ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"]


def _usd_obs(usgaap: dict, aliases: list[str]) -> tuple[list[dict], Optional[str]]:
    for tag in aliases:
        if tag not in usgaap:
            continue
        units = usgaap[tag].get("units", {})
        unit = "USD" if "USD" in units else next(
            (u for u in units if u.startswith("USD")), None
        )
        if unit is None:
            continue
        obs = [o for o in units[unit] if o.get("val") is not None]
        if obs:
            return obs, tag
    return [], None


def _annual_facts(usgaap: dict, aliases: list[str]) -> tuple[list[dict], Optional[str]]:
    obs, tag = _usd_obs(usgaap, aliases)
    annual = [o for o in obs if o.get("form") == "10-K" and o.get("fp") == "FY"]
    annual.sort(key=lambda o: o.get("end", ""))
    return annual, tag


def _instant_facts(usgaap: dict, aliases: list[str]) -> tuple[list[dict], Optional[str]]:
    obs, tag = _usd_obs(usgaap, aliases)
    instants = [o for o in obs if o.get("end")]
    instants.sort(key=lambda o: o.get("end", ""))
    return instants, tag


def _prev_by_year(facts: list[dict], end: str) -> Optional[dict]:
    """The fact closest to one year before ``end`` (for YoY growth)."""
    from datetime import date as _date

    try:
        target = _date.fromisoformat(end).replace(year=_date.fromisoformat(end).year - 1)
    except ValueError:
        return None
    prior = [o for o in facts if o["end"] < end]
    if not prior:
        return None
    return min(prior, key=lambda o: abs((_date.fromisoformat(o["end"]) - target).days))


def extract_fundamentals(facts_json: dict) -> dict:
    """Latest-fiscal-year fundamentals for one company, with method + evidence.

    Returns ``{capex, ocf, revenue, ar, ar_prior, rev_prior, method, evidence}``;
    any field may be ``None`` when the tag is absent or has no 10-K/FY fact."""
    usgaap = (facts_json.get("facts") or {}).get("us-gaap", {})

    capex_facts, capex_tag = _annual_facts(usgaap, CAPEX_TAGS)
    ocf_facts, ocf_tag = _annual_facts(usgaap, OCF_TAGS)
    rev_facts, rev_tag = _annual_facts(usgaap, REVENUE_TAGS)
    ar_facts, ar_tag = _instant_facts(usgaap, AR_TAGS)

    def annual(facts: list[dict]) -> Optional[dict]:
        return facts[-1] if facts else None

    capex = annual(capex_facts)
    ocf = annual(ocf_facts)
    rev = annual(rev_facts)
    ar = ar_facts[-1] if ar_facts else None
    ar_prior = _prev_by_year(ar_facts, ar["end"]) if ar else None
    rev_prior = rev_facts[-2] if len(rev_facts) >= 2 else None

    return {
        "capex": capex["val"] if capex else None,
        "ocf": ocf["val"] if ocf else None,
        "revenue": rev["val"] if rev else None,
        "ar": ar["val"] if ar else None,
        "ar_prior": ar_prior["val"] if ar_prior else None,
        "rev_prior": rev_prior["val"] if rev_prior else None,
        "method": {
            "capex": {"tag": capex_tag, "end": capex["end"] if capex else None},
            "ocf": {"tag": ocf_tag, "end": ocf["end"] if ocf else None},
            "revenue": {"tag": rev_tag, "end": rev["end"] if rev else None},
            "ar": {"tag": ar_tag, "end": ar["end"] if ar else None},
        },
        "evidence": {
            "capex_prior": capex_facts[-2]["val"] if len(capex_facts) >= 2 else None,
        },
    }


def compute_values(fund: dict) -> dict[str, float]:
    """``metric_id -> value`` for the auto-extractable AFRS indicators. A metric
    is omitted (not zero) when its inputs are missing or degenerate."""
    out: dict[str, float] = {}
    capex, ocf = fund.get("capex"), fund.get("ocf")
    if capex is not None and ocf not in (None, 0):
        out["company.capex_ocf_pct"] = round(capex / ocf * 100.0, 4)
        out["company.fcf_ocf_pct"] = round((ocf - capex) / ocf * 100.0, 4)

    ar, ar_prior = fund.get("ar"), fund.get("ar_prior")
    rev, rev_prior = fund.get("revenue"), fund.get("rev_prior")
    if None not in (ar, ar_prior, rev, rev_prior) and ar_prior > 0 and rev_prior > 0:
        ar_growth = (ar / ar_prior - 1.0) * 100.0
        rev_growth = (rev / rev_prior - 1.0) * 100.0
        out["company.ar_dso_concentration"] = round(ar_growth - rev_growth, 4)
    return out


def validate(fund: dict, values: dict[str, float]) -> tuple[str, list[str]]:
    """Hard validation checks; returns ``(confidence, flags)``.

    ``confidence`` is one of high/medium/low and reflects *extraction* trust,
    not the business signal (the scoring bands already grade that)."""
    flags: list[str] = []
    capex_tag = (fund.get("method") or {}).get("capex", {}).get("tag")
    if capex_tag == "PaymentsToAcquirePropertyPlantAndEquipment":
        # The legacy tag: some filers switched off it mid-series, so a value
        # under it may silently truncate a switch. Lower, not fail.
        flags.append("legacy_capex_tag")

    capex, ocf = fund.get("capex"), fund.get("ocf")
    if ocf is not None and ocf <= 0:
        flags.append("non_positive_ocf")
    if capex is not None and ocf is not None and ocf > 0 and capex > ocf * 1.5:
        flags.append("capex_far_exceeds_ocf")  # real for AI capex, still notable

    prior_capex = (fund.get("evidence") or {}).get("capex_prior")
    if capex and prior_capex and prior_capex > 0 and capex / prior_capex > 3.0:
        flags.append("capex_more_than_3x_prior")

    if "company.ar_dso_concentration" in values and abs(values["company.ar_dso_concentration"]) > 60:
        flags.append("ar_revenue_divergence")

    if not flags:
        return "high", []
    if "non_positive_ocf" in flags or "ar_revenue_divergence" in flags:
        return "low", flags
    return "medium", flags
