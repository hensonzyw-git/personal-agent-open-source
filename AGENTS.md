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
- Finance has separate annual expense and income tables. `finance.log_income`
  writes only income amount, name, date, and the `工资` / `其他` category. The
  user does not provide the income category: explicit salary wording maps to
  category/name `工资`; all other clear income maps to `其他` and retains its
  extracted subject (for example `公积金入账 -> 公积金`). It must not infer a
  family attribute or reuse expense refund/AA semantics.
- `finance.update_family_fund` is a separate R2 ledger-write tool. It may write
  a positive recharge amount directly, or reconcile a user-provided target
  balance by writing half the positive difference because the verified table
  formula doubles recharge. It never transfers bank money, writes formula or
  initial-balance fields, writes a negative top-up, or re-applies a failed
  reconciliation automatically.
- Finance MCP uses one self-built Feishu app and fixed, minimal OpenAPI access
  to the active annual ledger. Do not expose generic Feishu tools, arbitrary
  HTTP, SQL, shell, or filesystem access to the model.
- Each annual ledger is a protected server-side configuration change (Base,
  table, field IDs, allowed categories), not a new application or a prompt
  change. Validate schema before enabling writes.

## 3. Product scope and sequencing

P0 is the shared Agent/MCP/security foundation plus:

1. The approved Finance surface: expense, income, family fund, query, and
   daily human review.
2. Chat, device permissions, audit, the service-side allowed tool set, and the
   Finance daily review job. Full pending-action confirmation is later work.

Knowledge Base, HealthKit, and Wardrobe/OOTD are later-phase directions. Their
detailed product contracts belong to their own later PRDs, not the current PRD
1.0.1 gate. Wardrobe also remains blocked until the iCloud wardrobe source can be
accessed from the home development machine. Asset data is high-sensitive,
read-only later work; it is not MVP scope.

## 4. Finance MCP decisions that override older drafts

`docs/Finance MCP工具设计草案_v0.1.md` is the canonical detailed Finance
contract. Its approved product semantics have been reconciled into the PRD and
`MCP工具IR_v0.1`; only its explicitly implementation-level parameters remain
for Phase 1 technical design. Do not reintroduce superseded Finance rules.

## 4.1 Current phase gate

PRD v1.0.1 has passed product review. Do not proactively build its MCP server, Feishu
application, iOS screens, or write against personal records merely because the
requirements exist; wait for explicit user approval before entering Phase 1
technical design or development. Once technical-design authorization is given,
start with `docs/Phase1技术方案设计计划_v0.1.md`; it defines the sequence and
the gate between design and implementation.

Already confirmed:

- Tool is provisionally `finance.log_expense`; it receives structured semantic
  fields, not raw user text. The Host injects request, identity, device,
  timezone, trace, and idempotency data.
- The write destination is the Feishu annual `支出记录` table. Only the stored
  amount, name, date, family flag, and category fields may be written; formula
  fields and auto-number fields may not be written.
- `occurred_on` is the actual payment/entry date, not the future consumption
  date. Missing date means message day; paying today for a future trip is still
  recorded today. `昨天` and `前天` resolve before the MCP call.
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
- Foreign currency is converted to CNY using the current Frankfurter `ECB`
  reference rate at entry time, not a historical occurrence-date rate. Do not
  guess a rate. The original currency expression is temporarily appended to the
  displayed name; Henson may later replace the estimated CNY amount in Feishu
  with the actual settlement amount, and the Agent must not overwrite it.
- The agent writes directly for this R2 action and returns the external Feishu
  `record_id`; daily review only presents that day's writes for human field
  inspection and performs neither duplicate detection nor data mutation.
- `finance.log_expense` is the frozen single-entry business tool. When one
  message contains two or more fully resolved entries, use
  `finance.log_expense_batch`: it is all-or-none, returns every `record_id`,
  and must refuse before writing if source-side batch atomicity cannot be
  proven.
- Category mapping is evidence-based and conservative. Travel tags override
  item keywords; activity contexts (Disney/F1/concert etc.) can override food
  keywords to `玩乐`. Confirmed rules include `搓澡 -> 玩乐`, `网球场地` and
  `网球拍穿线 -> 日常生活`, and non-activity-context `饮料 -> 餐饮`.
  `买充电宝 -> 购物`; borrowed/rented `充电宝 -> 日常生活`. Ask only when a
  new input does not reveal whether the power bank was bought or borrowed.

The finance mapping derived from the current ledger is approved but remains
conservative; it is not permission to invent new mappings. Add representative
redacted evaluations before implementation.

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

- `个人Agent_PRD_v1.0.1.md` — reviewed Phase 1 product scope and requirements.
- `docs/Agent框架Spike初步结果_2026-07-23.md` — evidence for ADK-first choice.
- `docs/MCP工具IR_v0.1.md` — cross-domain IR baseline; archived Finance text is
  non-normative, while its Finance contract summary is current.
- `docs/Finance MCP工具设计草案_v0.1.md` — canonical detailed Finance contract.
- `docs/Phase1技术方案设计计划_v0.1.md` — next-stage work order and the gate
  before implementation.
- `ECS安全加固实施记录_2026-07-23.md` — security, encryption, snapshot, and
  rollback record.
- `PROJECT_STATUS.md` — exact handoff point and ordered next work.
