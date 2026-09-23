# Contributor and agent rules

This is a single-user reference implementation. The backend owns Agent/MCP policy;
iOS is the interaction surface and the development Worker is a bounded executor.

Before significant feature, permission or architecture changes, obtain maintainer
approval of a scoped PRD, then a technical design. Record the round under docs/gates/.
Inspect current changes before editing. Do not alter unrelated work.
Keep policy outside model adapters. Preserve authorization, idempotency, audit and
external-result evidence. Test failure and concurrency paths, not only happy paths.
Never commit credentials, personal records, production configuration or raw logs.
Use synthetic fixtures. Keep frozen contracts and packaged copies consistent;
never weaken hash or authorization assertions to pass a test.
Run relevant offline tests and git diff --check. Report exact verification limits.
Do not connect production services, call paid models, push or deploy without explicit
authorization. AGENTS.md and CLAUDE.md must remain byte-identical.
