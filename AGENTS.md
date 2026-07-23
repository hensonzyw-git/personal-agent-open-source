# Personal Agent — Agent Collaboration Guide

> Last updated: 2026-07-23
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
- iOS is a thin client. It must never contain long-lived Feishu, GitHub, model,
  or infrastructure credentials.
- Default public access design: `HTTPS + registered-device identity + short
  lived access tokens`. Tailscale was validated but deliberately is not a
  mobile dependency because it can conflict with the user's existing VPN use.
- The first accounting fact source remains Feishu Bitable. Do not migrate it to
  a self-hosted database during the MVP.
- Finance MCP uses one self-built Feishu app and fixed, minimal OpenAPI access
  to the active annual ledger. Do not expose generic Feishu tools, arbitrary
  HTTP, SQL, shell, or filesystem access to the model.
- Each annual ledger is a protected server-side configuration change (Base,
  table, field IDs, allowed categories), not a new application or a prompt
  change. Validate schema before enabling writes.

## 3. Product scope and sequencing

P0 is the shared Agent/MCP/security foundation plus:

1. Natural-language expense logging, transaction lookup, period analysis, and
   daily human review.
2. Knowledge-base retrieval and safe capture to `raw/inbox/`.
3. Chat, device permissions, audit, confirmation cards, and scheduled jobs.

HealthKit incremental ingestion is P1. Wardrobe/OOTD is P1 but blocked until
the iCloud wardrobe source can be accessed from the home development machine.
Asset data is high-sensitive, read-only later work; it is not MVP scope.

## 4. Finance MCP decisions that override older drafts

The active working specification is `docs/Finance MCP工具设计草案_v0.1.md`.
It overrides older conflicting finance language in the PRD and `MCP工具IR_v0.1`.
The Finance document is still a discussion draft: do not present unresolved
items as frozen production rules.

Already confirmed:

- Tool is provisionally `finance.log_expense`; it receives structured semantic
  fields, not raw user text. The Host injects request, identity, device,
  timezone, trace, and idempotency data.
- The write destination is the Feishu annual `支出记录` table. Only the stored
  amount, name, date, family flag, and category fields may be written; formula
  fields and auto-number fields may not be written.
- `Asia/Shanghai` determines dates. Missing date means message day; `昨天` and
  `前天` resolve deterministically before the MCP call.
- Every new entry must explicitly state `个人支出` or `家庭支出`. There is no
  default and history must not be used to infer this attribute. Missing scope
  means ask before writing.
- Normal expenses are positive; refunds and AA receipts are negative. Users do
  not need to type `-`; normalize from explicit semantics. Refund/AA category
  may inherit only from a unique, high-confidence matching original record;
  family scope never inherits. Ambiguous matches must ask.
- Preserve the user-provided name. Do not silently normalize, rename, correct,
  or restore text the user later removed in Feishu. Internal normalized names
  may be used only for matching/audit.
- Travel records are category `旅行` and retain the trip tag in the displayed
  name, for example `机票 #东京02`. An explicit tag is used as written. When a
  clear destination lacks a tag, query the active ledger's distinct trip tags:
  use the sole matching root, create the basic root if none exists, and ask if
  multiple same-destination trip instances exist. Never guess an abbreviation
  or numbered trip.
- Foreign currency must be converted to CNY using a historical rate for the
  occurrence date. Do not guess a rate. The original currency expression is
  temporarily appended to the displayed name and may later be manually removed.
- The agent writes directly for this R2 action and returns the external Feishu
  `record_id`; daily review detects duplicates and family/personal mistakes.
- Category mapping is evidence-based and conservative. Travel tags override
  item keywords; activity contexts (Disney/F1/concert etc.) can override food
  keywords to `玩乐`. Confirmed rules include `搓澡 -> 玩乐`, `网球场地` and
  `网球拍穿线 -> 日常生活`, and non-activity-context `饮料 -> 餐饮`.
  `买充电宝 -> 购物`; borrowed/rented `充电宝 -> 日常生活`. Ask only when a
  new input does not reveal whether the power bank was bought or borrowed.

The finance mapping derived from the current ledger is a candidate rule set,
not a permission to invent new mappings. Before implementing it, record user
approval and add representative redacted evaluations.

## 5. Engineering and safety rules

- Keep business policy outside ADK and outside any individual model SDK:
  `Agent -> tool intent -> policy -> MCP client -> connector -> fact source`.
- Every write needs a stable idempotency key, policy decision, audit envelope,
  and external success evidence. A model saying “done” is never success.
- Read operations must paginate or aggregate server-side; do not call a default
  first page “all data”.
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

## 8. Canonical project documents

- `个人Agent_PRD_v0.4.md` — product scope and requirements.
- `docs/Agent框架Spike初步结果_2026-07-23.md` — evidence for ADK-first choice.
- `docs/MCP工具IR_v0.1.md` — cross-domain IR baseline; finance sections have
  known older assumptions and must defer to the Finance draft.
- `docs/Finance MCP工具设计草案_v0.1.md` — active Finance design discussion.
- `ECS安全加固实施记录_2026-07-23.md` — security, encryption, snapshot, and
  rollback record.
- `PROJECT_STATUS.md` — exact handoff point and ordered next work.
