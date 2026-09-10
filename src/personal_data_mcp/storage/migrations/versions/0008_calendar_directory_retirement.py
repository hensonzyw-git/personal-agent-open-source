"""Calendar directory retirement and version

2026-09-10 step-6 review: the directory only ever accumulated. The phone
uploads its *whole* directory with every batch (design 2.1), but a calendar the
newest statement stopped naming stayed in `calendar_directory` forever — and
the row still answered the create-routing lookup. A re-installed phone reports
a fresh EventKit identifier for every calendar, so the stale and the current
row shared a title and turned a name that is unique on the phone into
「两个以上的日历都叫…」, a clarification with no useful answer.

`retired_at` marks a row a newer whole-directory statement dropped. The row is
kept rather than deleted: events already mirrored from that calendar still
point at its identifier, and the display read names them by it. Retiring
removes the row from *choice*, not from the record.

`snapshot_ts` is the version of the statement that last asserted the row, so
two directory statements can be ordered the way their events already are
(ingest F4/H2): an older statement may not rename a calendar, re-add one a
newer statement retired, or retire one it names.

Both columns are nullable. Rows written before this migration carry no version
testimony and are not retired, so null `snapshot_ts` reads as *oldest* — the
first statement after the upgrade may freely correct them.

Revision ID: 0008_calendar_directory_retirement
Revises: 0007_calendar_recurring_identity
Create Date: 2026-09-10
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0008_calendar_directory_retirement"
down_revision: str | None = "0007_calendar_recurring_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("calendar_directory", schema=None) as batch_op:
        batch_op.add_column(sa.Column("snapshot_ts", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "retired_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("calendar_directory", schema=None) as batch_op:
        batch_op.drop_column("retired_at")
        batch_op.drop_column("snapshot_ts")
