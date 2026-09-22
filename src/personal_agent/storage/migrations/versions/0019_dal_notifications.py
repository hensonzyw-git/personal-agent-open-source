"""Durable coalesced development notifications."""
from alembic import op
import sqlalchemy as sa
revision='0019_dal_notifications'
down_revision='0018_dal_reply_contexts'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("\nCREATE TABLE dal_notifications (\n\tnotification_id TEXT NOT NULL, \n\tdevice_id TEXT NOT NULL, \n\tevent_id TEXT NOT NULL, \n\tstatus TEXT NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tnext_attempt_at TEXT NOT NULL, \n\tCONSTRAINT pk_dal_notifications PRIMARY KEY (notification_id), \n\tCONSTRAINT dal_notification_delivery UNIQUE (device_id, event_id), \n\tCONSTRAINT ck_dal_notifications_dal_notification_state CHECK (status IN ('pending','sending','provider_accepted','undeliverable') AND attempts >= 0), \n\tCONSTRAINT fk_dal_notifications_device_id FOREIGN KEY(device_id) REFERENCES devices (device_id), \n\tCONSTRAINT fk_dal_notifications_event_id FOREIGN KEY(event_id) REFERENCES conversation_events (event_id)\n)\n\n")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM dal_notifications LIMIT 1')).first():
        raise RuntimeError('Development notification receipts must be preserved')
    op.drop_table('dal_notifications')
