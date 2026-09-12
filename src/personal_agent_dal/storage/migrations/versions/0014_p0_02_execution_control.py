"""Durable execution stop receipts; no authority backfill."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import UtcTimestamp

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('execution_control_receipts',
        sa.Column('receipt_id', sa.Text(), primary_key=True),
        sa.Column('command_id', sa.Text(), nullable=False, unique=True),
        sa.Column('request_sha256', sa.Text(), nullable=False),
        sa.Column('feature_id', sa.Text(), sa.ForeignKey('features.feature_id'), nullable=False),
        sa.Column('operation', sa.Text(), nullable=False),
        sa.Column('requested_by', sa.Text(), nullable=False),
        sa.Column('expected_gate_version', sa.Integer(), nullable=False),
        sa.Column('gate_version', sa.Integer(), nullable=False),
        sa.Column('approval_epoch', sa.Integer(), nullable=False),
        sa.Column('code', sa.Text(), nullable=False),
        sa.Column('recorded_at', UtcTimestamp(), nullable=False),
        sa.CheckConstraint("(length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*')", name='request_sha256_hex'),
        sa.CheckConstraint("operation IN ('pause', 'cancel')", name='operation'),
        sa.CheckConstraint('expected_gate_version >= 1 AND gate_version = expected_gate_version + 1', name='gate_version'),
        sa.CheckConstraint('approval_epoch >= 1', name='approval_epoch'),
        sa.CheckConstraint("(operation = 'pause' AND code = 'PAUSED') OR (operation = 'cancel' AND code = 'CANCELLED')", name='code'),
    )
    op.create_index('ix_execution_control_receipts_feature_id', 'execution_control_receipts', ['feature_id'])


def downgrade():
    op.drop_table('execution_control_receipts')
