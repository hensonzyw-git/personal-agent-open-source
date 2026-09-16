"""AFRS extraction + scoring tests (synthetic EDGAR JSON, no network)."""

from __future__ import annotations

import httpx
import pytest

from risk_monitor import afrs as afrs_mod
from risk_monitor.daily import collect_afrs
from risk_monitor.ingestion.edgar import EdgarClient, EdgarError
from risk_monitor.scoring import company_afrs, sector_afrs
from risk_monitor.scoring.policy import load_policy


def _fact(start, end, val, form="10-K", fp="FY"):
    return {"start": start, "end": end, "val": val, "form": form, "fp": fp}


def _instant(end, val):
    return {"end": end, "val": val}


def _companyfacts():
    """A healthy company: capex/ocf/revenue all under current tags, 2 FY rows each."""
    usgaap = {
        "PaymentsToAcquireProductiveAssets": {"units": {"USD": [
            _fact("2024-01-29", "2025-01-26", 30_000_000_000),
            _fact("2023-01-30", "2024-01-28", 10_000_000_000),
        ]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            _fact("2024-01-29", "2025-01-26", 60_000_000_000),
            _fact("2023-01-30", "2024-01-28", 50_000_000_000),
        ]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            _fact("2024-01-29", "2025-01-26", 100_000_000_000),
            _fact("2023-01-30", "2024-01-28", 100_000_000_000),
        ]}},
        "AccountsReceivableNetCurrent": {"units": {"USD": [
            _instant("2025-01-26", 11_000_000_000),
            _instant("2024-01-28", 10_000_000_000),
        ]}},
    }
    return {"facts": {"us-gaap": usgaap}}


def test_extract_and_compute_healthy():
    fund = afrs_mod.extract_fundamentals(_companyfacts())
    values = afrs_mod.compute_values(fund)

    assert values["company.capex_ocf_pct"] == 50.0       # 30B / 60B
    assert values["company.fcf_ocf_pct"] == 50.0         # (60-30) / 60
    # ar +10% vs revenue flat -> concentration +10.0 pp
    assert values["company.ar_dso_concentration"] == pytest.approx(10.0, abs=1e-6)

    confidence, flags = afrs_mod.validate(fund, values)
    assert confidence == "high"
    assert flags == []


def test_legacy_capex_tag_lowers_confidence():
    facts = _companyfacts()
    usgaap = facts["facts"]["us-gaap"]
    usgaap["PaymentsToAcquirePropertyPlantAndEquipment"] = usgaap.pop(
        "PaymentsToAcquireProductiveAssets")
    fund = afrs_mod.extract_fundamentals(facts)
    values = afrs_mod.compute_values(fund)
    confidence, flags = afrs_mod.validate(fund, values)
    assert "legacy_capex_tag" in flags
    assert confidence == "medium"


def test_non_positive_ocf_is_low():
    facts = _companyfacts()
    usgaap = facts["facts"]["us-gaap"]
    usgaap["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"][0]["val"] = -1_000_000
    fund = afrs_mod.extract_fundamentals(facts)
    values = afrs_mod.compute_values(fund)
    confidence, flags = afrs_mod.validate(fund, values)
    assert "non_positive_ocf" in flags
    assert confidence == "low"


def test_missing_tags_yield_no_score():
    fund = afrs_mod.extract_fundamentals({"facts": {"us-gaap": {}}})
    values = afrs_mod.compute_values(fund)
    assert values == {}
    outcome = company_afrs(load_policy(), values)
    assert outcome.score is None


def test_company_afrs_applies_filtering():
    """An indicator whose ``applies`` list omits the company is *excluded* (not
    "unavailable"), so its weight never renormalises into the company's
    denominator. NVDA is the canonical case: ``capex_ocf_pct`` applies to the
    other five names but not NVDA."""
    policy = load_policy()
    values = {
        "company.capex_ocf_pct": 150.0,   # red — but capex does not apply to NVDA
        "company.fcf_ocf_pct": 50.0,      # green
        "company.ar_dso_concentration": 0.0,  # green
    }
    nvda = company_afrs(policy, values, company="NVDA")
    orcl = company_afrs(policy, values, company="ORCL")

    # NVDA: capex_ocf is excluded entirely, so it is not scored, not unavailable,
    # and its weight is not renormalised. Covered = fcf(18) + ar(10) = 28.
    assert nvda.covered_weight == 28.0
    assert "company.capex_ocf_pct" not in nvda.unavailable
    assert all(r.metric_id != "company.capex_ocf_pct" for r in nvda.results)
    assert nvda.score == 0.0

    # ORCL: capex_ocf applies and is red, so its score must exceed NVDA's.
    assert orcl.score is not None and orcl.score > nvda.score
    capex = next(r for r in orcl.results if r.metric_id == "company.capex_ocf_pct")
    assert capex.available and capex.band == "red"


def test_company_afrs_unqualified_scores_all_indicators():
    """With ``company=None`` every indicator is in scope (the historical /
    convenience form), preserving the prior behaviour for callers that have not
    resolved a name."""
    policy = load_policy()
    values = {"company.fcf_ocf_pct": 50.0}
    full = company_afrs(policy, values)
    assert len(full.results) == len(policy["afrs"]["company_indicators"])


def test_sector_afrs_aggregation():
    policy = load_policy()
    scores = {"NVDA": 20.0, "ORCL": 40.0, "MSFT": 60.0, "META": 80.0}
    # median (40+60)/2 = 50; worst_two (60+80)/2 = 70; chain 0.
    expected = 0.60 * 50.0 + 0.25 * 70.0
    assert sector_afrs(policy, scores) == pytest.approx(expected, abs=1e-6)


def test_sector_afrs_empty_is_none():
    assert sector_afrs(load_policy(), {}) is None


def test_edgar_client_path_and_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/xbrl/companyfacts/CIK0001045810.json"
        assert request.headers["User-Agent"] == "risk-monitor maintainer@example.invalid"
        return httpx.Response(200, json=_companyfacts())

    with EdgarClient(transport=httpx.MockTransport(handler)) as c:
        data = c.company_facts("0001045810")
    assert "facts" in data


def test_edgar_client_404_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    with EdgarClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(EdgarError):
            c.company_facts("0000000000")


class _StubEdgar:
    def __init__(self, ok: dict[str, dict], fail: set[str]):
        self._ok = ok
        self._fail = fail

    def company_facts(self, cik: str) -> dict:
        if cik in self._fail:
            raise EdgarError("boom")
        return self._ok.get(cik, {"facts": {"us-gaap": {}}})


def test_collect_afrs_partitions_failure():
    # One company fails; the rest extract empty fundamentals (no score, no crash).
    ok_cik = "0001045810"
    stub = _StubEdgar(ok={ok_cik: _companyfacts()}, fail={"0001341439"})
    per_company, details = collect_afrs(stub)  # type: ignore[arg-type]

    assert "NVDA" in per_company and per_company["NVDA"]["company.capex_ocf_pct"] == 50.0
    assert "ORCL" not in per_company  # failed company omitted
    assert "error" in details["ORCL"]
    assert details["NVDA"]["confidence"] == "high"
