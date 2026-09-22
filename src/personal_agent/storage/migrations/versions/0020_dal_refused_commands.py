"""Persist signed refusal receipts instead of endlessly retrying stale decisions."""
from alembic import op
import sqlalchemy as sa
revision='0020_dal_refused_commands'
down_revision='0019_dal_notifications'
branch_labels=None
depends_on=None


def _constraints(include_refused):
    states="'accepted','refused'" if include_refused else "'accepted'"
    with op.batch_alter_table('dal_timeline_commands') as batch:
        batch.drop_constraint(op.f('ck_dal_timeline_commands_timeline_command_status'),type_='check')
        batch.drop_constraint(op.f('ck_dal_timeline_commands_timeline_receipt'),type_='check')
        batch.create_check_constraint('timeline_command_status',"status IN ('queued','delivery_unknown','cancelled',"+states+")")
        batch.create_check_constraint('timeline_receipt',"(status IN ("+states+") AND sealed_receipt IS NOT NULL AND attempts > 0) OR (status NOT IN ("+states+") AND sealed_receipt IS NULL)")


def upgrade():_constraints(True)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM dal_timeline_commands WHERE status='refused' LIMIT 1")).first():
        raise RuntimeError('Refused command receipts must be preserved')
    _constraints(False)
