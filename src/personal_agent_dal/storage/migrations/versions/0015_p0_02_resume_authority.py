"""Renewed approval authority. No backfilled execution or isolation authority."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import UtcTimestamp

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('worker_enrollments', sa.Column('registration_epoch', sa.Integer(), nullable=True))
    op.create_table('supervisor_launch_manifests',
        sa.Column('attempt_id', sa.Text(), sa.ForeignKey("provider_attempts.attempt_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('sha256', sa.Text(), nullable=False, unique=True),
        sa.Column('recorded_at', UtcTimestamp(), nullable=False),
    )
    op.create_table("workflow_profile_revisions",
        sa.Column("revision_id", sa.Text(), primary_key=True),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False, unique=True),
        sa.CheckConstraint("profile IN ('A', 'B')", name='profile'),
        sa.CheckConstraint('revision >= 1', name='revision'),
        sa.UniqueConstraint("profile", "revision", name="uq_workflow_profile_revision"),
    )
    op.create_table("execution_snapshots",
        sa.Column("sha256", sa.Text(), primary_key=True),
        sa.Column("revision_id", sa.Text(), sa.ForeignKey("workflow_profile_revisions.revision_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
    )
    op.create_table("workflow_selections",
        sa.Column("selection_id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False, unique=True),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("feature_id", sa.Text(), sa.ForeignKey("features.feature_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column("gate_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_sha256", sa.Text(), sa.ForeignKey("execution_snapshots.sha256", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("created_at", UtcTimestamp(), nullable=False),
        sa.CheckConstraint('feature_version >= 1 AND gate_version >= 1', name='versions'),
    )
    op.create_table("resume_proposals",
        sa.Column("source_snapshot_sha256", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False, unique=True),
        sa.Column("binding", sa.Text(), nullable=False),
        sa.Column("binding_sha256", sa.Text(), nullable=False),
        sa.Column("expires_at", UtcTimestamp(), nullable=False),
    )
    op.create_table("resume_approval_bindings",
        sa.Column("approval_id", sa.Text(), sa.ForeignKey("approvals.approval_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("proposal_id", sa.Text(), sa.ForeignKey("resume_proposals.proposal_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("decision_id", sa.Text(), nullable=False, unique=True),
        sa.Column("jti", sa.Text(), nullable=False, unique=True),
        sa.Column("claims", sa.Text(), nullable=False),
        sa.Column("claims_sha256", sa.Text(), nullable=False),
        sa.Column("key_thumbprint", sa.Text(), nullable=False),
    )
    op.create_table("resume_revocations",
        sa.Column("decision_id", sa.Text(), primary_key=True),
        sa.Column("recorded_at", UtcTimestamp(), nullable=False),
    )
    op.create_table("resume_receipts",
        sa.Column("receipt_id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False, unique=True),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("recorded_at", UtcTimestamp(), nullable=False),
    )
    op.create_table("replacement_budgets",
        sa.Column("old_attempt_id", sa.Text(), sa.ForeignKey("provider_attempts.attempt_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("new_attempt_id", sa.Text(), sa.ForeignKey("provider_attempts.attempt_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("old_action_id", sa.Text(), sa.ForeignKey("workflow_actions.action_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("new_action_id", sa.Text(), sa.ForeignKey("workflow_actions.action_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("approval_id", sa.Text(), sa.ForeignKey("approvals.approval_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("receipt_id", sa.Text(), sa.ForeignKey("resume_receipts.receipt_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.CheckConstraint('old_attempt_id <> new_attempt_id', name='different_attempt'),
    )
    op.create_table("dispatch_intents",
        sa.Column("intent_id", sa.Text(), primary_key=True),
        sa.Column("attempt_id", sa.Text(), sa.ForeignKey("provider_attempts.attempt_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("receipt_id", sa.Text(), sa.ForeignKey("resume_receipts.receipt_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("selection_id", sa.Text(), sa.ForeignKey("workflow_selections.selection_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", UtcTimestamp(), nullable=False),
        sa.CheckConstraint("status = 'awaiting_episode'", name='status'),
    )
    op.create_table("supervisor_identities",
        sa.Column("kid", sa.Text(), primary_key=True),
        sa.Column("worker_id", sa.Text(), sa.ForeignKey("worker_enrollments.worker_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("machine_id", sa.Text(), nullable=False),
        sa.Column("registration_epoch", sa.Integer(), nullable=False),
        sa.Column("boot_id", sa.Text(), nullable=False),
        sa.Column("supervisor_epoch", sa.Integer(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("revoked_at", UtcTimestamp(), nullable=True),
        sa.CheckConstraint('registration_epoch >= 1 AND supervisor_epoch >= 1', name='epochs'),
    )
    op.create_table("isolation_challenges",
        sa.Column("challenge_id", sa.Text(), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("expires_at", UtcTimestamp(), nullable=False),
        sa.Column("consumed_at", UtcTimestamp(), nullable=True),
    )
    op.create_table("isolation_evidence",
        sa.Column("isolation_id", sa.Text(), primary_key=True),
        sa.Column("challenge_id", sa.Text(), sa.ForeignKey("isolation_challenges.challenge_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("binding", sa.Text(), nullable=False),
        sa.Column("binding_sha256", sa.Text(), nullable=False, unique=True),
        sa.Column("expires_at", UtcTimestamp(), nullable=False),
        sa.Column("reserved_by", sa.Text(), sa.ForeignKey("provider_attempts.attempt_id", ondelete="RESTRICT"), nullable=True),
    )

def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM supervisor_launch_manifests LIMIT 1")).first():
        raise RuntimeError("launch authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM worker_enrollments WHERE registration_epoch IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("registration authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM workflow_profile_revisions LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM execution_snapshots LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM workflow_selections LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM resume_proposals LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM resume_approval_bindings LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM resume_revocations LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM resume_receipts LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM replacement_budgets LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM dispatch_intents LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM supervisor_identities LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM isolation_challenges LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    if bind.execute(sa.text("SELECT 1 FROM isolation_evidence LIMIT 1")).first():
        raise RuntimeError("resume authority exists; restore a coordinated backup instead")
    op.drop_table("isolation_evidence")
    op.drop_table("isolation_challenges")
    op.drop_table("supervisor_identities")
    op.drop_table("dispatch_intents")
    op.drop_table("replacement_budgets")
    op.drop_table("resume_receipts")
    op.drop_table("resume_revocations")
    op.drop_table("resume_approval_bindings")
    op.drop_table("resume_proposals")
    op.drop_table("workflow_selections")
    op.drop_table("execution_snapshots")
    op.drop_table("workflow_profile_revisions")
    op.drop_table("supervisor_launch_manifests")
    op.drop_column("worker_enrollments", "registration_epoch")
