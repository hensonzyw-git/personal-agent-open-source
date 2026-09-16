"""Derivation-function tests (pure, no network)."""

from __future__ import annotations

import pytest

from risk_monitor import derive


def _series(values):
    return [(f"2026-01-{i+1:02d}", v) for i, v in enumerate(values)]


def test_pct_vs_200dma_flat_is_zero():
    s = _series([100.0] * 201)
    assert derive.pct_vs_200dma(s) == 0.0


def test_pct_vs_200dma_above_dma():
    # 200-day window includes the latest close: 199 x 100 + 110 -> DMA 100.05,
    # so (110 / 100.05 - 1) * 100 = 9.945, not 10.0.
    s = _series([100.0] * 200 + [110.0])
    assert derive.pct_vs_200dma(s) == pytest.approx(9.945, abs=1e-3)


def test_pct_vs_200dma_too_short_returns_none():
    assert derive.pct_vs_200dma(_series([100.0] * 199)) is None


def test_change_over_days():
    s = _series([1.0, 1.1, 1.3])
    assert derive.change_over_days(s, 1) == 0.2
    assert derive.change_over_days(s, 1, basis_points=True) == 20.0


def test_change_over_days_insufficient():
    assert derive.change_over_days(_series([1.0, 2.0]), 5) is None


def test_latest_skips_missing():
    s = [("2026-01-01", 2.0), ("2026-01-02", None), ("2026-01-03", 2.5)]
    assert derive.latest(s) == ("2026-01-03", 2.5)
