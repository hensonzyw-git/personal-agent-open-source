"""Delivered per-device contexts are independent of semantic Sessions."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import EncryptedEnvelope,UtcTimestamp
revision='0018_dal_reply_contexts'
down_revision='0017_dal_timeline_events'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('dal_context_bindings',sa.Column('context_id',sa.Text(),primary_key=True),
        sa.Column('device_id',sa.Text(),sa.ForeignKey('devices.device_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('decision_id',sa.Text(),nullable=False),
        sa.Column('event_id',sa.Text(),sa.ForeignKey('conversation_events.event_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('binding_digest',sa.Text(),nullable=False),sa.Column('sealed_context',EncryptedEnvelope(),nullable=False),
        sa.Column('expires_at',UtcTimestamp(),nullable=False),sa.Column('consumed',sa.Integer(),nullable=False),
        sa.UniqueConstraint('device_id','decision_id',name='dal_context_device_decision'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM dal_context_bindings LIMIT 1')).first():raise RuntimeError('Delivered contexts exist; preserve bindings')
    op.drop_table('dal_context_bindings')
