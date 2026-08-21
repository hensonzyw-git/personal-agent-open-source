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
   HY OAS (`BAMLH0A0HYM2`), BBB OAS (`BAMLC0A4CBBB`), 10Y/30Y treasury
   (`DGS10`/`DGS30`), SPX (`SP500`), VIX (`VIXCLS`). A free API key is required;
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
   per-name daily adjusted closes from the **Yahoo Finance chart API**
   (`query1.finance.yahoo.com/v8/finance/chart`). Yahoo is free, key-less, and
   reachable from the home machine (validated 2026-08-21), but *unofficial* —
   it may throttle or change shape. It is used only for breadth; the index-level
   series stay on FRED. The two breadth metrics (`market.spx_pct_above_200dma`,
   `market.breadth_20d_change`) fail closed: a throttled/partial pull leaves them
   `unavailable` and renormalises MBS over the remaining indicators, and the run
   records a coverage fraction rather than silently computing breadth from a
   subset.

## Consequences

- No infra dependency beyond what the repo already uses; the daily job runs
  against SQLite on the same machine/ECS as Personal Agent.
- CSS's single-name layer is deliberately weaker than the OAS aggregate; its
  `proxy` status is surfaced, never silently treated as reported.
- A FRED key outage or API-limit hit must fail closed into `DATA_QUALITY_WARNING`,
  never into a zero/green score.
