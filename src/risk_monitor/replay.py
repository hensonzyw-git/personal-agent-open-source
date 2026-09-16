"""Replay: reproduce a stored day's scores and state from persisted rows.

PRD §14 requires ``replay --as-of`` to reproduce identical output. The
*guaranteed-reproducible boundary* is "observations -> score -> state": FRED raw
is stored verbatim, but breadth stores its derived series (per-ticker closes are
re-pullable from Tencent, whose raw closes may be revised), so the honest
replay re-derives the score from the stored observations and the state from the
stored score snapshots — never from a fresh network pull.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from risk_monitor.daily import recompute_state
from risk_monitor.domain.models import Observation, ScoreSnapshot
from risk_monitor.domain.storage import create_database_engine
from risk_monitor.scoring import compute_score
from risk_monitor.scoring.policy import load_policy


def _observations_for(session: Session, as_of: date) -> dict[str, dict[str, float | str]]:
    """Group stored observations by score key (mbs/css/rates_credit) and metric_id.

    A qualitative proxy label is persisted with ``value=None`` and its band
    label in ``value_text``; it is a scoring input exactly like a numeric value,
    so it must be read back here or replay silently renormalises the indicator
    away on every normal day."""
    out: dict[str, dict[str, float | str]] = {"mbs": {}, "css": {}, "rates_credit": {}}
    for o in session.scalars(select(Observation).where(Observation.as_of_date == as_of)):
        if o.metric_id.startswith("company."):
            continue
        v = o.value_text if o.value is None and o.value_text is not None else o.value
        if v is None:
            continue
        if o.metric_id.startswith("market."):
            out["mbs"][o.metric_id] = v
        elif o.metric_id.startswith("credit."):
            out["css"][o.metric_id] = v
        elif o.metric_id.startswith("rates."):
            out["rates_credit"][o.metric_id] = v
    return out


def replay_score(session: Session, policy: dict, as_of: date) -> dict:
    """Recompute MBS/CSS/RCS from stored observations and compare to the stored
    snapshot. Returns a comparison record with ``matches`` flags."""
    snap = session.scalars(
        select(ScoreSnapshot).where(ScoreSnapshot.as_of_date == as_of)
    ).all()
    snap = snap[-1] if snap else None
    values = _observations_for(session, as_of)

    mbs = compute_score("mbs", policy, values["mbs"])
    css = compute_score("css", policy, values["css"])
    rates_credit = compute_score("rates_credit", policy, values["rates_credit"])

    stored_mbs = snap.mbs if snap else None
    stored_css = snap.css if snap else None
    return {
        "as_of": as_of.isoformat(),
        "stored": {
            "mbs": stored_mbs,
            "css": stored_css,
            "afrs": snap.afrs if snap else None,
            "rates_credit": snap.rates_credit if snap else None,
        },
        "recomputed": {"mbs": mbs.score, "css": css.score, "rates_credit": rates_credit.score},
        "matches_mbs": snap is not None and _eq(stored_mbs, mbs.score),
        "matches_css": snap is not None and _eq(stored_css, css.score),
        "matches_rates_credit": snap is not None and _eq(
            snap.rates_credit if snap else None, rates_credit.score
        ),
    }


def replay(engine, policy: dict, as_of: date) -> dict:
    """Full replay for ``as_of``: score comparison + state recomputation."""
    with Session(engine) as session:
        score = replay_score(session, policy, as_of)
    # State: recompute only up to and including ``as_of``.
    state, reasons, conf = recompute_state(engine, policy, up_to=as_of)
    score["state"] = state
    score["state_reasons"] = reasons
    score["confirmations"] = conf
    return score


def _eq(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-6


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a stored day's scores and state.")
    parser.add_argument("--as-of", required=True, help="ISO date, e.g. 2026-08-20")
    parser.add_argument("--db", default=None, help="SQLite path (default: data/risk_monitor.db)")
    args = parser.parse_args(argv)

    policy = load_policy()
    engine = create_database_engine(args.db)
    result = replay(engine, policy, date.fromisoformat(args.as_of))

    print(json.dumps(result, ensure_ascii=False, indent=2))
    ok = (
        result.get("matches_mbs", False)
        and result.get("matches_css", False)
        and result.get("matches_rates_credit", False)
    )
    print("REPLAY", "PASS" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
