"""RCS (Treasury + credit) derivations and position-action overlay."""

from __future__ import annotations

from datetime import date, timedelta

from risk_monitor import rates_credit
from risk_monitor.report import build_report, push_summary
from risk_monitor.scoring import Scores, compute_score, indicated_state
from risk_monitor.scoring.policy import load_policy


def _series(end: date, values: list[float]) -> list[tuple[str, float]]:
    start = end - timedelta(days=len(values) - 1)
    return [((start + timedelta(days=i)).isoformat(), value) for i, value in enumerate(values)]


def test_derive_values_joins_treasury_curve_and_hy_change():
    end = date(2026, 8, 20)
    raw = {
        "dgs10_latest": (end.isoformat(), 4.80),
        "dfii10_latest": (end.isoformat(), 2.10),
        "dgs2_latest": (end.isoformat(), 4.20),
        "dgs3mo_latest": (end.isoformat(), 4.50),
        "dgs10_history": _series(end, [4.60] * 21 + [4.80]),
        "dfii10_history": _series(end, [1.95] * 21 + [2.10]),
        "dgs30_history": _series(end, [4.40] * 21 + [4.70]),
    }
    values, meta = rates_credit.derive_values(raw, hy_oas_20d_change_bp=60.0)

    assert values == {
        "rates.10y_yield_pct": 4.8,
        "rates.10y_real_yield_pct": 2.1,
        "rates.10y_2y_spread_bp": 60.0,
        "rates.10y_3m_spread_bp": 30.0,
        "rates.10y_20d_change_bp": 20.0,
        "rates.10y_real_20d_change_bp": 15.0,
        "rates.30y_20d_change_bp": 30.0,
        "rates.hy_oas_20d_change_bp": 60.0,
    }
    assert "rates.move_index" in meta["optional_missing"]
    assert meta["missing_core"] == []


def test_rcs_policy_maps_low_and_high_conditions():
    policy = load_policy()
    green = {
        "rates.10y_yield_pct": 4.4,
        "rates.10y_real_yield_pct": 1.4,
        "rates.10y_20d_change_bp": -1.0,
        "rates.10y_real_20d_change_bp": -1.0,
        "rates.10y_2y_spread_bp": 20.0,
        "rates.10y_3m_spread_bp": 20.0,
        "rates.30y_20d_change_bp": -1.0,
        "rates.hy_oas_20d_change_bp": 10.0,
    }
    red = {
        "rates.10y_yield_pct": 5.0,
        "rates.10y_real_yield_pct": 2.5,
        "rates.10y_20d_change_bp": 25.0,
        "rates.10y_real_20d_change_bp": 20.0,
        "rates.10y_2y_spread_bp": -101.0,
        "rates.10y_3m_spread_bp": -201.0,
        "rates.30y_20d_change_bp": 30.0,
        "rates.hy_oas_20d_change_bp": 100.0,
    }
    assert compute_score("rates_credit", policy, green).score == 0.0
    assert compute_score("rates_credit", policy, red).score == 100.0


def test_crash_trigger_requires_rates_credit_and_market_confirmation():
    values = {
        "rates.10y_yield_pct": 5.1,
        "rates.10y_real_20d_change_bp": 5.0,
        "rates.hy_oas_20d_change_bp": 60.0,
    }
    assert rates_credit.crash_trigger(values, mbs=55.0, css=None) is True
    assert rates_credit.crash_trigger(values, mbs=54.9, css=54.9) is False
    assert rates_credit.crash_trigger({**values, "rates.10y_real_20d_change_bp": 0.0}, mbs=70.0, css=None) is False


