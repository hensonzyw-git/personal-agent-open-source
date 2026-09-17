"""Persist cross-device decision validity before creating reply contexts."""
from alembic import op
import sqlalchemy as sa
revision='0021_dal_decision_projection'
down_revision='0020_dal_refused_commands'
branch_labels=None
depends_on=None

def upgrade():
    op.execute("\nCREATE TABLE dal_decision_states (\n\tdecision_id TEXT NOT NULL, \n\trequest_id TEXT NOT NULL, \n\tbinding_digest TEXT, \n\tevent_id TEXT, \n\tstatus TEXT NOT NULL, \n\tsource_seq INTEGER NOT NULL, \n\tCONSTRAINT pk_dal_decision_states PRIMARY KEY (decision_id), \n\tCONSTRAINT ck_dal_decision_states_dal_decision_projection CHECK (status IN ('pending','consumed','superseded') AND source_seq >= 1), \n\tCONSTRAINT fk_dal_decision_states_event_id FOREIGN KEY(event_id) REFERENCES conversation_events (event_id)\n)\n\n")

    op.execute('\nCREATE TABLE dal_notification_batches (\n\tbatch_id TEXT NOT NULL, \n\tdevice_id TEXT NOT NULL, \n\tevent_ids TEXT NOT NULL, \n\tcreated_at TEXT NOT NULL, \n\tCONSTRAINT pk_dal_notification_batches PRIMARY KEY (batch_id), \n\tCONSTRAINT fk_dal_notification_batches_device_id FOREIGN KEY(device_id) REFERENCES devices (device_id)\n)\n\n')
    op.execute('\nCREATE TABLE dal_notification_memberships (\n\tnotification_id TEXT NOT NULL, \n\tbatch_id TEXT NOT NULL, \n\tCONSTRAINT pk_dal_notification_memberships PRIMARY KEY (notification_id), \n\tCONSTRAINT fk_dal_notification_memberships_notification_id FOREIGN KEY(notification_id) REFERENCES dal_notifications (notification_id), \n\tCONSTRAINT fk_dal_notification_memberships_batch_id FOREIGN KEY(batch_id) REFERENCES dal_notification_batches (batch_id)\n)\n\n')

def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM dal_decision_states LIMIT 1')).first():
        raise RuntimeError('Decision validity must be preserved')
    if op.get_bind().execute(sa.text('SELECT 1 FROM dal_notification_batches LIMIT 1')).first():
        raise RuntimeError('Notification mappings must be preserved')
    op.drop_table('dal_notification_memberships')
    op.drop_table('dal_notification_batches')
    op.drop_table('dal_decision_states')
