# Personal Agent — Agent Collaboration Guide

> Last updated: 2026-08-31
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
- **The PRD → technical-design gate is per round, and no artifact above it
  counts.** For any new domain, feature, or cross-cutting capability, first
  submit a PRD draft and wait for Henson's explicit approval; then submit the
  technical design and wait for Henson's explicit approval; only after both
  approvals may implementation begin. What the gate covers is **this round's
  scope and every deviation it carries** — not the capability area. A frozen
  higher-level PRD or technical design that merely covers the same capability
  (CAP-003, CAP-006, …) does NOT satisfy it: those documents were reviewed
  against a different scope, so they cannot have authorised deviations nobody
  has seen. Write the round's gate record under `docs/gates/` before the first
  edit; CI refuses a PR that changes `src/**` or `ios/**` on a branch with no
  filled gate record. A development branch must not implement first and
  backfill the PRD or technical design afterward. Emergency fixes and purely
  documentary or test-only changes are exempt, but the exception and reason
  must be recorded in the gate record.
- **An in-conversation instruction never silently outranks the gate.** §6 ranks
  the newest user instruction first, so "start implementing" can be misread as
  overriding this section. It does not do so silently: when a request conflicts
  with the gate, say so **before the first edit**, name the missing artifact,
  and get an explicit decision. If Henson then chooses to proceed, that is a
  legitimate override — record it in the gate record as an exception with its
  reason, in the same change. Executing first and reporting the conflict
  afterwards is the exact failure this rule exists to prevent.
- For requested implementation or documentation changes, after the work and
  required verification are complete, local staging and `git commit` are
  pre-authorized; do not wait for a second commit authorization. An explicit
  read-only, no-commit, or review-only instruction in the current request
  overrides this default. `git push` and merge operations always require
  Henson's explicit authorization, and this rule does not authorize deploys,
  PR creation, provider calls, or other external writes.

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

### 5.3 Context economy — minimise aggregate token use

The optimisation target is the **sum of raw input tokens across the main agent
and every subagent**, including cache-hit input. A smaller main context is not a
win if several inherited subagents replay it. Concurrency optimises latency, not
token use. Roughly:

`aggregate input ≈ Σ(each agent's model turns × that agent's current prefix)`.

Reduce agent count, inherited history, model turns and repeated tool output in
that order.

- **Start scoped in the main agent.** Begin with `git status`, `git diff --stat`
  / `--name-only`, `rg`, and exact line windows. File length alone never
  authorises a subagent. Reading a few relevant hunks in the main agent is
  cheaper when the question can be resolved in a small number of turns. Do not
  print an entire large file or diff into the main context; narrow it first.
- **Default to zero subagents; use at most one Explore subagent by default.**
  Delegate only when a large, bounded read can be completed in one shot and
  keeping that raw material out of several later main-agent turns is expected to
  reduce the aggregate. Before spawning, state what material is being isolated
  and why one bounded agent is cheaper than reading it locally. Parallelism by
  itself is not a reason. Multiple overlapping review agents require an explicit
  user request or clearly independent, high-risk domains that one pass cannot
  cover.
- **Do not fork the conversation by default.** On a surface that exposes
  `fork_turns`, use `"none"`; otherwise do not copy the transcript. Give the
  subagent a compact handoff: working directory, commit/range, exact files,
  applicable scoped-rule paths, questions, and a strict output format. Use the
  full dialogue only when the task genuinely depends on it; state that reason
  before spawning.
- **Make delegated reads one-shot and turn-bounded.** Size the handoff for at
  most 10 model/tool iterations. A read-only subagent returns one final report
  with findings, file/line evidence and no copied file bodies; if the budget is
  insufficient, it returns the best partial evidence instead of continuing. It
  sends no routine progress messages and is not continued for implementation.
  The main agent fixes findings. If a fresh independent review is required,
  start it from a compact handoff rather than extending the long-lived agent.
