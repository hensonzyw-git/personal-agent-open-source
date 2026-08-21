"""Daily pipeline: ingest FRED -> derive -> score -> persist snapshots.

Run: ``python -m risk_monitor.daily`` (loads ``FRED_API_KEY`` from ``.env.local``).

The state is always *recomputed* from the full score-snapshot history (replay),
so a fresh run reproduces the identical confirmed state — the PRD's replay
requirement. AFRS is auto-extracted from SEC EDGAR companyfacts (Phase 3) with
hardened validation; a company whose fundamentals cannot be extracted is
omitted from the sector aggregate, never scored zero.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from risk_monitor import afrs as afrs_mod
from risk_monitor import breadth as breadth_mod
from risk_monitor import derive
from risk_monitor.config import load_dotenv_local
from risk_monitor.constituents import load_tickers
from risk_monitor.domain.models import (
    COMPANY_ENTITIES,
    Observation,
    RawArtifact,
    ScoreSnapshot,
    StateSnapshot,
)
from risk_monitor.domain.storage import initialize
from risk_monitor.ingestion.edgar import EdgarClient
from risk_monitor.ingestion.fred import FRED_SERIES, FredClient
from risk_monitor.ingestion.yahoo import YahooClient
from risk_monitor.scoring import (
    Indication,
    Scores,
    ScoreOutcome,
    StateTracker,
    company_afrs,
    compute_score,
    indicated_state,
    red_combo_today,
    sector_afrs,
)
from risk_monitor.scoring.policy import load_policy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_fred(client: FredClient) -> dict[str, object]:
    """Pull the raw series: full history for the two derived inputs (SPX 200dma,
    HY OAS 20d change), latest for the direct inputs."""
    return {
        "spx_history": client.history(FRED_SERIES["market.spx_close"]),
        "hy_oas_history": client.history(FRED_SERIES["credit.hy_oas_pct"]),
        "bbb_oas_latest": client.latest(FRED_SERIES["credit.bbb_oas_pct"]),
        "dgs10_latest": client.latest(FRED_SERIES["treasury.10y_yield"]),
        "dgs30_latest": client.latest(FRED_SERIES["treasury.30y_yield"]),
        "vix_latest": client.latest(FRED_SERIES["market.vix"]),
    }


def derive_market_values(raw: dict[str, object]) -> tuple[dict[str, float], dict, str]:
    """Return ``(mbs_values, css_values, as_of)`` from the FRED raw series.
    Metrics we cannot yet source (breadth, forward EPS, AI basket, term
    financing) are simply omitted — the engine marks them unavailable and
    renormalises. Breadth is merged separately in ``run()``."""
    spx = raw["spx_history"]
    hy = raw["hy_oas_history"]

    spx_vs_200dma = derive.pct_vs_200dma(spx)
    hy_oas = derive.latest(hy)
    hy_oas_pct = hy_oas[1] if hy_oas else None
    hy_oas_20d = derive.change_over_days(hy, 20, basis_points=True)
    spx_latest = derive.latest(spx)
    vix = raw["vix_latest"]
    bbb = raw["bbb_oas_latest"]

    mbs_values: dict[str, float] = {}
    if spx_vs_200dma is not None:
        mbs_values["market.spx_vs_200dma_pct"] = spx_vs_200dma
    if vix is not None and vix[1] is not None:
        mbs_values["market.vix"] = vix[1]

    css_values: dict[str, float] = {}
    if hy_oas_pct is not None:
        css_values["credit.hy_oas_pct"] = hy_oas_pct
    if hy_oas_20d is not None:
        css_values["credit.hy_oas_20d_change"] = hy_oas_20d
    if bbb is not None and bbb[1] is not None:
        css_values["credit.bbb_oas_pct"] = bbb[1]

    as_of = spx_latest[0] if spx_latest else date.today().isoformat()
    return mbs_values, css_values, as_of


def collect_breadth(client: YahooClient, tickers: list[str]) -> tuple[dict[str, float], dict, list]:
    """Compute the two breadth metrics from per-ticker daily closes. Returns
    ``(metrics, meta, breadth_series)``; metrics are omitted (not zero) when
    breadth cannot be computed, and ``meta`` carries coverage so the job can
    surface a drop. ``breadth_series`` is the compact ``[[date, pct], ...]``
    series stored as a raw artifact for provenance."""
    closes, errors = client.collect_closes(tickers)
    bs = breadth_mod.breadth_series(closes)
    cov = breadth_mod.coverage(closes)

    metrics: dict[str, float] = {}
    if bs:
        metrics["market.spx_pct_above_200dma"] = bs[-1][1]
    if len(bs) > 20:
        metrics["market.breadth_20d_change"] = round(bs[-1][1] - bs[-1 - 20][1], 4)

    meta = {
        "tickers_requested": len(tickers),
        "tickers_ok": len(closes),
        "tickers_failed": len(errors),
        "coverage": cov,
    }
    return metrics, meta, bs


def collect_afrs(client: EdgarClient) -> tuple[dict[str, dict[str, float]], dict]:
    """Extract the auto-extractable fundamentals for the six companies and
    attach extraction confidence + flags (the hardened "AI verification").
    Returns ``(per_company_values, details)`` keyed by entity_id."""
    per_company: dict[str, dict[str, float]] = {}
    details: dict[str, dict] = {}
    for company in COMPANY_ENTITIES:
        entity_id = company["entity_id"]
        try:
            facts = client.company_facts(company["cik"])
        except Exception as exc:  # noqa: BLE001 — fail closed per company
            details[entity_id] = {"error": str(exc)}
            continue
        fund = afrs_mod.extract_fundamentals(facts)
        values = afrs_mod.compute_values(fund)
        confidence, flags = afrs_mod.validate(fund, values)
        per_company[entity_id] = values
        details[entity_id] = {
            "confidence": confidence,
            "flags": flags,
            "method": fund.get("method", {}),
        }
    return per_company, details


def _serialise_outcome(outcome: ScoreOutcome) -> list[dict]:
    return [
        {
            "metric_id": r.metric_id,
            "available": r.available,
            "band": r.band,
            "score": r.score,
            "weight": r.weight,
            "value": r.value,
            "reason": r.reason,
        }
        for r in outcome.results
    ]


def recompute_state(
    engine,
    policy: dict,
    up_to: Optional[date] = None,
) -> tuple[str, list[str], int]:
    """Replay stored score-snapshot indications through a fresh StateTracker and
    return (state, reasons, consecutive_days_at_state). When ``up_to`` is given,
    only snapshots with ``as_of_date <= up_to`` are replayed (the ``replay
    --as-of`` path)."""
    tracker = StateTracker(policy, state="NORMAL")
    states_seen: list[str] = []
    last_reasons: list[str] = []
    with Session(engine) as session:
        query = select(ScoreSnapshot).order_by(ScoreSnapshot.as_of_date, ScoreSnapshot.id)
        if up_to is not None:
            query = query.where(ScoreSnapshot.as_of_date <= up_to)
        snaps = session.scalars(query).all()
        for snap in snaps:
            comp = json.loads(snap.component_json)
            ind = comp.get("indication")
            if ind is None:
                continue
            state = ind.get("state")  # may be None (data-quality)
            if state is None:
                tracker.feed(None)
            else:
                tracker.feed(Indication(state=state, reasons=ind.get("reasons", [])))
                last_reasons = ind.get("reasons", [])
            states_seen.append(tracker.state)

    final = states_seen[-1] if states_seen else "NORMAL"
    conf = 0
    for st in reversed(states_seen):
        if st == final:
            conf += 1
        else:
            break
    return final, last_reasons, conf


def run(db_path: Optional[str] = None, policy_path: Optional[str] = None) -> dict:
    load_dotenv_local()
    policy = load_policy(policy_path)
    engine = initialize(db_path, policy)

    with FredClient() as fred:
        raw = collect_fred(fred)
        mbs_values, css_values, as_of = derive_market_values(raw)

        # Breadth is self-computed (ADR-0001); a failed pull leaves the two
        # breadth metrics unavailable and renormalises MBS, never zero/green.
        breadth_client = YahooClient()
        breadth_metrics, breadth_meta, breadth_series = collect_breadth(breadth_client, load_tickers())
        mbs_values.update(breadth_metrics)

        mbs = compute_score("mbs", policy, mbs_values)
        css = compute_score("css", policy, css_values)

        # AFRS: auto-extracted fundamentals + hardened validation. A company
        # with no extractable fundamentals contributes no score; sector AFRS is
        # the median / worst-two aggregation over whatever names we could score.
        per_company, afrs_details = collect_afrs(EdgarClient())
        company_scores: dict[str, float] = {}
        for entity_id, values in per_company.items():
            outcome = company_afrs(policy, values)
            if outcome.score is not None:
                company_scores[entity_id] = outcome.score
        afrs: Optional[float] = sector_afrs(policy, company_scores)

        red_combo_day = {
            "market.spx_vs_200dma_pct": mbs_values.get("market.spx_vs_200dma_pct"),
            "market.spx_pct_above_200dma": mbs_values.get("market.spx_pct_above_200dma"),
            "credit.hy_oas_pct": css_values.get("credit.hy_oas_pct"),
        }
        red = red_combo_today(red_combo_day, policy)
        ind = indicated_state(
            Scores(mbs=mbs.score, css=css.score, afrs=afrs),
            red_combo=red,
            early_warning_count=mbs.early_warning_count + css.early_warning_count,
        )

        component = {
            "mbs": _serialise_outcome(mbs),
            "css": _serialise_outcome(css),
            "afrs": {
                "score": afrs,
                "company_scores": company_scores,
                "details": afrs_details,
            },
            "indication": {"state": ind.state, "reasons": ind.reasons, "red_combo": red},
            "raw_values": {"mbs": mbs_values, "css": css_values},
            "breadth": breadth_meta,
        }

        # Persist raw artifacts + observations + score snapshot.
        with Session(engine) as session:
            for series_id, obs in _raw_artifact_rows(raw):
                body = json.dumps([{"date": d, "value": v} for d, v in obs])
                session.add(RawArtifact(
                    raw_record_id=uuid.uuid4().hex,
                    source_id="fred",
                    uri=f"{fred.base_url}/series/observations?series_id={series_id}",
                    retrieved_at=_utcnow(),
                    content_sha256=_sha256(body),
                    content=body,
                ))
            if breadth_series:
                bbody = json.dumps({"as_of": as_of, "breadth": breadth_series})
                session.add(RawArtifact(
                    raw_record_id=uuid.uuid4().hex,
                    source_id="yahoo",
                    uri=f"{breadth_client.base_url}/v8/finance/chart/{{ticker}}",
                    retrieved_at=_utcnow(),
                    content_sha256=_sha256(bbody),
                    content=bbody,
                ))
            for mid, v in mbs_values.items():
                _store_observation(session, mid, _entity_for(mid), v, as_of)
            for mid, v in css_values.items():
                _store_observation(session, mid, _entity_for(mid), v, as_of)
            # Company fundamentals: one observation per auto-extracted indicator.
            # The giant companyfacts JSON is NOT stored as a raw artifact (it can
            # be several MB per CIK); instead the score snapshot's component_json
            # carries the per-company scores + extraction method/flags for replay.
            for entity_id, values in per_company.items():
                for mid, v in values.items():
                    _store_company_observation(session, mid, entity_id, v, as_of)
            session.add(ScoreSnapshot(
                as_of_date=date.fromisoformat(as_of),
                mbs=mbs.score,
                css=css.score,
                afrs=afrs,
                component_json=json.dumps(component),
                policy_version=policy["policy_version"],
                quality_status="ok" if (mbs.score is not None and css.score is not None) else "data_quality_warning",
                created_at=_utcnow(),
            ))
            session.commit()

        state, reasons, conf = recompute_state(engine, policy)
        with Session(engine) as session:
            session.add(StateSnapshot(
                as_of_date=date.fromisoformat(as_of),
                state=state,
                transition_reason="; ".join(reasons) if reasons else ind.reasons and "; ".join(ind.reasons) or "",
                confirmations=conf,
                policy_version=policy["policy_version"],
                created_at=_utcnow(),
            ))
            session.commit()

    return {
        "as_of": as_of,
        "mbs": mbs.score,
        "css": css.score,
        "afrs": afrs,
        "state": state,
        "indication": ind.state,
        "reasons": reasons or ind.reasons,
        "mbs_unavailable": mbs.unavailable,
        "css_unavailable": css.unavailable,
        "breadth": breadth_meta,
    }


_ENTITY_MAP = {
    "market.spx_vs_200dma_pct": "SPX",
    "market.spx_pct_above_200dma": "BREADTH",
    "market.breadth_20d_change": "BREADTH",
    "market.vix": "VIX",
    "credit.hy_oas_pct": "HY_OAS",
    "credit.hy_oas_20d_change": "HY_OAS",
    "credit.bbb_oas_pct": "BBB_OAS",
}


def _entity_for(metric_id: str) -> str:
    return _ENTITY_MAP.get(metric_id, "SPX")


def _store_observation(session: Session, metric_id: str, entity_id: str, value: Optional[float], as_of: str) -> None:
    session.add(Observation(
        metric_id=metric_id,
        entity_id=entity_id,
        value=value,
        unit="percent" if "pct" in metric_id or "change" in metric_id else "index",
        period_type="daily_close",
        as_of_date=date.fromisoformat(as_of),
        retrieved_at=_utcnow(),
        source_id="fred",
        extraction_method="derived",
        confidence="derived",
        status="active",
        definition_version="2026-08-21.1",
    ))


def _store_company_observation(session: Session, metric_id: str, entity_id: str, value: Optional[float], as_of: str) -> None:
    """An annual, XBRL-extracted company fundamental. ``confidence`` is ``reported``
    (a directly reported 10-K fact), unlike the market metrics which are ``derived``."""
    session.add(Observation(
        metric_id=metric_id,
        entity_id=entity_id,
        value=value,
        unit="percent" if "pct" in metric_id else "percentage_points",
        period_type="annual",
        as_of_date=date.fromisoformat(as_of),
        retrieved_at=_utcnow(),
        source_id="sec_edgar",
        extraction_method="xbrl",
        confidence="reported",
        status="active",
        definition_version="2026-08-21.1",
    ))


def _raw_artifact_rows(raw: dict[str, object]) -> list[tuple[str, list]]:
    """Map the collected raw data back to (series_id, observations) for append-only
    raw-artifact storage."""
    return [
        (FRED_SERIES["market.spx_close"], raw["spx_history"]),
        (FRED_SERIES["credit.hy_oas_pct"], raw["hy_oas_history"]),
        (FRED_SERIES["credit.bbb_oas_pct"], [raw["bbb_oas_latest"]] if raw["bbb_oas_latest"] else []),
        (FRED_SERIES["treasury.10y_yield"], [raw["dgs10_latest"]] if raw["dgs10_latest"] else []),
        (FRED_SERIES["treasury.30y_yield"], [raw["dgs30_latest"]] if raw["dgs30_latest"] else []),
        (FRED_SERIES["market.vix"], [raw["vix_latest"]] if raw["vix_latest"] else []),
    ]


def main() -> None:
    result = run()
    print("=== US/AI systemic risk monitor — daily snapshot ===")
    print(f"as_of          : {result['as_of']}")
    print(f"MBS            : {result['mbs']}")
    print(f"CSS            : {result['css']}")
    print(f"AFRS           : {result['afrs']}")
    print(f"indication     : {result['indication']}")
    print(f"confirmed state: {result['state']}")
    print(f"reasons        : {'; '.join(result['reasons']) if result['reasons'] else '-'}")
    print(f"mbs unavailable: {result['mbs_unavailable']}")
    print(f"css unavailable: {result['css_unavailable']}")


if __name__ == "__main__":
    main()
