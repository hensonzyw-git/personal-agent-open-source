"""Development Agent Loop workflow service.

A strictly single-user, deterministic orchestration service for Henson's
Development Agent Loop. It owns the Feature/RecoveryCase state machines,
idempotency, audit, outbox, approvals and the Timeline/notification
projection for development work.

Boundary: this package must stay independent of the Finance connectors and of
every production credential. It never imports `personal_data_mcp`, never reads
an Feishu/provider secret, and never opens a network, GitHub or Worker path in
the DAL-007–013 synthetic slice. The config loader in `config.py` is the
single choke point that enforces that isolation fail-closed.

Reference: docs/开发Agent闭环开发拆解_v0.1.md §5 (Wave 1), and the frozen
contracts under docs/dal/ (DAL-001–003 contract package, DAL-004 threat model).
"""
