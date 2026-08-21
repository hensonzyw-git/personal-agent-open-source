"""Pure scoring and state-machine functions.

No I/O, no database, no network. Every function is deterministic given
(policy, values), so identical inputs reproduce identical scores and states —
the PRD's ``replay --as-of`` requirement.

The state machine is deliberately a monotonic-upgrade / hysteretic-downgrade
ladder, not a total function over (mbs, css, afrs): the PRD's per-state rows
are *necessary* conditions, and gaps (e.g. ``css >= 55`` with neither ``afrs``
nor ``mbs`` confirming) mean "no upgrade", which falls back to holding the
current state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

BAND_SCORE = {"green": 0, "yellow": 35, "orange": 65, "red": 100}
BANDS = ("green", "yellow", "orange", "red")
STATE_ORDER = ("NORMAL", "RISK_ACCUMULATION", "CREDIT_CONFIRMATION", "DELEVERAGING")

_QUALITATIVE = "qualitative"


def score_numeric(value: float, bands: dict) -> tuple[str, float]:
    """Map a numeric value to its (band, score) using lo-inclusive / hi-exclusive
    bounds; ``None`` bound means unbounded. First matching band wins."""
    for label in BANDS:
        b = bands[label]
        lo, hi = b.get("lo"), b.get("hi")
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return label, BAND_SCORE[label]
    raise ValueError(f"value {value!r} matched no band in {bands!r}")


@dataclass
class IndicatorResult:
    metric_id: str
    weight: float
    available: bool
    score: float | None = None
    band: str | None = None
    value: float | None = None
    reason: str | None = None


@dataclass
class ScoreOutcome:
    score: float | None
    results: list[IndicatorResult]
    covered_weight: float
    unavailable: list[str]

    @property
    def early_warning_count(self) -> int:
        """Count of available indicators at orange/red (score >= 65)."""
        return sum(1 for r in self.results if r.available and r.score is not None and r.score >= 65)


def score_indicators(
    indicators: Iterable[dict],
    values: dict[str, float | str],
    unavailable: set[str],
) -> list[IndicatorResult]:
    results: list[IndicatorResult] = []
    for ind in indicators:
        mid = ind["metric_id"]
        w = ind["weight"]
        if mid in unavailable:
            results.append(IndicatorResult(mid, w, False, reason="not_disclosed"))
        elif ind.get("type") == _QUALITATIVE:
            if mid not in values:
                results.append(IndicatorResult(mid, w, False, reason="qualitative_unavailable"))
            else:
                label = values[mid]
                results.append(IndicatorResult(mid, w, True, score=BAND_SCORE[label], band=label))
        elif mid not in values:
            results.append(IndicatorResult(mid, w, False, reason="missing_value"))
        else:
            label, sc = score_numeric(float(values[mid]), ind["bands"])
            results.append(IndicatorResult(mid, w, True, score=sc, band=label, value=float(values[mid])))
    return results


def weighted_score(results: list[IndicatorResult]) -> ScoreOutcome:
    """Weighted mean over available indicators, renormalised by covered weight.

    A fully-unavailable score returns ``score=None`` (caller must treat it as a
    data-quality condition, never as 0/green)."""
    avail = [r for r in results if r.available]
    unavailable = [r.metric_id for r in results if not r.available]
    covered = sum(r.weight for r in avail)
    if not avail or covered <= 0:
        return ScoreOutcome(None, results, 0.0, unavailable)
    score = sum(r.score * r.weight for r in avail) / covered
    return ScoreOutcome(round(score, 4), results, round(covered, 4), unavailable)


def compute_score(
    score_key: str,
    policy: dict,
    values: dict[str, float | str],
    unavailable: frozenset[str] = frozenset(),
) -> ScoreOutcome:
    inds_key = "company_indicators" if score_key == "afrs" else "indicators"
    results = score_indicators(policy[score_key][inds_key], values, set(unavailable))
    return weighted_score(results)


def _applies_to(indicator: dict, company: str | None) -> bool:
    """An indicator applies to a company when its ``applies`` list is absent
    (whole-cohort) or names the company. ``company=None`` means "no specific
    company" and applies every indicator."""
    if company is None:
        return True
    applies = indicator.get("applies")
    return applies is None or company in applies


def company_afrs(
    policy: dict,
    values: dict[str, float | str],
    unavailable: frozenset[str] = frozenset(),
    *,
    company: str | None = None,
) -> ScoreOutcome:
    """One company's AI Fundamental Risk Score over its *applicable* indicators.

    ``company`` is the entity_id (ticker). An indicator whose ``applies`` list
    omits the company is excluded entirely — it is not "unavailable" (which
    would renormalise its weight into the company's denominator and silently
    change the score), it is simply not part of that company's score at all.
    With ``company=None`` the full indicator set is scored (the convenience form
    used by tests and callers that have not resolved a name)."""
    indicators = policy["afrs"]["company_indicators"]
    if company is not None:
        indicators = [ind for ind in indicators if _applies_to(ind, company)]
    results = score_indicators(indicators, values, set(unavailable))
    return weighted_score(results)


