"""P0-02 action, attempt and execution-gate persistence.

The existing ``worker_jobs`` lease is a transport-level ownership record and
``leases`` is a policy-level worker claim.  Neither records the crash boundary
of a provider action itself.  This revision adds the smallest three tables that
can express that boundary without changing an existing machine contract:

* ``workflow_actions`` is the version/snapshot-bound business action;
* ``provider_attempts`` is the CAS-pre-reserved outward attempt and its fences;
* ``execution_gates`` is the per-feature stop/approval-epoch authority.

No existing feature is backfilled with a gate.  Treating absent history as a
new open gate would silently authorise historical rows; the composition layer
must create a gate explicitly for every P0-02 action it starts.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-12
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_actions",
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("feature_id", sa.Text(), nullable=False),
        sa.Column("stage_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("action_key", sa.Text(), nullable=False),
        sa.Column("input_binding_sha256", sa.Text(), nullable=False),
        sa.Column("execution_snapshot_sha256", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active_attempt_id", sa.Text(), nullable=True),
        sa.Column("created_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint("length(kind) > 0", name="kind_non_empty"),
        sa.CheckConstraint(
            "length(action_key) > 0", name="action_key_non_empty"
        ),
        sa.CheckConstraint(
            "(length(input_binding_sha256) = 64 "
            "AND input_binding_sha256 NOT GLOB '*[^0-9a-f]*')",
            name="input_binding_sha256_hex",
        ),
        sa.CheckConstraint(
            "(length(execution_snapshot_sha256) = 64 "
            "AND execution_snapshot_sha256 NOT GLOB '*[^0-9a-f]*')",
            name="execution_snapshot_sha256_hex",
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.ForeignKeyConstraint(
            ["feature_id"], ["features.feature_id"], name="fk_workflow_actions_feature_id"
        ),
        sa.PrimaryKeyConstraint("action_id", name="pk_workflow_actions"),
        sa.UniqueConstraint(
            "feature_id", "action_key", name="uq_workflow_actions_feature_action_key"
        ),
    )
    op.create_index("ix_workflow_actions_feature_id", "workflow_actions", ["feature_id"])

    op.create_table(
        "provider_attempts",
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=True),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("dispatch_started_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("lease_id", sa.Text(), nullable=True),
        sa.Column("job_lease_epoch", sa.Integer(), nullable=True),
        sa.Column("policy_lease_epoch", sa.Integer(), nullable=True),
        sa.Column("approval_epoch", sa.Integer(), nullable=True),
        sa.Column("result_digest", sa.Text(), nullable=True),
        sa.Column("result_recorded_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True),
        sa.Column("result_consumed_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True),
        sa.Column("created_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint(
            "state IN ('prepared', 'dispatching', 'result_recorded', 'unknown', 'superseded')",
            name="state",
        ),
        sa.CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("fence >= 0", name="fence_non_negative"),
        sa.CheckConstraint(
            "job_lease_epoch IS NULL OR job_lease_epoch >= 0",
            name="job_lease_epoch_non_negative",
        ),
        sa.CheckConstraint(
            "policy_lease_epoch IS NULL OR policy_lease_epoch >= 0",
            name="policy_lease_epoch_non_negative",
        ),
        sa.CheckConstraint(
            "approval_epoch IS NULL OR approval_epoch >= 0",
            name="approval_epoch_non_negative",
        ),
        sa.CheckConstraint(
            "(result_digest IS NULL OR (length(result_digest) = 64 "
            "AND result_digest NOT GLOB '*[^0-9a-f]*'))",
            name="result_digest_hex",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"], ["workflow_actions.action_id"], name="fk_provider_attempts_action_id"
        ),
        sa.ForeignKeyConstraint(
            ["lease_id"], ["leases.lease_id"], name="fk_provider_attempts_lease_id"
        ),
        sa.PrimaryKeyConstraint("attempt_id", name="pk_provider_attempts"),
        sa.UniqueConstraint(
            "action_id", "attempt_no", name="uq_provider_attempts_action_attempt"
        ),
    )
    op.create_index("ix_provider_attempts_action_id", "provider_attempts", ["action_id"])

    op.create_table(
        "execution_gates",
        sa.Column("feature_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("approval_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint(
            "mode IN ('open', 'paused', 'cancelled', 'delivered')",
            name="mode",
        ),
        sa.CheckConstraint(
            "approval_epoch >= 0", name="approval_epoch_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["feature_id"], ["features.feature_id"], name="fk_execution_gates_feature_id"
        ),
        sa.PrimaryKeyConstraint("feature_id", name="pk_execution_gates"),
    )


def downgrade() -> None:
    op.drop_table("execution_gates")
    op.drop_index("ix_provider_attempts_action_id", table_name="provider_attempts")
    op.drop_table("provider_attempts")
    op.drop_index("ix_workflow_actions_feature_id", table_name="workflow_actions")
    op.drop_table("workflow_actions")
