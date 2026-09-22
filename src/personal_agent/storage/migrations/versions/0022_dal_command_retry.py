"""Persist command retry deadlines and halt reasons without falsifying outcomes."""
from alembic import op
import sqlalchemy as sa
revision='0022_dal_command_retry'
down_revision='0021_dal_decision_projection'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('dal_timeline_commands',sa.Column('next_attempt_at',sa.Text(),nullable=True))
    op.add_column('dal_timeline_commands',sa.Column('delivery_error',sa.Text(),nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM dal_timeline_commands WHERE next_attempt_at IS NOT NULL OR delivery_error IS NOT NULL LIMIT 1")).first():
        raise RuntimeError('Command retry and unknown-result evidence must be preserved')
    op.drop_column('dal_timeline_commands','delivery_error')
    op.drop_column('dal_timeline_commands','next_attempt_at')
