"""Qualitative-indicator proxy tests (policy ``proxies``, pure).

Both proxies are deterministic mappings from a free secondary-data input to a
band label, and both must fail closed: when the input is too short / sparse to
produce a signal, the proxy returns ``None`` (indicator stays ``unavailable``)
rather than a fabricated ``green``.
"""

from __future__ import annotations

import pytest

from risk_monitor.proxy import ai_basket_proxy, term_financing_proxy
from risk_monitor.scoring.policy import load_policy


@pytest.fixture(scope="module")
def policy():
    return load_policy()


def _close_series(pct_target: float, base: float = 100.0) -> list[tuple[str, float]]:
    """200 base points plus one offset final point so ``pct_vs_200dma`` lands
    inside the band for ``pct_target``. Targets are chosen far from band edges
    (green +5, yellow -5, orange -15, red -30) so the 200dma-window offset is
    immaterial."""
    latest = base * (1.0 + pct_target / 100.0)
    return [(f"d{i:03d}", base) for i in range(200)] + [("d200", latest)]


# ---------------------------------------------------------------------------
# term_financing <- 30Y 20d change (basis points)
# ---------------------------------------------------------------------------


def _yield_series(first: float, last: float, n: int = 21) -> list[tuple[str, float]]:
    """A ``n``-point yield series running linearly from ``first`` to ``last``.
    ``change_over_days(_, 20)`` reads last - first, so a 21-point series gives a
    clean 20-trading-day change."""
    step = (last - first) / (n - 1)
    return [(f"d{i:03d}", round(first + step * i, 4)) for i in range(n)]


def test_term_financing_band_mapping(policy):
    # 50 bp over 20 days -> orange [50, 75)
    label, change_bp = term_financing_proxy(policy, _yield_series(4.0, 4.5))
    assert label == "orange"
    assert change_bp == pytest.approx(50.0)

    assert term_financing_proxy(policy, _yield_series(4.0, 4.10))[0] == "green"  # 10 bp
    assert term_financing_proxy(policy, _yield_series(4.0, 4.25))[0] == "yellow"  # 25 bp
    assert term_financing_proxy(policy, _yield_series(4.0, 4.75))[0] == "red"  # 75 bp


def test_term_financing_fails_closed(policy):
    # Empty or too-short series -> (None, None), not a fabricated green.
    assert term_financing_proxy(policy, []) == (None, None)
    assert term_financing_proxy(policy, _yield_series(4.0, 4.5, n=20)) == (None, None)


# ---------------------------------------------------------------------------
# ai_basket <- six-name equity drawdown vs 200dma
# ---------------------------------------------------------------------------


def test_ai_basket_aggregate_orange(policy):
    # two names at orange/red -> orange
    closes = {
        "NVDA": _close_series(-15.0),
        "ORCL": _close_series(-30.0),
        "MSFT": _close_series(5.0),
        "META": _close_series(5.0),
        "AMZN": _close_series(5.0),
        "GOOGL": _close_series(5.0),
    }
    label, per_name = ai_basket_proxy(policy, closes)
    assert label == "orange"
    assert per_name["NVDA"] == pytest.approx(-15.0, abs=0.1)
    assert per_name["ORCL"] == pytest.approx(-30.0, abs=0.2)


def test_ai_basket_aggregate_yellow(policy):
    # one name at yellow, none at orange -> yellow
    closes = {
        "NVDA": _close_series(-5.0),
        "ORCL": _close_series(5.0),
        "MSFT": _close_series(5.0),
        "META": _close_series(5.0),
        "AMZN": _close_series(5.0),
        "GOOGL": _close_series(5.0),
    }
    label, _ = ai_basket_proxy(policy, closes)
    assert label == "yellow"


def test_ai_basket_all_green(policy):
    closes = {name: _close_series(5.0) for name in
              ("NVDA", "ORCL", "MSFT", "META", "AMZN", "GOOGL")}
    label, _ = ai_basket_proxy(policy, closes)
    assert label == "green"


def test_ai_basket_insufficient_names_fails_closed(policy):
    # Only 3 of 6 names produced a signal (< min_names=4) -> unavailable.
    closes = {
        "NVDA": _close_series(-15.0),
        "ORCL": _close_series(-15.0),
        "MSFT": _close_series(-15.0),
        # META/AMZN/GOOGL missing entirely
    }
    label, per_name = ai_basket_proxy(policy, closes)
    assert label is None
    assert set(per_name) == {"NVDA", "ORCL", "MSFT"}


def test_ai_basket_short_series_counts_as_no_signal(policy):
    # A name whose series is too short for a 200dma (or missing) contributes no
    # signal and does not count toward min_names: only 3 names have a signal, so
    # the basket stays unavailable.
    closes = {
        "NVDA": _close_series(-15.0),
        "ORCL": _close_series(-15.0),
        "MSFT": _close_series(-15.0),
        "META": [("d0", 100.0)] * 10,  # too short for a 200dma
        "AMZN": None,
        "GOOGL": None,
    }
    label, per_name = ai_basket_proxy(policy, closes)
    assert label is None
    assert per_name["META"] is None
    assert per_name["AMZN"] is None
