"""Encrypted Timeline intake and stable read snapshots; no automatic Jobs."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import EncryptedEnvelope, UtcTimestamp

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('development_requests',
        sa.Column('request_id', sa.Text(), primary_key=True),
        sa.Column('source_message_ref', sa.Text(), nullable=False, unique=True),
        sa.Column('request_sha256', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('created_at', UtcTimestamp(), nullable=False),
        sa.CheckConstraint('version >= 1', name='request_version'),
        sa.CheckConstraint("status IN ('clarifying','ready','linked','cancelled')", name='request_status'))
    op.create_table('development_workflows',
        sa.Column('workflow_id',sa.Text(),primary_key=True),
        sa.Column('request_id',sa.Text(),sa.ForeignKey('development_requests.request_id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.Column('feature_id',sa.Text(),sa.ForeignKey('features.feature_id',ondelete='RESTRICT'),unique=True),
        sa.Column('contract_version',sa.Text(),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('phase',sa.Text(),nullable=False),
        sa.Column('status',sa.Text(),nullable=False),
        sa.CheckConstraint("contract_version = 'dal.timeline-workflow/1.0' AND version >= 1",name='workflow_contract'),
        sa.CheckConstraint("status IN ('active','blocked','paused','completed','cancelled')",name='workflow_status'),
        sa.CheckConstraint("(phase = 'accepted' AND status = 'completed') OR (phase != 'accepted' AND status != 'completed')",name='workflow_completion'))
    op.create_table('development_request_revisions',
        sa.Column('revision_id', sa.Text(), primary_key=True),
        sa.Column('request_id', sa.Text(), sa.ForeignKey('development_requests.request_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('body_sha256', sa.Text(), nullable=False),
        sa.Column('sealed_body', EncryptedEnvelope(), nullable=False),
        sa.UniqueConstraint('request_id','revision',name='request_revision'))
    op.create_table('development_commands',
        sa.Column('command_id',sa.Text(),primary_key=True),
        sa.Column('body_sha256',sa.Text(),nullable=False),
        sa.Column('sealed_result',EncryptedEnvelope(),nullable=False))
    op.create_table('development_event_streams',
        sa.Column('stream_id',sa.Text(),primary_key=True),
        sa.Column('singleton',sa.Integer(),nullable=False,unique=True),
        sa.Column('next_seq',sa.Integer(),nullable=False),
        sa.Column('tail_digest',sa.Text(),nullable=False),
        sa.CheckConstraint('singleton = 1 AND next_seq >= 1',name='stream_singleton'))
    op.create_table('development_events',
        sa.Column('event_id',sa.Text(),primary_key=True),
        sa.Column('stream_id',sa.Text(),sa.ForeignKey('development_event_streams.stream_id'),nullable=False),
        sa.Column('seq',sa.Integer(),nullable=False),
        sa.Column('kind',sa.Text(),nullable=False),
        sa.Column('request_id',sa.Text(),sa.ForeignKey('development_requests.request_id'),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('body_digest',sa.Text(),nullable=False),
        sa.Column('prev_digest',sa.Text(),nullable=False),
        sa.Column('digest',sa.Text(),nullable=False),
        sa.Column('sealed_body',EncryptedEnvelope(),nullable=False),
        sa.UniqueConstraint('stream_id','seq',name='event_sequence'),
        sa.CheckConstraint('seq >= 1',name='sequence_positive'))
    op.create_table('development_event_consumers',
        sa.Column('stream_id',sa.Text(),sa.ForeignKey('development_event_streams.stream_id'),primary_key=True),
        sa.Column('consumer_id',sa.Text(),primary_key=True),
        sa.Column('ack_seq',sa.Integer(),nullable=False),
        sa.Column('tail_digest',sa.Text(),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.CheckConstraint("consumer_id = 'pa-timeline' AND ack_seq >= 0 AND version >= 1",name='consumer_cursor'))
    op.create_table('development_query_snapshots',
        sa.Column('snapshot_id',sa.Text(),primary_key=True),
        sa.Column('subject',sa.Text(),nullable=False),
        sa.Column('view',sa.Text(),nullable=False),
        sa.Column('created_at',UtcTimestamp(),nullable=False),
        sa.Column('expires_at',UtcTimestamp(),nullable=False),
        sa.Column('sealed_body',EncryptedEnvelope(),nullable=False))


def downgrade():
    for name in ('development_query_snapshots','development_event_consumers','development_events','development_event_streams',
                 'development_commands','development_request_revisions','development_workflows','development_requests'):
        op.drop_table(name)
