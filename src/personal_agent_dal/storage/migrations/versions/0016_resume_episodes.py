"""Unique, atomic intent to replacement Job binding; preserve all authority."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import UtcTimestamp
revision='0016'
down_revision='0015'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('resume_episodes',
        sa.Column('intent_id',sa.Text(),sa.ForeignKey('dispatch_intents.intent_id',ondelete='RESTRICT'),primary_key=True),
        sa.Column('attempt_id',sa.Text(),sa.ForeignKey('provider_attempts.attempt_id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.Column('job_id',sa.Text(),sa.ForeignKey('worker_jobs.job_id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.Column('created_at',UtcTimestamp(),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM resume_episodes LIMIT 1')).first():
        raise RuntimeError('consumed scheduling authority exists; preserve records and restore coordinated backup')
    op.drop_table('resume_episodes')
