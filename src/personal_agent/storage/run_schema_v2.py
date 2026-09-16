"""Frozen schema for migration 0013. Future schema changes use a new revision.

Core tables deliberately bypass ORM identity maps for every concurrency read.
This definition is also registered on Base.metadata for backup/schema checks.
"""

import sqlalchemy as sa

from personal_agent_core.sqlite import EncryptedEnvelope


RUN_TABLES = ("agent_tasks", "agent_runs", "agent_run_steps", "agent_run_outcomes",
              "agent_budget_reservations", "search_budget_days")


def register_run_tables(metadata):
    if "agent_tasks" in metadata.tables:
        return {name: metadata.tables[name] for name in RUN_TABLES}

    def col(name, kind=sa.Text, **kwargs):
        return sa.Column(name, kind, **kwargs)

    def counter(name, limit):
        return col(name, sa.Integer, nullable=False, server_default="0"), sa.CheckConstraint(
            f"{name} BETWEEN 0 AND {limit}", name=f"{name}_bounded")

    tasks = sa.Table("agent_tasks", metadata,
        col("task_id", primary_key=True),
        sa.Column("timeline_id", sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        col("runtime_version", sa.Integer, nullable=False, server_default="2"),
        col("status", nullable=False, server_default="active"),
        col("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("active_operation_id", sa.ForeignKey("operations.operation_id", ondelete="SET NULL")),
        col("sealed_goal", EncryptedEnvelope, nullable=False),
        col("sealed_constraints", EncryptedEnvelope, nullable=False),
        col("source_refs", EncryptedEnvelope), col("write_slot"),
        *counter("llm_used", 12), *counter("read_used", 9), *counter("web_used", 6),
        *counter("active_ms", 180000),
        sa.CheckConstraint("runtime_version = 2 AND revision >= 1", name="task_version"),
        sa.CheckConstraint("status IN ('active','waiting','paused','completed','cancelled')", name="task_status"),
    )
    runs = sa.Table("agent_runs", metadata,
        sa.Column("operation_id", sa.ForeignKey("operations.operation_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("task_id", sa.ForeignKey("agent_tasks.task_id", ondelete="CASCADE")),
        col("expected_task_revision", sa.Integer),
        col("runtime_version", sa.Integer, nullable=False, server_default="2"),
        col("state", nullable=False, server_default="accepted"),
        col("revision", sa.Integer, nullable=False, server_default="1"),
        col("lease_owner"), col("lease_until_ms", sa.Integer),
        col("fence", sa.Integer, nullable=False, server_default="1"),
        col("sealed_input_snapshot", EncryptedEnvelope, nullable=False),
        col("catalog_hash"), col("policy_hash"),
        sa.Column("timeline_id", sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        col("started_ms", sa.Integer, nullable=False), col("deadline_ms", sa.Integer, nullable=False),
        *counter("llm_used", 4), *counter("read_used", 3), *counter("web_used", 2),
        *counter("active_ms", 60000),
        sa.Column("candidate_operation_id", sa.ForeignKey("operations.operation_id", ondelete="SET NULL")),
        col("expected_source_version", sa.Integer),
        sa.Column("superseded_by_operation_id", sa.ForeignKey("operations.operation_id", ondelete="SET NULL"), unique=True),
        sa.CheckConstraint("runtime_version = 2 AND fence >= 1 AND revision >= 1", name="run_version"),
        sa.CheckConstraint("deadline_ms <= started_ms + 60000", name="message_deadline"),
        sa.CheckConstraint("state IN ('accepted','thinking','reading','finalizing','completed','partial','failed','cancelled','handoff','parked')", name="run_state"),
    )
    sa.Table("agent_run_steps", metadata,
        sa.Column("operation_id", sa.ForeignKey("agent_runs.operation_id", ondelete="CASCADE"), primary_key=True),
        col("step_no", sa.Integer, primary_key=True), col("call_no", sa.Integer, primary_key=True),
        col("attempt_no", sa.Integer, nullable=False), col("call_id", nullable=False),
        col("args_hash"), col("kind", nullable=False), col("status", nullable=False),
        col("sealed_args", EncryptedEnvelope), col("sealed_evidence", EncryptedEnvelope),
        col("provider_request_id"), col("attempt_nonce", nullable=False),
        col("started_ms", sa.Integer), col("ended_ms", sa.Integer),
        sa.CheckConstraint("step_no >= 1 AND call_no >= 0 AND attempt_no >= 1", name="step_numbers"),
        sa.UniqueConstraint("operation_id", "attempt_nonce", name="uq_run_attempt_nonce"),
    )
    sa.Table("agent_run_outcomes", metadata,
        sa.Column("operation_id", sa.ForeignKey("agent_runs.operation_id", ondelete="CASCADE"), primary_key=True),
        col("version", sa.Integer, nullable=False), col("outcome_kind", nullable=False),
        col("task_status", nullable=False), col("sealed_answer", EncryptedEnvelope),
        col("evidence_refs", EncryptedEnvelope), col("failure_code"),
    )
    reservations = sa.Table("agent_budget_reservations", metadata,
        sa.Column("operation_id", sa.ForeignKey("agent_runs.operation_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("task_id", sa.ForeignKey("agent_tasks.task_id", ondelete="CASCADE"), primary_key=True),
        col("reservation_no", sa.Integer, primary_key=True),
        col("task_revision", sa.Integer, nullable=False), col("state", nullable=False),
        *counter("llm", 12), *counter("read", 9), *counter("web", 6), *counter("time_ms", 180000),
        *counter("charged_llm", 12), *counter("charged_read", 9), *counter("charged_web", 6),
        *counter("charged_time_ms", 180000),
        sa.CheckConstraint("state IN ('held','charged','released','orphan_charge')", name="reservation_state"),
    )
    sa.Index("ix_agent_budget_held", reservations.c.task_id, reservations.c.state)
    sa.Table("search_budget_days", metadata,
        col("provider", primary_key=True), col("utc_day", primary_key=True),
        col("reserved_count", sa.Integer, nullable=False), col("limit_snapshot", sa.Integer, nullable=False),
        sa.CheckConstraint("reserved_count >= 0 AND reserved_count <= limit_snapshot", name="daily_budget"),
    )
    sa.Index("ix_agent_tasks_timeline", tasks.c.timeline_id, tasks.c.status)
    sa.Index("ix_agent_runs_task", runs.c.task_id, runs.c.state)
    return {name: metadata.tables[name] for name in RUN_TABLES}
