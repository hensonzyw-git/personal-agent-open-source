"""Scoring + state-machine tests, driven by the frozen policy.

Covers PRD §14 acceptance criteria and the band/renormalisation boundaries.
Everything is offline — no network, no database.
"""

from __future__ import annotations

import pytest

from risk_monitor.scoring import (
    BAND_SCORE,
    Indication,
    STATE_ORDER,
    StateTracker,
    Scores,
    compute_score,
    indicated_state,
    red_combo_today,
    score_numeric,
    sector_afrs,
)
from risk_monitor.scoring.policy import load_policy


@pytest.fixture(scope="module")
def policy():
    return load_policy()


# ---------------------------------------------------------------------------
# Band mapping
# ---------------------------------------------------------------------------


def test_band_boundaries_vix(policy):
    bands = next(i["bands"] for i in policy["mbs"]["indicators"] if i["metric_id"] == "market.vix")
    # green <20, yellow [20,25), orange [25,30), red >=30
    assert score_numeric(19.99, bands) == ("green", 0)
    assert score_numeric(20.0, bands) == ("yellow", 35)
    assert score_numeric(24.99, bands) == ("yellow", 35)
    assert score_numeric(25.0, bands) == ("orange", 65)
    assert score_numeric(29.99, bands) == ("orange", 65)
    assert score_numeric(30.0, bands) == ("red", 100)


def test_band_direction_hy_oas(policy):
    bands = next(i["bands"] for i in policy["css"]["indicators"] if i["metric_id"] == "credit.hy_oas_pct")
    assert score_numeric(3.5, bands) == ("green", 0)
    assert score_numeric(4.5, bands) == ("orange", 65)
    assert score_numeric(6.1, bands) == ("red", 100)


# ---------------------------------------------------------------------------
# Weighted score + renormalisation
# ---------------------------------------------------------------------------


def test_mbs_all_green_all_red(policy):
    green = {
        "market.spx_vs_200dma_pct": 5.0,
        "market.spx_pct_above_200dma": 70.0,
        "market.vix": 15.0,
        "market.breadth_20d_change": 0.0,
        "market.fwd_eps_revisions": "green",
    }
    red = {
        "market.spx_vs_200dma_pct": -10.0,
        "market.spx_pct_above_200dma": 30.0,
        "market.vix": 35.0,
        "market.breadth_20d_change": -25.0,
        "market.fwd_eps_revisions": "red",
    }
    assert compute_score("mbs", policy, green).score == 0.0
    assert compute_score("mbs", policy, red).score == 100.0


def test_weight_renormalisation_when_eps_unavailable(policy):
    # fwd_eps_revisions (weight 20) missing -> covered weight 80.
    values = {
        "market.spx_vs_200dma_pct": -10.0,   # red, weight 25
        "market.spx_pct_above_200dma": 70.0,  # green
        "market.vix": 15.0,                   # green
        "market.breadth_20d_change": 0.0,     # green
    }
    out = compute_score("mbs", policy, values)
    assert out.covered_weight == 80.0
    assert out.score == pytest.approx(25 * 100 / 80)
    assert "market.fwd_eps_revisions" in out.unavailable


def test_fully_unavailable_returns_none(policy):
    out = compute_score("mbs", policy, {})
    assert out.score is None
    assert len(out.unavailable) == 5


# ---------------------------------------------------------------------------
# AFRS aggregation
# ---------------------------------------------------------------------------


def test_sector_afrs_median_and_worst_two(policy):
    company_scores = {
        "NVDA": 10.0,
        "ORCL": 20.0,
        "MSFT": 30.0,
        "META": 40.0,
        "AMZN": 50.0,
        "GOOGL": 60.0,
    }
    median = 35.0          # median of [10,20,30,40,50,60] = 35
    worst_two_mean = 55.0  # mean of [50,60]
    expected = 0.60 * median + 0.25 * worst_two_mean + 0.15 * 0.0
    assert sector_afrs(policy, company_scores) == pytest.approx(expected)


def test_sector_afrs_requires_minimum_company_coverage(policy):
    # A single issuer cannot represent the six-name AI-capex sector.
    assert sector_afrs(policy, {"ORCL": 100.0}) is None
    assert sector_afrs(policy, {
        "ORCL": 10.0, "MSFT": 20.0, "META": 30.0, "AMZN": 40.0,
    }) is not None


# ---------------------------------------------------------------------------
# State machine — PRD §14 acceptance cases
# ---------------------------------------------------------------------------


