"""commit capability persistence (R09-A3)

Adds `commit_capabilities` — the persistent one-time commit capability row
(DAL-031 / DAL-004 §5), the durable half of the pure issue/consume gates in
`personal_agent_dal.machine.commit_capability`. CAS on `state_version`;
consumption, intent, audit and outbox are the caller's one transaction.

No business authority is added here: the frozen transition registry is
untouched, and no engine spec consumes this table yet — the controller
composition and the deterministic git executor land in the next R09-A3
slice.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'commit_capabilities',
        sa.Column('capability_id', sa.Text(), nullable=False),
        sa.Column('schema_version', sa.Text(), nullable=False),
        sa.Column('state_version', sa.Integer(), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('approval_id', sa.Text(), nullable=False),
        sa.Column('feature_id', sa.Text(), nullable=False),
        sa.Column('task_id', sa.Text(), nullable=False),
        sa.Column('repository_id', sa.Text(), nullable=False),
        sa.Column('allowed_paths_json', sa.Text(), nullable=False),
        sa.Column('refs', sa.Text(), nullable=True),
        sa.Column('artifact_or_diff_sha256', sa.Text(), nullable=False),
        sa.Column('base_sha', sa.Text(), nullable=False),
        sa.Column('result_sha', sa.Text(), nullable=False),
        sa.Column('lease_epoch', sa.Integer(), nullable=False),
        sa.Column('capability_epoch', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.Integer(), nullable=False),
        sa.Column('max_uses', sa.Integer(), nullable=False),
        sa.Column('uses_consumed', sa.Integer(), nullable=False),
        sa.Column('policy_version', sa.Text(), nullable=False),
        sa.Column('issue_idempotency_key', sa.Text(), nullable=False),
        sa.Column('trailers_json', sa.Text(), nullable=False),
        sa.Column('revoked_at', sa.Integer(), nullable=True),
        sa.Column('consumed_by', sa.Text(), nullable=True),
        sa.Column('consumed_at', sa.Integer(), nullable=True),
        sa.Column('created_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint("state IN ('issued', 'consumed', 'superseded')", name='ck_commit_capabilities_state'),
        sa.CheckConstraint("action IN ('commit_candidate')", name='ck_commit_capabilities_action'),
        sa.CheckConstraint('state_version >= 1', name='ck_commit_capabilities_state_version_positive'),
        sa.CheckConstraint('uses_consumed >= 0 AND uses_consumed <= max_uses', name='ck_commit_capabilities_uses_within_max'),
        sa.CheckConstraint('max_uses = 1', name='ck_commit_capabilities_max_uses_is_one'),
        sa.CheckConstraint('(consumed_by IS NULL) = (consumed_at IS NULL)', name='ck_commit_capabilities_consumption_is_all_or_nothing'),
        sa.CheckConstraint("(consumed_by IS NULL) = (state = 'issued') OR (state = 'consumed' AND consumed_by IS NOT NULL)", name='ck_commit_capabilities_state_matches_consumption'),
        sa.CheckConstraint("length(base_sha) = 40 AND base_sha NOT GLOB '*[^0-9a-f]*'", name='ck_commit_capabilities_base_sha_hex'),
        sa.CheckConstraint("length(result_sha) = 40 AND result_sha NOT GLOB '*[^0-9a-f]*'", name='ck_commit_capabilities_result_sha_hex'),
        sa.CheckConstraint("length(artifact_or_diff_sha256) = 64 AND artifact_or_diff_sha256 NOT GLOB '*[^0-9a-f]*'", name='ck_commit_capabilities_artifact_or_diff_sha256_hex'),
        sa.CheckConstraint('lease_epoch >= 0 AND capability_epoch >= 0', name='ck_commit_capabilities_epochs_non_negative'),
        sa.CheckConstraint('expires_at >= 0', name='ck_commit_capabilities_expires_at_non_negative'),
        sa.PrimaryKeyConstraint('capability_id', name='pk_commit_capabilities'),
        sa.UniqueConstraint('issue_idempotency_key', name='uq_commit_capabilities_issue_idempotency_key'),
    )
    with op.batch_alter_table('commit_capabilities', schema=None) as batch_op:
        batch_op.create_index('ix_commit_capabilities_feature_id', ['feature_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('commit_capabilities', schema=None) as batch_op:
        batch_op.drop_index('ix_commit_capabilities_feature_id')
    op.drop_table('commit_capabilities')
