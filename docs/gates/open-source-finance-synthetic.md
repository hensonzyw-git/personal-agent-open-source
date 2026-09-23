# Open-source Finance synthetic corpus gate — exception

Date: 2026-09-23
Source baseline: a49b13e626d2c5c43cca31fb7a67d92e81d9b03c
Scope: public historical sanitization and replacement of 20 user-provided, redacted Finance expressions with a separately versioned synthetic evaluation corpus. Remove private digest witnesses and old model outputs from all public history. Keep provenance verification fail-closed: an unregistered reviewed label must be rejected, and only test-only synthetic witnesses may exercise positive witness behavior.

The maintainer approved the pure-synthetic public corpus in the current conversation. The repository AGENTS.md normally requires separate per-round PRD and technical-design approvals before src edits. The maintainer explicitly chose to record this publication-privacy change as a one-round gate exception and proceed using the concrete design in the private preparation record FINANCE_PUBLIC_EVAL_DESIGN.md. This exception does not authorize production feature changes, deployment, provider calls, remote writes or publication.

Public evidence: final synthetic corpus, provenance tests, package verification, privacy scan and test results will be recorded before any publication request. Old private sample semantics or historical scores must not be represented as synthetic evidence.

Follow-up on 2026-09-23: a publication review found `TRV-005` labeled synthetic while carrying a live-defect provenance tag and specific event details. The maintainer requested removal from the current corpus and retained public history, a review of the other synthetic labels, and a guard against live-origin tags on synthetic cases. This is a privacy correction within the previously approved pure-synthetic publication exception, not a new product capability. The replacement must keep only the travel-transport classification purpose and use plainly fictional case details. Historical and package verification remain required before publication.

The same privacy correction also covers corresponding test and prompt examples and personal deployment account/key-path identifiers. The retained history is rewritten locally; its exact-marker scan, full Gitleaks classification, package inspection and rebuilt machine receipts are recorded in `docs/verification.md`. This record does not authorize a remote force push or changing repository visibility.
