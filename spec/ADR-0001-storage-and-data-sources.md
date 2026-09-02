# ADR-0001 — Storage engine and data-source decisions

- Status: Accepted (2026-08-21)
- Supersedes: PRD §8 literal "PostgreSQL + object storage" suggestion
- Scope: `src/risk_monitor/` — the US/AI systemic-risk monitor

## Context

The PRD §8 suggests "PostgreSQL + object storage" but §2 declares the system is
**not** a multi-tenant SaaS, and §11 requires the MVP to run locally with no
paid data source. Personal Agent already runs SQLAlchemy 2.0 + alembic on
SQLite with well-understood transaction semantics (§5.2 of `CLAUDE.md`). The
data-source question ("free, no license needed") was resolved with Henson on
2026-08-21.

## Decision

1. **SQLite (via SQLAlchemy + alembic), not PostgreSQL.** Single user, append-only
   time series + provenance, zero additional infra. Object storage is deferred;
   raw SEC filings are referenced by URL + `content_sha256` rather than blob-stored,
   with the raw response body kept only for the artifacts we must replay locally.
   The schema keeps the same table family the PRD names, so a later lift to
   PostgreSQL is a migration, not a redesign.
2. **FRED as the market/credit source, under its personal non-commercial terms.**
   HY OAS (`BAMLH0A0HYM2`), BBB OAS (`BAMLC0A4CBBB`), 10Y/2Y/3M/30Y treasury
   (`DGS10`/`DGS2`/`DGS3MO`/`DGS30`), 10Y inflation-indexed yield (`DFII10`),
   SPX (`SP500`), VIX (`VIXCLS`). A free API key is required;
   the key is stored only in `.env.local` (mode 600, gitignored). Henson is
   single-user and does not redistribute, which stays inside FRED's personal-use
   terms; the ICE BofA OAS series is third-party-licensed to FRED and its history
   window is shrinking to ~3 years from 2026-04, so we archive locally from day one.
3. **`market.fwd_eps_revisions` = `unavailable` in MVP.** Excluded from MBS and its
   weight renormalised, per PRD §15.5. No fabricated proxy.
4. **Single-name AI credit basket = `proxy=true`.** No free daily CDS and TRACE
   redistribution is restricted; the per-name credit signal is built from rating
   changes (filings / rating-agency announcements) + the six names' bond/equity
   credit signals, explicitly marked proxy in the UI. PRD §7 admits this fallback.
5. **Breadth is self-computed** from a fixed S&P 500 constituent snapshot
   (the public-domain `datasets/s-and-p-500-companies` GitHub mirror, checked in
   as `src/risk_monitor/assets/spx_constituents.json` with a `snapshot_date`) +
   per-name daily closes from the **Tencent Finance kline API**
   (`web.ifzq.gtimg.cn/appstock/app/fqkline/get`). Tencent is free, key-less and
   China-reachable — Yahoo's chart API geo-blocks the production ECS (Aliyun
   mainland IP) with a hard 403, so Tencent replaces Yahoo as the per-name close
   source. The `YahooClient` adapter is retained (not deleted) so the swap can be
   rolled back to Yahoo from the home machine if Tencent throttles or changes
   shape. Tencent returns *unadjusted* (raw) closes; the raw-vs-adjusted
   measurement (2026-08-22, `scripts/compare_raw_vs_adj.py`) found a systematic
   −3.19pp breadth understatement, so the breadth band thresholds are recalibrated
   (green ≥57 / yellow 47–57 / orange 37–47 / red <37) to keep scoring
   semantically consistent with the pre-swap adjusted-close convention. Tencent
   also supplies the six ai_basket names' closes under the same raw-close
   convention; their per-name raw-vs-adjusted `pct_vs_200dma` delta is ≤0.5pp
   (NVDA −0.09 / ORCL −0.48 / MSFT −0.49 / META −0.11 / AMZN 0.00 / GOOGL −0.08),
   far inside the 0/−10/−25 band widths, so the `per_name_bands` thresholds are
   deliberately *not* recalibrated — no name's band label changes. The index-level
   series stay on FRED. The two breadth metrics (`market.spx_pct_above_200dma`,
   `market.breadth_20d_change`) fail closed on *both* coverage shapes: a pull
   covering fewer than 80% of requested tickers, **or** a pull whose tickers
   mostly lack a 200dma signal (e.g. the source returns 200 with a blank `day`
   array for suspended/invalid codes), leaves them `unavailable` and renormalises
   MBS over the remaining indicators — the run records both fractions rather than
   silently computing breadth from a subset.

## Consequences

- No infra dependency beyond what the repo already uses; the daily job runs
  against SQLite on the same machine/ECS as Personal Agent.
- CSS's single-name layer is deliberately weaker than the OAS aggregate; its
  `proxy` status is surfaced, never silently treated as reported.
- A FRED key outage or API-limit hit must fail closed into `DATA_QUALITY_WARNING`,
  never into a zero/green score.
- The daily card now has a separate Rates & Credit Score (RCS) module with a
  20% target weight. It uses Treasury level/real-rate/curve and HY OAS change;
  MOVE, Treasury liquidity, Fed Funds futures and valuation remain explicit
  unavailable evidence until a verified daily source is connected. A 10Y yield
  alone only slows new buying; active equity-exposure review requires rising
  real yields, widening HY OAS and market/credit confirmation.
