# Personal Agent — Agent Collaboration Guide

> Last updated: 2026-07-28
>
> `AGENTS.md` and `CLAUDE.md` must remain byte-for-byte equivalent. Update both
> in the same change.

## 1. Project intent

Personal Agent is a strictly single-user system for Henson: an iPhone app is
the conversational and confirmation UI; a self-hosted backend owns the Agent
runtime, MCP client, policy, audit, scheduled jobs, and high-privilege
credentials. The system turns selected personal data sources into narrowly
scoped, reusable MCP business tools.

It is **not** a general public assistant, a multi-tenant SaaS product, or an
iOS-local Agent loop. The design objective is a safe, testable personal data
and action layer that works across phone, computers, and future Agent hosts.

## 2. Confirmed architecture and boundaries

- Phase 1 Agent framework: **Google ADK**. Claude Agent SDK remains a
  comparison and fallback option, not the active implementation path.
- Preferred model: Zhipu GLM. Keep framework-facing model adapters replaceable;
  business policy, MCP contracts, and connectors must remain model-neutral.
- The backend, not iOS, is the first-class MCP Host/Client. It must support
  stdio and remote Streamable HTTP MCP, discovery, cancellation, timeouts,
  reconnects, tool allowlists, and audit.
- Default public access design: `HTTPS + registered-device identity + short
  lived access tokens`. Tailscale was validated but deliberately is not a
  mobile dependency because it can conflict with the user's existing VPN use.

iOS-specific constraints, device enrollment rules, and lost-device handling
live in `.qoder/rules/ios-device.md` (loaded when touching `ios/` or device
auth code).

## 3. Product scope and sequencing

P0 is the shared Agent/MCP/security foundation plus:

1. The approved Finance surface: expense, income, family fund, query, and
   daily human review.
2. Chat, device permissions, audit, the service-side allowed tool set, and
   the Finance daily review job. Full pending-action confirmation is later work.

Knowledge Base, HealthKit, and Wardrobe/OOTD are later-phase directions. Their
detailed product contracts belong to their own later PRDs, not the current PRD
1.0.1 gate. Wardrobe also remains blocked until the iCloud wardrobe source can be
accessed from the home development machine. Asset data is high-sensitive,
read-only later work; it is not MVP scope.

## 4. Finance MCP decisions and phase gate

The detailed Finance MCP contract, expense write rules, category mapping, and
gate authorization live in `.qoder/rules/finance.md` (loaded when touching
Finance MCP code, dispatcher, or docs).

The current phase gate, CAP-001 baseline, and Development Agent Loop baseline
live in `.qoder/rules/phase-gates.md` (loaded when touching `PROJECT_STATUS.md`,
Phase 1 docs, or agent runtime code).

The authoritative handoff point and ordered next work is always
`PROJECT_STATUS.md`.

## 5. Engineering and safety rules

- Keep business policy outside ADK and outside any individual model SDK:
  `Agent -> tool intent -> policy -> MCP client -> connector -> fact source`.
- Every write needs a stable idempotency key, policy decision, audit envelope,
  and external success evidence. A model saying "done" is never success.
- Read operations must paginate or aggregate server-side; do not call a default
  first page "all data".
- Preserve `raw/` versus `wiki/` boundaries for knowledge work. Automated
  capture writes only to `raw/inbox/`; wiki edits and destructive changes use
  a pending-action / human confirmation flow.
- Do not add generic tool capabilities merely for convenience. Prefer the
  smallest business-named tool and explicit schema.
- Treat personal data and resource identifiers as sensitive. Never commit
  exports, raw HealthKit data, ledger records, private keys, certificates,
  Feishu app credentials, model keys, tokens, server backups, or production
  databases. `.env.local` is local-only and must have mode `600`.
- Use placeholders in examples and tests. Current synthetic evals must remain
  distinguishable from user-reviewed, redacted expressions.
- For any production data access, use the minimum fields and permissions needed
  for the task, and report what was read or written.
- Before changes, inspect the worktree and preserve unrelated user changes.
  Use focused edits; do not refactor adjacent code or documents unless asked.

### 5.1 Non-deterministic and adversarial boundaries

These rules exist because a DEV-027 review found six blocking defects behind a
fully green suite. The cause was structural, not careless: fakes were built from
the same assumptions as the code, so they could only ever confirm those
assumptions, and one small happy-path live check was mistaken for validation.

- A green suite is evidence only where the failure modes are already enumerated.
  At a model, network, or attacker-facing boundary, confidence comes from
  adversarial coverage, not from a passing run.
- For any such boundary, design the failing cases **before** the implementation,
  and make each one a test: empty response, multiple tool calls, a tool call
  mixed with prose, malformed arguments, multi-turn context, a tampered
  environment variable or host, and a provider error. Fail closed everywhere;
  never silently truncate, drop, or repair model output.
