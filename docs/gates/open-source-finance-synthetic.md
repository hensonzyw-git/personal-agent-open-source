# Open-source Finance synthetic corpus gate — exception

Date: 2026-09-23
Source baseline: a49b13e626d2c5c43cca31fb7a67d92e81d9b03c
Scope: public historical sanitization and replacement of 20 user-provided, redacted Finance expressions with a separately versioned synthetic evaluation corpus. Remove private digest witnesses and old model outputs from all public history. Keep provenance verification fail-closed: an unregistered reviewed label must be rejected, and only test-only synthetic witnesses may exercise positive witness behavior.

The maintainer approved the pure-synthetic public corpus in the current conversation. The repository AGENTS.md normally requires separate per-round PRD and technical-design approvals before src edits. The maintainer explicitly chose to record this publication-privacy change as a one-round gate exception and proceed using the concrete design in the private preparation record FINANCE_PUBLIC_EVAL_DESIGN.md. This exception does not authorize production feature changes, deployment, provider calls, remote writes or publication.

Public evidence: final synthetic corpus, provenance tests, package verification, privacy scan and test results will be recorded before any publication request. Old private sample semantics or historical scores must not be represented as synthetic evidence.
