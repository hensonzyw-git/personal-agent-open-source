"""freeze a whole turn's action list before any of it is issued

Design 4.1 (review R1-F5). One message may ask for several calendar events
("下周一10点牙医，下午3点理发"), and the model answers with one call per event.
The initial design derived each call's idempotency key from its position in the
model's response -- but the response was not written down anywhere, so if the
process died after issuing the first item, recovery would re-ask the model, and
the second item's key would then name whatever the model happened to produce
second that time. The same key would mean a different event.

So the list is frozen first: the whole turn's calls are written to `operations`
in one transaction, each already at `dispatching` with its attested arguments
retained, before any of them is authorised or parked. A retry then resumes from
the list instead of re-asking the model, and the derived key
`uuid5(message key, index)` always names the item it named before.

- `plan_key` -- the message's own idempotency key, so every item of one turn is
  findable from that turn alone.
- `plan_index` -- the item's position, which is what the derived key is built
  from and what the response projection is ordered by.

Both are nullable with no backfill: an operation that is not part of a turn with
several actions has no position, which is what NULL says. They are written
together or not at all (a key with no index is a list nobody can order; an index
with no key is a position in a list nobody can find), and the pair is unique
because `uuid5(plan_key, index)` can only ever name one operation.

Revision ID: 0011_action_plan
Revises: 0010_calendar_override
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0011_action_plan"
down_revision: str | None = "0010_calendar_override"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plan_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("plan_index", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "plan_membership_is_whole",
            "(plan_key IS NULL) = (plan_index IS NULL)"
            " AND (plan_index IS NULL OR plan_index >= 0)",
        )
        batch_op.create_unique_constraint(
            "plan_item_once", ["plan_key", "plan_index"]
        )


def downgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.drop_constraint("plan_item_once", type_="unique")
        batch_op.drop_constraint("plan_membership_is_whole", type_="check")
        batch_op.drop_column("plan_index")
        batch_op.drop_column("plan_key")