- Never let a fake be the only counterparty. A fake written from the same mental
  model as the code proves nothing about the real one. Live checks must include
  the failure shapes, not only the clean path.
- A model or provider must never be trusted to bound its own behaviour. Pin the
  provider host and path; a credential may only travel to that pinned endpoint.
- When the contract has a gap, look it up or ask. Do not invent a plausible
  rule: in a ledger, a plausible-sounding invention silently loses money.
- Spike code is historical evidence, not a production template. Re-derive the
  threat model rather than copying spike wiring into production.
- Framework and architecture choices that are already confirmed (Phase 1 uses
  Google ADK) may not be quietly replaced for implementation convenience.
  Surface the conflict and the trade-off; let Henson decide. Never describe an
  architecture the code does not actually implement.

### 5.2 Structural rules for dispatchers, validators and compare-and-swap

These rules were derived from a CAP-001 slice F review that found six defects
behind a passing plan. They are structural rather than incidental, so they live
next to §5.1 instead of in a commit message.

- **Uniform signatures for co-dispatched functions.** A set of functions called
  by the same dispatcher (validators, handlers, callbacks) must share the
  identical signature. A function that does not need every parameter must still
  accept every parameter. Do not adapt signatures with lambdas at the dispatch
  site: adaptation inevitably leaves one function un-adapted, and the dispatch
  site cannot statically detect the mismatch. The CAP-001 Compactor had two
  validators that took only `payload` while the rest took `(payload, sources)`;
  the lambda bridge failed silently on the first and crashed on the second.
- **Leak checks precede correctness checks.** In a validation pipeline that
  carries a leak risk, secret/credential scanning must run before fact/semantic
  checks. A credential string often contains numbers, dates or identifiers, so
  a fact check that runs first will mis-classify a leaked secret as an invented
  fact, return the wrong failure code, and hide the real exposure. Order is a
  safety property, not a style choice.
- **CAS lineage reference is not the supersede target.** In any
  compare-and-swap build, "which record the new row names as parent" (lineage
  / source chain) and "which old row the CAS supersedes" (replacement target)
  are two independent concepts and must travel as two independent parameters.
  They coincide for an incremental build; they diverge for a full rebuild,
  where the lineage chain is broken (`parent = None`) but the old active row
  still has to be replaced. Binding them into one parameter makes full rebuild
  and incremental build unable to be correct at the same time, and only a
  concurrent or full-rebuild test exposes it.
- **Cross-session state reads bypass the ORM identity map.** SQLAlchemy's
  `Session.get()` and `Session.query().get()` return the identity-map cached
  object, which cannot see status changes another session has already
  committed. Any pre-CAS check, stale-parent check or concurrency guard that
  must read the database's current real state must use a raw `SELECT` (or
  `session.expire_all()` before the read). Concurrent tests are the only
  reliable way to expose this; a single-session test will always see its own
  writes.
- **A transaction on this engine is a real transaction, and that has two
  consequences.** `create_database_engine` disables pysqlite's own transaction
  handling and emits `BEGIN` itself. Without that, pysqlite never opened a
  transaction for `SAVEPOINT`, so a released `begin_nested()` block committed on
  the spot and `Session.rollback()` could not undo it -- a failed request left
  rows behind, and design 6.2's "Boundary Record and event in one transaction"
  was not true. With it: (1) rollback really does discard the whole unit, so
  compensating deletes on error paths are unnecessary and should not be added;
  (2) a session that has **read** cannot write once another session has
  committed -- SQLite refuses the snapshot upgrade immediately, and
  `busy_timeout` does not apply because waiting cannot help. Therefore: never
  hold a transaction across a model, MCP or Feishu call (commit first -- the
  orchestrator commits after context assembly and after every state
  transition), and wrap read-then-write units in
  `personal_agent_core.sqlite.run_write_transaction`, which re-runs them
  against fresh state. Only wrap work with no external side effects; the
  duplicate-decision endpoint and the review-detail read pass `retry=False`
  precisely because they dispatch outside.

### 5.3 Context economy — keeping per-request input small

These rules exist because a long coding session can spend millions of input
tokens on repeated prefixes (system prompt + tool schemas + AGENTS.md + full
conversation history) even when most of that prefix is cache-hit at a discount.
Cache hits are not free; they are discounted, so a large prefix still costs.

- **Read large files through a subagent.** Files over ~200 lines should be read
  via the `Explore` agent (or a custom subagent with `tools: [Read, Grep, Glob]`),
  not directly with `Read` in the main context. The file contents stay in the
  subagent's isolated context and only the summary returns to the main session.
  A 1000-line file read directly adds ~10K tokens to every subsequent request in
  that session; read through a subagent it adds near-zero.