def test_high_afrs_low_css_is_not_credit_confirmation(policy):
    # afrs >= 55 with css < 55 must be RISK_ACCUMULATION, never CREDIT_CONFIRMATION.
    ind = indicated_state(Scores(mbs=30.0, css=40.0, afrs=60.0))
    assert ind.state == "RISK_ACCUMULATION"


def test_css_55_alone_does_not_upgrade(policy):
    # The PRD's confirmation philosophy: credit stress needs a fundamental or
    # market co-signal. css=60 with neither afrs nor mbs confirming -> no upgrade.
    ind = indicated_state(Scores(mbs=30.0, css=60.0, afrs=40.0))
    assert ind.state == "NORMAL"


def test_single_day_vix_spike_is_not_red(policy):
    # VIX alone (weight 15) cannot push MBS >= 55, so no systemic red.
    values = {
        "market.spx_vs_200dma_pct": 5.0,
        "market.spx_pct_above_200dma": 70.0,
        "market.vix": 40.0,               # red
        "market.breadth_20d_change": 0.0,
        "market.fwd_eps_revisions": "green",
    }
    mbs = compute_score("mbs", policy, values).score
    assert mbs == pytest.approx(100 * 15 / 100)  # only VIX contributes
    assert mbs < 55
    assert indicated_state(Scores(mbs=mbs, css=10.0, afrs=10.0)).state == "NORMAL"


def test_red_combo_five_days_deleveraging(policy):
    day = {
        "market.spx_vs_200dma_pct": -5.0,
        "market.spx_pct_above_200dma": 30.0,
        "credit.hy_oas_pct": 7.0,
    }
    assert red_combo_today(day, policy) is True

    tracker = StateTracker(policy)
    scores = Scores(mbs=50.0, css=50.0, afrs=40.0)  # would be NORMAL without the combo
    for _ in range(4):
        state, changed = tracker.feed(indicated_state(scores, red_combo=True))
    assert state == "NORMAL" and changed is False

    state, changed = tracker.feed(indicated_state(scores, red_combo=True))
    assert state == "DELEVERAGING" and changed is True


def test_upgrade_needs_five_consecutive_days(policy):
    tracker = StateTracker(policy)
    ind = indicated_state(Scores(mbs=30.0, css=40.0, afrs=60.0))  # RISK_ACCUMULATION
    for _ in range(4):
        assert tracker.feed(ind) == ("NORMAL", False)
    assert tracker.feed(ind) == ("RISK_ACCUMULATION", True)


def test_upgrade_streak_resets_when_the_target_state_changes(policy):
    tracker = StateTracker(policy)
    risk_accumulation = Indication("RISK_ACCUMULATION")
    credit_confirmation = Indication("CREDIT_CONFIRMATION")

    assert tracker.feed(risk_accumulation) == ("NORMAL", False)
    for _ in range(4):
        assert tracker.feed(credit_confirmation) == ("NORMAL", False)
    assert tracker.feed(credit_confirmation) == ("CREDIT_CONFIRMATION", True)


def test_downgrade_one_level_after_twenty_days(policy):
    tracker = StateTracker(policy, state="DELEVERAGING")
    ind = indicated_state(Scores(mbs=10.0, css=10.0, afrs=10.0))  # NORMAL
    for _ in range(19):
        state, changed = tracker.feed(ind)
        assert state == "DELEVERAGING" and changed is False
    state, changed = tracker.feed(ind)
    assert state == "CREDIT_CONFIRMATION" and changed is True  # one level down


def test_major_override_is_immediate(policy):
    assert indicated_state(Scores(mbs=10.0, css=10.0, afrs=10.0), major_override=True).state == "DELEVERAGING"


def test_missing_confirmation_score_retains_state(policy):
    # mbs or css missing -> indication is None -> tracker keeps last state.
    ind = indicated_state(Scores(mbs=None, css=10.0, afrs=50.0))
    assert ind.state is None
    tracker = StateTracker(policy, state="RISK_ACCUMULATION")
    assert tracker.feed(ind) == ("RISK_ACCUMULATION", False)


def test_state_order_is_monotonic():
    assert STATE_ORDER == ("NORMAL", "RISK_ACCUMULATION", "CREDIT_CONFIRMATION", "DELEVERAGING")
    assert BAND_SCORE == {"green": 0, "yellow": 35, "orange": 65, "red": 100}
