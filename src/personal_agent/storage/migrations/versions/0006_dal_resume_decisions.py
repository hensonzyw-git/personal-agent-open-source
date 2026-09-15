"""Durable PA resume clicks and delivery; never backfill approvals."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import UtcTimestamp
revision="0006_dal_resume_decisions"
down_revision="0005_finance_safe_retry"
branch_labels=None
depends_on=None

def upgrade():
    op.create_table("dal_resume_proposals",
        sa.Column("proposal_id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False, unique=True),
        sa.Column("device_id", sa.Text(), sa.ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("expires_at", UtcTimestamp(), nullable=False),
    )
    op.create_table("dal_resume_decisions",
        sa.Column("decision_id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False, unique=True),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), sa.ForeignKey("dal_resume_proposals.proposal_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("device_id", sa.Text(), sa.ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("key_thumbprint", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("claims", sa.Text(), nullable=False),
        sa.Column("expires_at", UtcTimestamp(), nullable=False),
    )
    op.create_table("dal_resume_deliveries",
        sa.Column("decision_id", sa.Text(), sa.ForeignKey("dal_resume_decisions.decision_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )

def downgrade():
    bind=op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM dal_resume_proposals LIMIT 1")).first():
        raise RuntimeError("resume decisions exist; restore coordinated backup")
    if bind.execute(sa.text("SELECT 1 FROM dal_resume_decisions LIMIT 1")).first():
        raise RuntimeError("resume decisions exist; restore coordinated backup")
    if bind.execute(sa.text("SELECT 1 FROM dal_resume_deliveries LIMIT 1")).first():
        raise RuntimeError("resume decisions exist; restore coordinated backup")
    op.drop_table("dal_resume_deliveries")
    op.drop_table("dal_resume_decisions")
    op.drop_table("dal_resume_proposals")
