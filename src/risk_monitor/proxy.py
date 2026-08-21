"""Free secondary-data proxies for qualitative CSS indicators (policy ``proxies``).

``css.ai_basket`` and ``css.term_financing`` have no free daily primary source,
so each is mapped from a free proxy input to the indicator's band label:

- ``ai_basket``       <- six-name equity drawdown vs 200dma (credit-stress proxy)
- ``term_financing``  <- 30Y treasury 20d change (term/tradability proxy)

Pure functions: deterministic given (policy, inputs), no I/O. A proxy returns
``None`` (not a fabricated "green") when its input is insufficient, so the
indicator stays ``unavailable`` and renormalises exactly as the engine expects.
"""

from __future__ import annotations

from typing import Optional

from risk_monitor import derive
from risk_monitor.scoring.engine import score_numeric


def term_financing_proxy(
    policy: dict,
    dgs30_history: Optional[list],
) -> tuple[Optional[str], Optional[float]]:
    """Map the 30Y treasury 20d change (basis points) to a band label.

    Returns ``(label, change_bp)``; both are ``None`` when the series is too
    short for a 20-day change, leaving ``credit.term_financing`` unavailable."""
    if not dgs30_history:
        return None, None
    change_bp = derive.change_over_days(dgs30_history, 20, basis_points=True)
    if change_bp is None:
        return None, None
    label, _ = score_numeric(change_bp, policy["proxies"]["term_financing"]["bands"])
    return label, change_bp


def ai_basket_proxy(
    policy: dict,
    name_closes: dict[str, list],
) -> tuple[Optional[str], dict[str, Optional[float]]]:
    """Map the six names' equity drawdown vs 200dma to an ai_basket band label.

    Returns ``(label, per_name_pct)``. ``label`` is ``None`` (unavailable) when
    fewer than ``aggregate.min_names`` names produced a signal, so a partial
    Yahoo pull fails closed instead of scoring a subset. ``per_name_pct`` is the
    per-name ``pct_vs_200dma`` (``None`` for a name with no signal), kept for
    explainability."""
    per_name_bands = policy["proxies"]["ai_basket"]["per_name_bands"]
    agg = policy["proxies"]["ai_basket"]["aggregate"]

    per_name_pct: dict[str, Optional[float]] = {}
    orange_plus = 0
    yellow_plus = 0
    for entity_id, series in name_closes.items():
        pct = derive.pct_vs_200dma(series) if series else None
        per_name_pct[entity_id] = pct
        if pct is None:
            continue
        label, _ = score_numeric(pct, per_name_bands)
        if label in ("orange", "red"):
            orange_plus += 1
            yellow_plus += 1
        elif label == "yellow":
            yellow_plus += 1

    if len([p for p in per_name_pct.values() if p is not None]) < agg["min_names"]:
        return None, per_name_pct
    if orange_plus >= agg["orange_names"]:
        return "orange", per_name_pct
    if yellow_plus >= agg["yellow_names"]:
        return "yellow", per_name_pct
    return "green", per_name_pct
