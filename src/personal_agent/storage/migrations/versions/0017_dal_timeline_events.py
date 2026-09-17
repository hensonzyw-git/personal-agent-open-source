"""Durable contiguous consumer and exactly-once local Timeline projection."""
from alembic import op
import sqlalchemy as sa
revision='0017_dal_timeline_events'
down_revision='0016_dal_timeline_commands'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('dal_consumer_cursors',
        sa.Column('consumer_id',sa.Text(),primary_key=True),sa.Column('stream_id',sa.Text(),nullable=False),
        sa.Column('received_seq',sa.Integer(),nullable=False),sa.Column('acked_seq',sa.Integer(),nullable=False),
        sa.Column('tail_digest',sa.Text(),nullable=False),sa.Column('version',sa.Integer(),nullable=False),
        sa.CheckConstraint("consumer_id = 'pa-timeline' AND 0 <= acked_seq AND acked_seq <= received_seq AND version >= 1",name='dal_cursor_order'))
    op.create_table('dal_event_inbox',
        sa.Column('event_id',sa.Text(),primary_key=True),sa.Column('stream_id',sa.Text(),nullable=False),
        sa.Column('seq',sa.Integer(),nullable=False),sa.Column('digest',sa.Text(),nullable=False),
        sa.Column('workflow_id',sa.Text(),nullable=False),
        sa.Column('timeline_event_id',sa.Text(),sa.ForeignKey('conversation_events.event_id',ondelete='RESTRICT'),nullable=False),
        sa.UniqueConstraint('stream_id','seq',name='dal_inbox_sequence'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM dal_event_inbox LIMIT 1')).first():
        raise RuntimeError('Timeline events exist; preserve inbox and cursor')
    op.drop_table('dal_event_inbox');op.drop_table('dal_consumer_cursors')
