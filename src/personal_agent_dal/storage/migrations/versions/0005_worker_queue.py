"""worker job queue and idempotent result receipt

`DAL-016` runnable layer. Adds the two tables the Home Mac Worker claims and
completes: `worker_jobs` (the durable job queue, CAS-claimed on
`state='pending'`, heartbeated, and reclaimed on lease expiry) and
`worker_result_receipts` (one idempotent result per job). The state-machine
`leases` table stays the policy-level lease bound to feature + approval/capability
epochs; this is the lower-level "which worker process runs this job right now"
layer, kept separate so the frozen lease/epoch policy semantics are untouched.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_JOB_STATES = (
    "'pending', 'leased', 'running', 'succeeded', 'failed', 'expired', 'cancelled'"
)


def upgrade() -> None:
    op.create_table(
        'worker_jobs',
        sa.Column('job_id', sa.Text(), nullable=False),
        sa.Column('feature_id', sa.Text(), nullable=False),
        sa.Column('repository_id', sa.Text(), nullable=False),
        sa.Column('base_sha', sa.Text(), nullable=False),
        sa.Column('branch_name', sa.Text(), nullable=False),
        sa.Column('toolchain_ref', sa.Text(), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('lease_epoch', sa.Integer(), nullable=False),
        sa.Column('worker_id', sa.Text(), nullable=True),
        sa.Column('lease_expires_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=True),
        sa.Column('heartbeat_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=True),
        sa.Column('result_sha256', sa.Text(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column('updated_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint(
            f"state IN ({_JOB_STATES})",
            name='ck_worker_jobs_state',
        ),
        sa.CheckConstraint(
            "length(base_sha) = 40 AND base_sha NOT GLOB '*[^0-9a-f]*'",
            name='ck_worker_jobs_base_sha_hex',
        ),
        sa.CheckConstraint('attempt_count >= 0', name='ck_worker_jobs_attempt_count_non_negative'),
        sa.CheckConstraint('lease_epoch >= 0', name='ck_worker_jobs_lease_epoch_non_negative'),
        sa.CheckConstraint(
            "(worker_id IS NULL) = (lease_expires_at IS NULL) "
            "AND (worker_id IS NULL) = (heartbeat_at IS NULL)",
            name='ck_worker_jobs_lease_fields_travel_together',
        ),
        sa.CheckConstraint(
            "((state IN ('leased', 'running')) AND worker_id IS NOT NULL) OR "
            "((state NOT IN ('leased', 'running')) AND worker_id IS NULL)",
            name='ck_worker_jobs_active_state_has_lease',
        ),
        sa.PrimaryKeyConstraint('job_id', name='pk_worker_jobs'),
    )
    op.create_index('ix_worker_jobs_state', 'worker_jobs', ['state'])
    op.create_index('ix_worker_jobs_feature_id', 'worker_jobs', ['feature_id'])

    op.create_table(
        'worker_result_receipts',
        sa.Column('receipt_id', sa.Text(), nullable=False),
        sa.Column('job_id', sa.Text(), nullable=False),
        sa.Column('result_sha256', sa.Text(), nullable=False),
        sa.Column('receipt_schema_version', sa.Text(), nullable=False),
        sa.Column('recorded_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint(
            "length(result_sha256) = 64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'",
            name='ck_worker_result_receipts_result_sha256_hex',
        ),
        sa.PrimaryKeyConstraint('receipt_id', name='pk_worker_result_receipts'),
        sa.ForeignKeyConstraint(
            ['job_id'], ['worker_jobs.job_id'],
            name='fk_worker_result_receipts_job_id_worker_jobs',
            ondelete='RESTRICT',
        ),
        sa.UniqueConstraint('job_id', name='uq_worker_result_receipts_job_id'),
    )


def downgrade() -> None:
    op.drop_table('worker_result_receipts')
    op.drop_index('ix_worker_jobs_feature_id', table_name='worker_jobs')
    op.drop_index('ix_worker_jobs_state', table_name='worker_jobs')
    op.drop_table('worker_jobs')
