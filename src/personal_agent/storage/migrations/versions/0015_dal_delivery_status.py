"""Preserve ambiguity after attempted delivery; constrain durable status truth."""
from alembic import op
import sqlalchemy as sa
revision='0015_dal_delivery_status'
down_revision='0014_dal_resume_decisions'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("UPDATE dal_resume_deliveries SET status='delivery_unknown' WHERE status='expired' AND attempts > 0")
    with op.batch_alter_table('dal_resume_deliveries') as batch:
        batch.create_check_constraint('delivery_status',"status IN ('queued','accepted','rejected','expired','delivery_unknown')")
        batch.create_check_constraint('delivery_attempts','attempts >= 0')
        batch.create_check_constraint('expired_never_attempted',"status <> 'expired' OR attempts = 0")
        batch.create_check_constraint('unknown_attempted',"status <> 'delivery_unknown' OR attempts > 0")
        batch.create_check_constraint('delivery_evidence',"(status = 'accepted' AND approval_id IS NOT NULL AND attempts > 0) OR (status <> 'accepted' AND approval_id IS NULL)")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM dal_resume_deliveries WHERE status='delivery_unknown' LIMIT 1")).first():
        raise RuntimeError('ambiguous delivery exists; preserve records and restore coordinated backup')
    with op.batch_alter_table('dal_resume_deliveries') as batch:
        for name in ('delivery_status','delivery_attempts','expired_never_attempted','unknown_attempted','delivery_evidence'):
            batch.drop_constraint(name,type_='check')
