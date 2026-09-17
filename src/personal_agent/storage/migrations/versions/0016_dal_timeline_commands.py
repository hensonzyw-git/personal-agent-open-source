"""PA encrypted Timeline command outbox."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import EncryptedEnvelope, UtcTimestamp
revision='0016_dal_timeline_commands'
down_revision='0015_dal_delivery_status'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('dal_timeline_commands',
        sa.Column('command_id',sa.Text(),primary_key=True),
        sa.Column('device_id',sa.Text(),sa.ForeignKey('devices.device_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('key_thumbprint',sa.Text(),nullable=False),
        sa.Column('body_sha256',sa.Text(),nullable=False),
        sa.Column('sealed_body',EncryptedEnvelope(),nullable=False),
        sa.Column('sealed_receipt',EncryptedEnvelope(),nullable=True),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('attempts',sa.Integer(),nullable=False),
        sa.Column('created_at',UtcTimestamp(),nullable=False),
        sa.CheckConstraint("status IN ('queued','delivery_unknown','accepted','cancelled')",name='timeline_command_status'),
        sa.CheckConstraint('attempts >= 0',name='timeline_command_attempts'),
        sa.CheckConstraint("status != 'cancelled' OR attempts = 0",name='timeline_cancel_unsent'),
        sa.CheckConstraint("status != 'delivery_unknown' OR attempts > 0",name='timeline_unknown_sent'),
        sa.CheckConstraint("(status = 'accepted' AND sealed_receipt IS NOT NULL AND attempts > 0) OR (status != 'accepted' AND sealed_receipt IS NULL)",name='timeline_receipt'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM dal_timeline_commands LIMIT 1')).first():
        raise RuntimeError('Timeline commands exist; preserve outbox and restore coordinated backup')
    op.drop_table('dal_timeline_commands')
