"""scope review items by table, and constrain the notification outbox

`DEV-028`. Two tables change here, and the daily-review one is the easier to
miss, so it is stated first:

- `daily_review_items` was unique on `(review_id, record_id)`. A Feishu record
  id is only unique *within its table*, so an expense and an income row that
  happened to share an id could not both appear on one day's card -- the insert
  would fail and the whole card would be lost. The pair becomes
  `(review_id, tool, record_id)`, which is the identity the review code uses;
- `notification_outbox` had an unconstrained `provider_status`, and nothing
  stopping two rows for the same review and device. A free-text status lets
  "sent" and "delivered" appear in a table whose whole point is that the
  provider accepting a notification is *not* the user having seen it, so the set
  is now closed; and without the unique pair a retry that inserted instead of
  updating would send the same card twice.

No rows exist yet -- no device is enrolled, nothing has ever been queued, and no
review card has been built outside a test -- so the upgrade needs no data
migration. The downgrade restores the narrower review-item pair and drops the
outbox constraints.

Revision ID: 0002_notification_outbox_constraints
Revises: 0001_agent_baseline
Create Date: 2026-07-26
"""

from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = "0002_notification_outbox_constraints"
down_revision: str | None = "0001_agent_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUS_CHECK = (
    "provider_status IN ('pending', 'provider_accepted', 'undeliverable')"
)


def upgrade() -> None:
    with op.batch_alter_table("daily_review_items", schema=None) as batch_op:
        batch_op.drop_constraint("review_id_record_id", type_="unique")
        batch_op.create_unique_constraint(
            "review_id_tool_record_id", ["review_id", "tool", "record_id"]
        )
    with op.batch_alter_table("notification_outbox", schema=None) as batch_op:
        # Named exactly as the declarative model names them, so a migrated
        # database and a `create_all` one carry identical constraints. The
        # check picks up the `ck_<table>_` prefix from the naming convention;
        # an explicitly named unique constraint does not, which is also how the
        # baseline spells `review_id_record_id`.
        batch_op.create_check_constraint("provider_status", _STATUS_CHECK)
        batch_op.create_unique_constraint(
            "review_id_device_id", ["review_id", "device_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("notification_outbox", schema=None) as batch_op:
        batch_op.drop_constraint("review_id_device_id", type_="unique")
        batch_op.drop_constraint("provider_status", type_="check")
    with op.batch_alter_table("daily_review_items", schema=None) as batch_op:
        batch_op.drop_constraint("review_id_tool_record_id", type_="unique")
        batch_op.create_unique_constraint(
            "review_id_record_id", ["review_id", "record_id"]
        )
