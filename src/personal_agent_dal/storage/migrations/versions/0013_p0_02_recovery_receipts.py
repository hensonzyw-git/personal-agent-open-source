"""P0-02 I3 recovery receipts; no backfill or inferred provider outcome."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import UtcTimestamp

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_provider_attempts_job_id", "provider_attempts", ["job_id"])
    op.create_table('provider_recovery_receipts',
        sa.Column('receipt_id', sa.Text(), primary_key=True),
        sa.Column('command_id', sa.Text(), nullable=False, unique=True),
        sa.Column('request_sha256', sa.Text(), nullable=False),
        sa.Column('attempt_id', sa.Text(), sa.ForeignKey('provider_attempts.attempt_id'), nullable=False),
        sa.Column('expected_version', sa.Integer(), nullable=False),
        sa.Column('requested_by', sa.Text(), nullable=False),
        sa.Column('owner_id', sa.Text(), nullable=False),
        sa.Column('fence', sa.Integer(), nullable=False),
        sa.Column('probe_status', sa.Text(), nullable=False),
        sa.Column('probe_code', sa.Text(), nullable=False),
        sa.Column('evidence_sha256', sa.Text(), nullable=True),
        sa.Column('code', sa.Text(), nullable=False),
        sa.Column('recorded_at', UtcTimestamp(), nullable=False),
        sa.CheckConstraint("(length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*')", name='request_sha256_hex'),
        sa.CheckConstraint("(evidence_sha256 IS NULL OR (length(evidence_sha256) = 64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'))", name='evidence_sha256_hex'),
        sa.CheckConstraint("probe_status IN ('unavailable', 'running', 'stopped', 'partial', 'complete')", name='probe_status'),
    )
    op.create_index('ix_provider_recovery_receipts_attempt_id', 'provider_recovery_receipts', ['attempt_id'])


def downgrade():
    op.drop_table('provider_recovery_receipts')
    op.drop_index('ix_provider_attempts_job_id', table_name='provider_attempts')
