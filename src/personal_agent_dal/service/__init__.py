"""DAL-R05 Dev Workflow Service — the thin-http transport composition root.

The service is deliberately thin: it wraps the frozen queue/lease primitives in
`personal_agent_dal.worker.queue` behind the `docs/dal/openapi/worker-transport-v1.yaml`
endpoints, adds transport auth (enrollment + HMAC token), and carries the
cross-cutting bounds (kill switch, rate limit, bounded body, redacted audit).
It owns no business state machine — `worker_jobs` is the authority for "which
worker runs which job now", while feature/run authority stays with the DWS
business layer this transport serves.
"""