- **Shrink the active tool set per session.** Launch read-heavy sessions with a
  limited tool set (e.g. `--tools Read Grep Glob Agent`) instead of the full
  30+ tool registry. Each tool's JSON schema travels on every request; 30 tools
  cost ~15-20K tokens/round. Define coding and research profiles and switch.
- **Compact proactively.** Run `/compact <focus>` before auto-compaction fires
  so you control what is retained. Monitor with `/usage`. A lower
  `model.contextWindow` (400000 instead of 1000000) makes auto-compaction
  trigger sooner and keeps the per-request prefix smaller.
- **Prefer scoped rules over monolithic AGENTS.md.** Area-specific context
  (Finance, iOS, phase gates) belongs in `.qoder/rules/*.md` with `paths`
  frontmatter, loaded only when matching files are touched. AGENTS.md keeps
  only cross-cutting rules that every turn needs.

## 6. Source-of-truth precedence

When documents conflict, use this order:

1. The newest explicit user instruction in the current conversation.
2. A newer domain design draft that records that decision.
3. Current live schema / tool output / code behavior.
4. `PROJECT_STATUS.md` for handoff context.
5. PRD, IR, and historical spike reports.

Update obsolete documents once a decision is frozen; until then, flag the
conflict instead of silently choosing an old default.

## 7. Required verification before claiming completion

- Run targeted tests and `git diff --check` after code or document changes.
- For external integrations, verify the actual response/evidence ID; do not
  infer success from configuration alone.
- Keep offline fixture validation separate from real model, Feishu, or remote
  MCP evidence.
- Do not print secrets in commands, logs, test snapshots, or replies.
- If a change affects ECS networking, SSH, backups, or encryption, preserve a
  rollback path and verify that the personal site remains reachable.
- A task touching a non-deterministic or adversarial boundary is complete only
  when all four hold: the confirmed framework is actually used, the failure
  modes of §5.1 are covered by tests, the production composition really exists
  (a seam wired only in tests is not wiring), and the documentation states
  exactly that — no more.
- State completion at the level actually proven. Distinguish "offline tests
  pass", "one happy-path live call succeeded", and "the failure modes were
  exercised live". Passing tests written against one's own assumptions is the
  weakest of the three, and must not be reported as the strongest.
- When updating `PROJECT_STATUS.md`, re-read the sections a change affects and
  fix the ones it contradicts. Two sections disagreeing about who owns a piece
  of work is a defect, not a documentation detail.

## 8. Canonical project documents

- `个人Agent_PRD_v1.0.1.md` — reviewed Phase 1 product scope and requirements.
- `docs/Agent框架Spike初步结果_2026-07-23.md` — evidence for ADK-first choice.
- `docs/MCP工具IR_v0.1.md` — cross-domain IR baseline; archived Finance text is
  non-normative, while its Finance contract summary is current.
- `docs/Finance MCP工具设计草案_v0.1.md` — canonical detailed Finance contract.
- `docs/Agent横向能力PRD_v1.0.md` — frozen cross-cutting product contract for
  Timeline/Session, context compaction, memory, routing, multimodal input and
  streaming.
- `docs/Agent横向能力技术方案_v1.0.md` — frozen framework-neutral architecture,
  versioned data/API contracts, failure boundaries and CAP acceptance gates for
  those capabilities.
- `docs/开发Agent闭环PRD_v1.0.md` — confirmed product and Human-in-the-loop
  baseline for feature intake through reviewed PR, with manual high-risk gates.
- `docs/开发Agent闭环技术方案_v1.0.md` — graph-orchestrated deterministic
  workflow, GitHub and Home Mac Worker architecture, provider routing, security
  and recovery design.
- `docs/开发Agent闭环开发拆解_v0.1.md` — separate DAL-001–050 implementation
  track and gates; Phase 1 completion blocks DAL-001, DAL-G0 is not authorised,
  and no task has started.
- `docs/Phase1技术方案设计计划_v0.1.md` — next-stage work order and the gate
  before implementation.
- `docs/Phase1技术方案_v0.1.md` — current Phase 1 technical design;
  final review passed, but it does not authorize development.
- `docs/Phase1技术方案评审结论_2026-07-23.md` — initial findings, resolved
  P0 contracts, and final design Go decision.
- `docs/Phase1开发拆解_v0.1.md` — executable DEV/CAP tasks, dependencies,
  gates, external inputs and completion definition; development is authorised,
  G0–G3 passed, and CAP-001 is next before DEV-030/G4.
- `ECS安全加固实施记录_2026-07-23.md` — security, encryption, snapshot, and
  rollback record.
- `docs/iOS开发环境_Personal_Team_v0.1.md` — Personal Team constraints, the iOS
  project layout, and the manual Xcode steps.
- `PROJECT_STATUS.md` — exact handoff point and ordered next work.
