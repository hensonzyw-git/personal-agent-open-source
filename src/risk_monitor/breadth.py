"""Breadth computation: % of S&P 500 constituents above their own 200dma.

Pure functions over per-ticker daily closes (ADR-0001: breadth is
self-computed, not pulled). No I/O, no network. A ticker contributes a signal
only on days it has at least ``window`` prior closes; days where no ticker has
a signal are skipped, so a coverage drop shrinks the denominator rather than
fabricating a percentage.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

Obs = tuple[str, Optional[float]]  # (date_iso, close_or_None)
WINDOW = 200


def _non_null(series: Sequence[Obs]) -> list[tuple[str, float]]:
    return [(d, v) for d, v in series if v is not None]


def _rolling_above(series: Sequence[Obs], window: int = WINDOW) -> list[tuple[str, bool]]:
    """For each day from the ``window``-th close onward, ``(date, close > 200dma)``.

    The moving average includes the current close (same convention as
    ``derive.pct_vs_200dma``). Returns ``[]`` when the series is shorter than
    ``window``."""
    clean = _non_null(series)
    if len(clean) < window:
        return []
    running = sum(v for _, v in clean[:window])
    out: list[tuple[str, bool]] = []
    for i in range(window, len(clean) + 1):
        dma = running / window
        out.append((clean[i - 1][0], clean[i - 1][1] > dma))
        if i < len(clean):
            running += clean[i][1] - clean[i - window][1]
    return out


def breadth_series(
    series_by_ticker: dict[str, Sequence[Obs]],
    window: int = WINDOW,
) -> list[tuple[str, float]]:
    """Per-date breadth = % of tickers with a signal that are above their 200dma.

    ``series_by_ticker`` maps ticker -> ascending ``(date, close)``. The date
    axis is the union of each ticker's signal days (trading days); a ticker
    missing that day simply leaves the denominator for that day."""
    flags = {t: dict(_rolling_above(s, window)) for t, s in series_by_ticker.items()}
    dates = sorted({d for f in flags.values() for d in f})
    out: list[tuple[str, float]] = []
    for d in dates:
        have = [f[d] for f in flags.values() if d in f]
        if not have:
            continue
        pct = round(sum(1 for b in have if b) / len(have) * 100.0, 4)
        out.append((d, pct))
    return out


def breadth_today(
    series_by_ticker: dict[str, Sequence[Obs]],
    window: int = WINDOW,
) -> Optional[tuple[str, float]]:
    bs = breadth_series(series_by_ticker, window)
    return bs[-1] if bs else None


def breadth_change(
    series_by_ticker: dict[str, Sequence[Obs]],
    days: int = 20,
    window: int = WINDOW,
) -> Optional[float]:
    """Latest breadth minus breadth ``days`` trading days earlier, in percentage
    points. ``None`` when there are fewer than ``days + 1`` signal days."""
    bs = breadth_series(series_by_ticker, window)
    if len(bs) <= days:
        return None
    return round(bs[-1][1] - bs[-1 - days][1], 4)


def coverage(series_by_ticker: dict[str, Sequence[Obs]], window: int = WINDOW,
             *, as_of: str | None = None) -> float:
    """Fraction with a signal on the target date, not merely sometime in history."""
    total = len(series_by_ticker)
    if total == 0:
        return 0.0
    if as_of is None:
        as_of = max((d for s in series_by_ticker.values() for d, v in s if v is not None), default=None)
    with_signal = sum(1 for s in series_by_ticker.values()
                      if any(d == as_of for d, _ in _rolling_above(s, window)))
    return round(with_signal / total, 4)
