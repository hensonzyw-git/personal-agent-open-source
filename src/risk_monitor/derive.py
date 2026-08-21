"""Pure derivation functions over raw time series.

Each takes a list of ``(date, value)`` ascending and returns a derived value,
or ``None`` when the series is too short / missing (fail-closed, never a
fabricated number).
"""

from __future__ import annotations

from typing import Optional, Sequence

Obs = tuple[str, Optional[float]]


def _non_null(series: Sequence[Obs]) -> list[tuple[str, float]]:
    return [(d, v) for d, v in series if v is not None]


def pct_vs_200dma(series: Sequence[Obs]) -> Optional[float]:
    """(latest close / 200-day simple mean - 1) * 100, in percent."""
    clean = _non_null(series)
    if len(clean) < 200:
        return None
    latest = clean[-1][1]
    window = [v for _, v in clean[-200:]]
    dma = sum(window) / 200.0
    if dma == 0:
        return None
    return round((latest / dma - 1.0) * 100.0, 4)


def change_over_days(series: Sequence[Obs], days: int, *, basis_points: bool = False) -> Optional[float]:
    """``latest - value `days` trading days ago``. Returns percentage points by
    default; ``basis_points=True`` multiplies by 100 (for OAS bp change)."""
    clean = _non_null(series)
    if len(clean) <= days:
        return None
    change = clean[-1][1] - clean[-1 - days][1]
    return round(change * (100.0 if basis_points else 1.0), 4)


def latest(series: Sequence[Obs]) -> Optional[tuple[str, float]]:
    clean = _non_null(series)
    return clean[-1] if clean else None