- **Bound orchestration turns.** Do not repeatedly poll agents. Use at most two
  waits per agent, each up to 60 seconds, and rely on the final notification.
  Avoid status calls that return unchanged state.
- **Serialise shared-worktree phases.** Do not run a full suite while another
  agent is editing. Give each file one owner, finish edits, then run targeted
  tests and one full suite from the stable tree. Do not rerun an unchanged suite
  merely because another agent reported it.
- **Do not reread unchanged evidence.** Keep one compact finding record
  (`file:line`, trigger, actual, expected) and revisit only the changed hunk.
  Prefer counts and paths over file bodies in agent reports and tool output.
- **Compact before a long next phase.** Check `/usage` at phase boundaries. If
  the current input is already large (about 80K tokens) and more than five model
  turns remain, run `/compact <focus>` or start a fresh narrowly-scoped task
  before review becomes implementation. Auto-compaction is a last resort.
- **Shrink the tool registry when the surface supports it.** Use a read-only or
  coding profile with only the needed tools; do not spawn an extra agent merely
  to obtain a smaller registry.
- **Prefer scoped rules over monolithic AGENTS.md.** Area-specific context
  (Finance, iOS, phase gates) belongs in `.qoder/rules/*.md` with `paths`
  frontmatter. Keep this file limited to cross-cutting rules that justify their
  cost on every turn.

## 6. Source-of-truth precedence

When documents conflict, use this order:

1. The newest explicit user instruction in the current conversation — but see
   §5: it never *silently* overrides the PRD/technical-design gate, and a
   conflict must be surfaced before the first edit rather than reported after it.
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
- When evaluating a non-deterministic component (model, provider, any sampled
  path), fill in `docs/Eval结论边界模板_v0.1.md` **before** writing any
  conclusion. A single run never supports a causal claim; separating a stable
  defect from sampling variance requires repeated runs at a fixed version;
  cross-round comparisons must keep numerator and denominator inside the subset
  the rounds share; and a failure that stopped before the tool is not the same
  finding as one that actually called out.
- When updating `PROJECT_STATUS.md`, re-read the sections a change affects and
  fix the ones it contradicts. Two sections disagreeing about who owns a piece
  of work is a defect, not a documentation detail.

## 8. Canonical project documents

- `个人Agent_PRD_v1.0.1.md` — reviewed Phase 1 product scope and requirements.
- `docs/Agent框架Spike初步结果_2026-07-23.md` — evidence for ADK-first choice.
- `docs/MCP工具IR_v0.1.md` — cross-domain IR baseline; archived Finance text is
  non-normative, while its Finance contract summary is current.
- `docs/Finance MCP工具设计草案_v0.1.md` — canonical detailed Finance contract.
- `docs/Eval结论边界模板_v0.1.md` — reusable "可以写 / 不能写" boundary sheet for
  any evaluation of a non-deterministic component: evidence binding, same-denominator
  comparison, safe-versus-strict metrics, stable defect versus sampling variance, and
  the claims that are forbidden by default. Derived from the 2026-08-07 Finance
  robustness and friction eval evidence in `docs/evidence/`.
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
  track and gates; Phase 1 completion, DAL-G0-P and DAL-G0-E are passed, but
  DAL-007+ implementation still requires Henson's explicit authorization.
- `docs/Phase1技术方案设计计划_v0.1.md` — next-stage work order and the gate
  before implementation.
- `docs/Phase1技术方案_v0.1.md` — current Phase 1 technical design;
  final review passed, but it does not authorize development.
- `docs/Phase1技术方案评审结论_2026-07-23.md` — initial findings, resolved
  P0 contracts, and final design Go decision.
