"""Separate phone authorization context and durable public-request identity."""
from alembic import op
import sqlalchemy as sa
revision='0023_dal_authorization_context'
down_revision='0022_dal_command_retry'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('dal_timeline_commands',sa.Column('submission_sha256',sa.Text()))
    op.execute('\nCREATE TABLE dal_authorization_contexts (\n\tcontext_id TEXT NOT NULL, \n\tdevice_id TEXT NOT NULL, \n\tkey_thumbprint TEXT NOT NULL, \n\tproposal_id TEXT NOT NULL, \n\tbinding_digest TEXT NOT NULL, \n\tsealed_context TEXT NOT NULL, \n\texpires_at TEXT NOT NULL, \n\ttoken_jti TEXT, \n\ttoken_expires_at TEXT, \n\tcommand_id TEXT, \n\tCONSTRAINT pk_dal_authorization_contexts PRIMARY KEY (context_id), \n\tCONSTRAINT authorization_context_device UNIQUE (device_id, key_thumbprint, proposal_id), \n\tCONSTRAINT fk_dal_authorization_contexts_device_id FOREIGN KEY(device_id) REFERENCES devices (device_id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_dal_authorization_contexts_command_id FOREIGN KEY(command_id) REFERENCES dal_timeline_commands (command_id)\n)\n\n')

def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM dal_authorization_contexts')).scalar_one():
        raise RuntimeError('Authorization history exists; reconcile and use the reviewed backup rollback plan')
    op.drop_table('dal_authorization_contexts')
    op.drop_column('dal_timeline_commands','submission_sha256')
