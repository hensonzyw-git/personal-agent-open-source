"""remember which Timeline event is this review card's frozen snapshot

The daily review moves from a separate page to a card in the Timeline (design
`1j`). The card's values are read once, when the nightly job builds (or reopens)
the card, and are sealed into a `daily_review` Timeline event; scrolling back
renders that frozen snapshot and never re-reads Feishu.

`timeline_event_id` is the pointer from the review row to its latest sealed
snapshot. It exists for two reasons that a scan over `conversation_events`
cannot serve:

- **idempotency across a crash.** The review row and its items are committed in
  one transaction, the Feishu value read and the event append in later ones. A
  crash between them leaves a review row with no event -- and with the old
  separate page gone, a review with no Timeline event is a review that no longer
  exists anywhere. The next run can see `timeline_event_id IS NULL` and repair
  it instead of returning `ALREADY_EXISTS` and walking away.
- **which snapshot is current.** A late-verified write reopens the card and a
  *new* snapshot event is appended. The row always names the newest one; the
  older events stay sealed in the archive and the client resolves the card to
  the latest by `review_id`.

Nullable with no backfill: a review built before this migration has no Timeline
event, and NULL is exactly that fact -- it is what makes the repair path above
able to recognise it.

Revision ID: 0007_daily_review_timeline_event
Revises: 0006_receipt_record_fields
Create Date: 2026-08-18
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0007_daily_review_timeline_event"
down_revision: str | None = "0006_receipt_record_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("daily_reviews", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("timeline_event_id", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("daily_reviews", schema=None) as batch_op:
        batch_op.drop_column("timeline_event_id")
