"""Join the independently developed Calendar and media migration histories.

No existing revision is renamed or reparented: a database at either prior head
must apply the missing sibling branch before recording this common head.
Downgrading this merge marker changes no tables; each branch retains its own
guarded downgrade.
"""

revision = "0012_calendar_media_merge"
down_revision = ("0011_action_plan", "0010_media_upload_idempotency")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