def sector_afrs(
    policy: dict,
    company_scores: dict[str, float],
    chain_red_flag: float | None = None,
) -> float | None:
    """Sector AFRS = 60% six-company median + 25% worst-two mean + 15% chain
    red-flag score (PRD §5.3). ``chain_red_flag`` is 0-100 and defaults to 0
    when no chain-level linked-financing flag is established."""
    if not company_scores:
        return None
    agg = policy["afrs"]["sector_aggregation"]
    vals = sorted(company_scores.values())
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    worst_two = vals[-2:] if n >= 2 else vals
    worst_two_mean = sum(worst_two) / len(worst_two)
    chain = chain_red_flag if chain_red_flag is not None else 0.0
    return round(agg["median_six"] * median + agg["worst_two_mean"] * worst_two_mean + agg["chain_red_flags"] * chain, 4)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass
class Scores:
    mbs: float | None = None
    css: float | None = None
    afrs: float | None = None


@dataclass
class Indication:
    """The raw daily signal from scores + composite rules. ``state=None`` means
    the confirmation scores are too incomplete to indicate (data-quality
    condition); callers must retain the last confirmed state, not downgrade."""

    state: str | None
    reasons: list[str] = field(default_factory=list)


def indicated_state(
    scores: Scores,
    *,
    red_combo: bool = False,
    major_override: bool = False,
    early_warning_count: int = 0,
) -> Indication:
    if major_override:
        return Indication("DELEVERAGING", ["major_fact_override"])
    mbs, css, afrs = scores.mbs, scores.css, scores.afrs
    if mbs is None or css is None:
        return Indication(None, ["missing confirmation score -> DATA_QUALITY_WARNING"])

    if red_combo or (css >= 75 and mbs >= 70):
        reasons: list[str] = []
        if red_combo:
            reasons.append("systemic_red_combo_v1")
        if css >= 75 and mbs >= 70:
            reasons.append("css>=75 and mbs>=70")
        return Indication("DELEVERAGING", reasons)

    if (afrs is not None and afrs >= 55 and css >= 55) or (css >= 70 and mbs >= 55):
        if afrs is not None and afrs >= 55 and css >= 55:
            return Indication("CREDIT_CONFIRMATION", ["afrs>=55 and css>=55"])
        return Indication("CREDIT_CONFIRMATION", ["css>=70 and mbs>=55"])

    if css < 55 and ((afrs is not None and afrs >= 55) or early_warning_count >= 2):
        reasons: list[str] = []
        if afrs is not None and afrs >= 55:
            reasons.append("afrs>=55")
        if early_warning_count >= 2:
            reasons.append(f"{early_warning_count} market early-warning items")
        return Indication("RISK_ACCUMULATION", reasons)

    return Indication("NORMAL", ["no upgrade condition met"])


def red_combo_today(day: dict, policy: dict) -> bool:
    """True when a single day satisfies the systemic-red composite. Persistence
    ("for_trading_days") is deliberately NOT applied here — the StateTracker's
    uniform ``upgrade_days`` owns it, so the combo path and the score path share
    one 5-day confirmation instead of stacking two gates. ``day`` is keyed by
    metric_id."""
    conds = policy["state_machine"]["systemic_red_combo"]["when_all"]
    for c in conds:
        value = day.get(c["metric"])
        if value is None:
            return False
        op, threshold = c["op"], c["value"]
        if op == "<" and not value < threshold:
            return False
        if op == ">" and not value > threshold:
            return False
    return True


class StateTracker:
    """Applies PRD §4.1 hysteresis over a daily sequence of Indications.

    Upgrade: jump to the indicated (higher) state after ``upgrade_days``
    consecutive days at-or-above it. Downgrade: step down exactly one level
    after ``downgrade_days`` consecutive days at-or-below it. A ``None``
    indication (data-quality) retains the state and does not reset or feed the
    streak."""

    def __init__(self, policy: dict, state: str = "NORMAL"):
        sm = policy["state_machine"]
        self.state = state
        self.up_days = sm["upgrade_days"]
        self.down_days = sm["downgrade_days"]
        self._dir = 0
        self._count = 0

    def feed(self, indication: Indication | None) -> tuple[str, bool]:
        """Return ``(state, changed)``."""
        if indication is None or indication.state is None:
            return self.state, False

        ci = STATE_ORDER.index(self.state)
        ii = STATE_ORDER.index(indication.state)
        if ii > ci:
            direction, needed, target = 1, self.up_days, indication.state
        elif ii < ci:
            direction, needed, target = -1, self.down_days, STATE_ORDER[ci - 1]
        else:
            self._dir = 0
            self._count = 0
            return self.state, False

        self._count = self._count + 1 if direction == self._dir else 1
        self._dir = direction
        if self._count >= needed:
            self.state = target
            self._dir = 0
            self._count = 0
            return self.state, True
        return self.state, False
