"""Operation-specific revocation receipts and replacement policy lease binding."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import UtcTimestamp
revision='0017'
down_revision='0016'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('resume_revoke_receipts',
        sa.Column('request_id',sa.Text(),primary_key=True),
        sa.Column('receipt_id',sa.Text(),nullable=False,unique=True),
        sa.Column('request_sha256',sa.Text(),nullable=False),
        sa.Column('decision_id',sa.Text(),sa.ForeignKey('resume_revocations.decision_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('actor_id',sa.Text(),nullable=False),
        sa.Column('body',sa.Text(),nullable=False),
        sa.Column('recorded_at',UtcTimestamp(),nullable=False))
    op.create_table('resume_lease_issuances',
        sa.Column('intent_id',sa.Text(),sa.ForeignKey('resume_episodes.intent_id',ondelete='RESTRICT'),primary_key=True),
        sa.Column('job_lease_epoch',sa.Integer(),primary_key=True),
        sa.Column('lease_id',sa.Text(),sa.ForeignKey('leases.lease_id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.CheckConstraint('job_lease_epoch >= 1',name='positive_epoch'))


def downgrade():
    for table in ('resume_lease_issuances','resume_revoke_receipts'):
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first():
            raise RuntimeError('resume authority exists; preserve records and restore coordinated backup')
    op.drop_table('resume_lease_issuances')
    op.drop_table('resume_revoke_receipts')