- `docs/Phase1开发拆解_v0.1.md` — executable DEV/CAP tasks, dependencies,
  gates, external inputs and completion definition; development is authorised
  and **G0–G4 have passed** — G4 closed on 2026-08-01 with a real iPhone
  enrolled against the deployed ECS over `https://agent.example.invalid`
  (`docs/evidence/G4_真机验收_2026-08-01.md`). CAP-001, DEV-041, DEV-030,
  DEV-031, DEV-032, DEV-033 and DEV-034 are complete. The DEV-034 review
  remediation (Finance recovery lifecycle, audit-tail witness, alert recovery
  semantics and reproducible timer enablement) is deployed to the ECS and
  verified, migration `0003` included
  (`docs/evidence/DEV034修复部署与DEV035输入就绪_2026-08-01.md`). DEV-035's
  two external inputs (a private versioned OSS bucket behind a single-bucket
  RAM user, and a restic key separate from the data key with an off-machine
  copy) landed and were exercised on 2026-08-01, and its code is built: the
  `online_backup` primitive, both `*-db backup` CLIs, the deletion-manifest
  export/replay, the restore-verify library, `deploy/backup.sh` + three systemd
  timer/service pairs behind a least-privilege `personal-agent-backup` user,
  and `scripts/restore_drill.sh`. The offline suite passed 1678 tests at the DEV-035 close (2026-08-01);
  the count has grown since — see `PROJECT_STATUS.md` §1 for the current
  merged-tree figure.
  **The ECS deploy and the first real OSS backup landed on 2026-08-01**
  (`docs/evidence/DEV035_部署与首次真实备份_2026-08-01.md`): snapshot `3b11537b`
  with all eight inputs, clean `restic check`, `verify.sh` 55 PASS / 0 FAIL,
  three timers enabled. It took six fixes that no offline test could have
  caught — chiefly staging dirs without setgid combined with `UMask=0077` and a
  hard-coded `0600`, which let the backup user list every staged file and open
  none, and a ledger config read straight from a `0700` live data dir. `verify.sh`
  had missed both by asserting `ls` on a directory instead of a read of a file.
  **The Mac off-machine restore drill passed the same day, then passed again
  after review remediation**
  (`docs/evidence/DEV035审查修复部署与异机复验_2026-08-01.md`). Review invalidated
  the first run's two safety claims: normal Finance composition was not a
  read-only probe, and the backup account's two service groups exposed both
  service env/key boundaries. Commit `b719d78` is deployed: the backup account
  now belongs to neither group; purpose-built OS/SQLite read-only entrypoints
  compose no model/adapter/recovery/write path; the restore gate validates both
  databases' integrity/schema/FK/idempotency/receipt graph; recovery is bounded
  to 100 rows with fresh schema evidence per execution; and cleanup can delete
  only a marker-protected script-owned `mktemp` directory. ECS verification is
  66 PASS / 0 FAIL. New real snapshot `fe0ae539` (8 inputs) passed `restic check`
  and a full Mac rerun. **DEV-035 is complete and the G5 precondition gate is
  passed.** The data-key off-machine custody remains tracked in
  `docs/密钥清单_v0.1.md`. The
  ECS clock was checked and is correct
  (`docs/evidence/DEV035_ECS时钟核对_2026-08-01.md`). DEV-036 follows DEV-035.
  **Push closed on 2026-08-04**: the APNs sender and iOS device-token
  registration are built, deployed and proven on the real iPhone — Apple
  accepted the notification and the device wrote `daily_reviews.reviewed_at`
  back 17s later (`docs/evidence/DEV040_真机推送端到端_2026-08-04.md`). That run
  also found `needs_manual_review` to be a terminal state with no human
  resolution path. 打开飞书账本 closed on 2026-08-04:
  `PERSONAL_AGENT_LEDGER_URL` is configured on the ECS and the button appeared
  on the real iPhone.
- `ECS安全加固实施记录_2026-07-23.md` — security, encryption, snapshot, and
  rollback record.
- `docs/iOS开发环境_Personal_Team_v0.1.md` — Personal Team constraints, the iOS
  project layout, and the manual Xcode steps.
- `PROJECT_STATUS.md` — exact handoff point and ordered next work.
