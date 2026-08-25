"""worker transport enrollment and checkpoint records

`DAL-R04`/`DAL-R05` runnable layer. Adds the two DWS-side tables the thin-http
transport needs: `worker_enrollments` (durable machine registry for transport
auth) and `worker_checkpoints` (ECS-side recovery evidence fenced by job +
lease epoch + sequence, idempotent on job+sequence). No business authority is
added: these are transport records only.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-26
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'worker_enrollments',
        sa.Column('worker_id', sa.Text(), nullable=False),
        sa.Column('capabilities', sa.Text(), nullable=False),
        sa.Column('created_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column('revoked_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=True),
        sa.CheckConstraint("length(worker_id) >= 1", name='ck_worker_enrollments_worker_id_nonempty'),
        sa.PrimaryKeyConstraint('worker_id', name='pk_worker_enrollments'),
    )

    op.create_table(
        'worker_checkpoints',
        sa.Column('checkpoint_id', sa.Text(), nullable=False),
        sa.Column('job_id', sa.Text(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('lease_epoch', sa.Integer(), nullable=False),
        sa.Column('artifact_sha256', sa.Text(), nullable=False),
        sa.Column('artifact_size_bytes', sa.Integer(), nullable=False),
        sa.Column('changed_files', sa.Text(), nullable=False),
        sa.Column('sensitivity', sa.Text(), nullable=False),
        sa.Column('recorded_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint('sequence >= 0', name='ck_worker_checkpoints_sequence_nonnegative'),
        sa.CheckConstraint('lease_epoch >= 0', name='ck_worker_checkpoints_lease_epoch_nonnegative'),
        sa.CheckConstraint('artifact_size_bytes >= 1', name='ck_worker_checkpoints_artifact_size_positive'),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'",
            name='ck_worker_checkpoints_artifact_sha256_hex',
        ),
        sa.CheckConstraint(
            "sensitivity IN ('checkpoint', 'diff', 'log')",
            name='ck_worker_checkpoints_sensitivity',
        ),
        sa.PrimaryKeyConstraint('checkpoint_id', name='pk_worker_checkpoints'),
        sa.ForeignKeyConstraint(
            ['job_id'], ['worker_jobs.job_id'],
            name='fk_worker_checkpoints_job_id_worker_jobs',
            ondelete='RESTRICT',
        ),
        sa.UniqueConstraint('job_id', 'sequence', name='uq_worker_checkpoints_job_sequence'),
    )
    op.create_index('ix_worker_checkpoints_job_id', 'worker_checkpoints', ['job_id'])


def downgrade() -> None:
    op.drop_index('ix_worker_checkpoints_job_id', table_name='worker_checkpoints')
    op.drop_table('worker_checkpoints')
    op.drop_table('worker_enrollments')
