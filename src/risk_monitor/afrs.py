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
    """Gather equivalent aliases before choosing a period; never stop at an old tag."""
    import math
    rows = []
    for tag in aliases:
        for obs in usgaap.get(tag, {}).get("units", {}).get("USD", []):
            value = obs.get("val")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                rows.append({**obs, "tag": tag})
    return rows, None


def _days(start: str, end: str) -> int:
    from datetime import date
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (ValueError, TypeError):
        return -1


def _deduplicate(rows: list[dict]) -> list[dict]:
    """Newest filing wins per period; ambiguous same-version values are unavailable.

    Keep an unavailable marker at the latest period rather than falling back
    to an older, apparently healthy value. Preserve selected source facts.
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row.get("start"), row["end"]), []).append(row)
    result = []
    for group in groups.values():
        filed = max(r.get("filed", "") for r in group)
        latest = [r for r in group if r.get("filed", "") == filed]
        selected = dict(sorted(latest, key=lambda r: (r.get("tag", ""), r.get("accn", "")))[0])
        if len({r["val"] for r in latest}) != 1:
            selected.update(val=None, error="conflicting_facts")
        result.append(selected)
    return sorted(result, key=lambda r: (r["end"], r.get("start", "")))


def _annual_facts(usgaap: dict, aliases: list[str]) -> tuple[list[dict], Optional[str]]:
    rows, _ = _usd_obs(usgaap, aliases)
    annual = _deduplicate([
        r for r in rows if r.get("form") in {"10-K", "10-K/A"}
        and r.get("fp") == "FY" and 330 <= _days(r.get("start"), r.get("end")) <= 380
    ])
    return annual, annual[-1]["tag"] if annual else None


def _instant_facts(usgaap: dict, aliases: list[str]) -> tuple[list[dict], Optional[str]]:
    rows, _ = _usd_obs(usgaap, aliases)
    instants = _deduplicate([
        r for r in rows if not r.get("start") and _days(r.get("end"), r.get("end")) == 0
    ])
    return instants, instants[-1]["tag"] if instants else None


def _prev_by_year(facts: list[dict], end: str) -> Optional[dict]:
    prior = [r for r in facts if 350 <= _days(r["end"], end) <= 380]
    return min(prior, key=lambda r: abs(_days(r["end"], end) - 365)) if prior else None


def extract_fundamentals(facts_json: dict) -> dict:
    """Annual-only baseline with period-aligned ratios and source evidence.

    Quarterly/TTM is deliberately not inferred from cumulative annual records.
    """
    usgaap = (facts_json.get("facts") or {}).get("us-gaap", {})
    capex_rows, _ = _annual_facts(usgaap, CAPEX_TAGS)
    ocf_rows, _ = _annual_facts(usgaap, OCF_TAGS)
    rev_rows, _ = _annual_facts(usgaap, REVENUE_TAGS)
    ar_rows, _ = _instant_facts(usgaap, AR_TAGS)
    last = lambda rows: rows[-1] if rows else None
    capex, ocf, rev = last(capex_rows), last(ocf_rows), last(rev_rows)
    rev_prior = _prev_by_year(rev_rows, rev["end"]) if rev else None
    # The balance-sheet points must be the exact endpoints of the annual
    # revenue comparison, not each series' independent latest observations.
    ar = next((r for r in ar_rows if rev and r["end"] == rev["end"]), None)
    ar_prior = next((r for r in ar_rows if rev_prior and r["end"] == rev_prior["end"]), None)
    selected = dict(capex=capex, ocf=ocf, revenue=rev, ar=ar, ar_prior=ar_prior, rev_prior=rev_prior)
    invalid = [f"{name}:{r['error']}" for name, r in selected.items() if r and r.get("error")]
    cashflow_aligned = bool(capex and ocf and
        (capex["start"], capex["end"]) == (ocf["start"], ocf["end"]))
    if capex and ocf and not cashflow_aligned:
        invalid.append("cashflow_period_mismatch")
    if rev and (not ar or not ar_prior or not rev_prior):
        invalid.append("receivables_comparison_unavailable")
    prior_capex = _prev_by_year(capex_rows, capex["end"]) if capex else None
    def evidence(row):
        return {k: row.get(k) for k in ("tag", "start", "end", "filed", "accn", "form", "val")} if row else {}
    return {
        **{name: r.get("val") if r else None for name, r in selected.items()},
        "cashflow_aligned": cashflow_aligned,
        "invalid": invalid,
        "method": {name: evidence(selected[name]) for name in ("capex", "ocf", "revenue", "ar")},
        "evidence": {"selected_facts": {name: evidence(r) for name, r in selected.items()},
                     "capex_prior": prior_capex.get("val") if prior_capex else None},
    }


def compute_values(fund: dict) -> dict[str, float]:
    """``metric_id -> value`` for the auto-extractable AFRS indicators. A metric
    is omitted (not zero) when its inputs are missing or degenerate."""
    out: dict[str, float] = {}
    capex, ocf = fund.get("capex"), fund.get("ocf")
    if capex is not None and ocf is not None and ocf > 0 and fund.get("cashflow_aligned", True):
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
    flags: list[str] = list(fund.get("invalid", []))
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
    if fund.get("invalid") or "non_positive_ocf" in flags or "ar_revenue_divergence" in flags:
        return "low", flags
    return "medium", flags
