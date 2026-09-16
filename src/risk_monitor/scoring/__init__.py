"""Scoring and state-machine — pure, deterministic, versioned against
``spec/scoring_policy.yml``."""

from .engine import (
    BAND_SCORE,
    BANDS,
    STATE_ORDER,
    IndicatorResult,
    ScoreOutcome,
    Scores,
    Indication,
    StateTracker,
    compute_score,
    company_afrs,
    indicated_state,
    red_combo_today,
    score_numeric,
    sector_afrs,
    weighted_score,
)

__all__ = [
    "BAND_SCORE",
    "BANDS",
    "STATE_ORDER",
    "IndicatorResult",
    "ScoreOutcome",
    "Scores",
    "Indication",
    "StateTracker",
    "compute_score",
    "company_afrs",
    "indicated_state",
    "red_combo_today",
    "score_numeric",
    "sector_afrs",
    "weighted_score",
]
