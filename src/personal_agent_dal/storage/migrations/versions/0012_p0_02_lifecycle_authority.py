"""P0-02 bound authority, consumption receipt and immutable arrival evidence.

Historical attempts retain NULL authority and fail closed; never infer approval.
"""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import UtcTimestamp

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('provider_attempts', sa.Column('feature_version', sa.Integer(), nullable=True))
    op.add_column('provider_attempts', sa.Column('capability_epoch', sa.Integer(), nullable=True))
    op.add_column('provider_attempts', sa.Column('consumption_receipt_id', sa.Text(), nullable=True))
    op.create_table('provider_result_observations',
        sa.Column('observation_id', sa.Text(), primary_key=True),
        sa.Column('attempt_id', sa.Text(), sa.ForeignKey('provider_attempts.attempt_id'), nullable=False),
        sa.Column('owner_id', sa.Text(), nullable=False),
        sa.Column('fence', sa.Integer(), nullable=False),
        sa.Column('expected_version', sa.Integer(), nullable=False),
        sa.Column('digest', sa.Text(), nullable=False),
        sa.Column('code', sa.Text(), nullable=False),
        sa.Column('recorded_at', UtcTimestamp(), nullable=False),
        sa.CheckConstraint("(length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*')", name='digest_hex'),
    )
    op.create_index('ix_provider_result_observations_attempt_id', 'provider_result_observations', ['attempt_id'])


def downgrade():
    op.drop_table('provider_result_observations')
    with op.batch_alter_table('provider_attempts') as batch:
        batch.drop_column('consumption_receipt_id')
        batch.drop_column('capability_epoch')
        batch.drop_column('feature_version')