def test_position_action_has_caution_easing_and_strict_state_boundaries():
    policy = load_policy()
    caution, reasons = rates_credit.position_action(
        policy,
        state="NORMAL",
        values={"rates.10y_yield_pct": 4.8},
        mbs=10.0,
        css=10.0,
    )
    assert caution == "暂停激进加仓，新增美元资金分批投入"
    assert reasons == ["10y_in_4.7%-5.0%_caution_zone"]

    review, _ = rates_credit.position_action(
        policy,
        state="RISK_ACCUMULATION",
        values={
            "rates.10y_yield_pct": 5.1,
            "rates.10y_real_20d_change_bp": 5.0,
            "rates.hy_oas_20d_change_bp": 60.0,
        },
        mbs=60.0,
        css=40.0,
    )
    assert review == "考虑主动降低权益总仓位（人工确认）"

    easing, _ = rates_credit.position_action(
        policy,
        state="NORMAL",
        values={
            "rates.10y_yield_pct": 4.4,
            "rates.10y_real_20d_change_bp": -5.0,
            "rates.hy_oas_20d_change_bp": 10.0,
        },
        mbs=10.0,
        css=10.0,
    )
    assert easing == "恢复正常加仓速度（分批投入）"

    delev, _ = rates_credit.position_action(
        policy,
        state="DELEVERAGING",
        values={"rates.10y_yield_pct": 4.8},
        mbs=10.0,
        css=10.0,
    )
    assert delev == policy["actions"]["DELEVERAGING"]

    confirmed, reasons = rates_credit.position_action(
        policy,
        state="CREDIT_CONFIRMATION",
        values={
            "rates.10y_yield_pct": 5.1,
            "rates.10y_real_20d_change_bp": 5.0,
            "rates.hy_oas_20d_change_bp": 60.0,
        },
        mbs=60.0,
        css=60.0,
    )
    assert confirmed == policy["actions"]["CREDIT_CONFIRMATION"]
    assert reasons == ["confirmed_state_action"]


def test_rates_credit_can_raise_state_without_treating_10y_alone_as_crash():
    assert indicated_state(
        Scores(mbs=30.0, css=40.0, rates_credit=60.0),
        rates_credit_complete=True,
    ).state == "RISK_ACCUMULATION"
    assert indicated_state(
        Scores(mbs=50.0, css=60.0, rates_credit=70.0),
        rates_credit_complete=True,
    ).state == "CREDIT_CONFIRMATION"
    assert indicated_state(
        Scores(mbs=30.0, css=40.0, rates_credit=90.0),
        rates_credit_trigger=True,
        rates_credit_complete=True,
    ).state == "DELEVERAGING"


def test_incomplete_rcs_cannot_raise_the_state_after_renormalisation():
    policy = load_policy()
    only_nominal = compute_score(
        "rates_credit", policy, {"rates.10y_yield_pct": 5.1}
    )
    assert only_nominal.score == 100.0
    assert indicated_state(
        Scores(mbs=10.0, css=55.0, rates_credit=only_nominal.score)
    ).state == "NORMAL"


def test_incomplete_rcs_crash_trigger_cannot_escalate_state():
    assert indicated_state(
        Scores(mbs=60.0, css=60.0, rates_credit=100.0),
        rates_credit_trigger=True,
        rates_credit_complete=False,
    ).state == "NORMAL"


def test_report_and_push_expose_rcs_when_present():
    report = build_report({
        "as_of": "2026-08-20",
        "mbs": 10.0,
        "css": 20.0,
        "afrs": 30.0,
        "rates_credit": 65.0,
        "state": "RISK_ACCUMULATION",
        "action": "暂停激进加仓，新增美元资金分批投入",
        "rates_credit_components": [
            {"metric_id": "rates.10y_yield_pct", "available": True, "band": "orange", "value": 4.8}
        ],
    })
    assert report["scores"]["rates_credit"] == 65.0
    assert report["components"]["rates_credit"] == [
        {"label": "10Y 美债收益率", "value": "4.80%", "band": "orange"}
    ]
    assert "RCS 65.0" in push_summary(report)


def test_report_keeps_missing_rcs_evidence_visible_on_the_card():
    report = build_report({
        "as_of": "2026-08-20",
        "mbs": 10.0,
        "css": 20.0,
        "afrs": 30.0,
        "rates_credit": 65.0,
        "state": "NORMAL",
        "rates_credit_unavailable": ["rates.move_index"],
        "rates_credit_meta": {
            "missing_core": ["rates.10y_real_yield_pct"],
            "optional_missing": ["rates.move_index", "rates.treasury_liquidity"],
        },
    })
    assert report["components"]["rates_credit"] == [
        {"label": "MOVE", "value": "不可用", "band": "unavailable"},
        {"label": "10Y 实际利率", "value": "不可用", "band": "unavailable"},
        {"label": "国债流动性", "value": "不可用", "band": "unavailable"},
    ]
