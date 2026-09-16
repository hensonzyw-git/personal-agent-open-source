"""Calendar watermark window coverage

Second review F7 (2026-09-08): the watermark said *when* a device last
completed a snapshot but not *which window* it covered, so completing the
September window made a January window — never uploaded — read as fresh.
`calendar_device_sync` gains the completed window's bounds; freshness may
only trust the watermark for queries inside that range.

The columns are nullable: rows written before this migration must still
decode, and a null bound reads as covering nothing (honestly stale).

Revision ID: 0006_calendar_window_coverage
Revises: 0005_calendar_mirror
Create Date: 2026-09-08
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006_calendar_window_coverage"
down_revision: str | None = "0005_calendar_mirror"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("calendar_device_sync", schema=None) as batch_op:
        batch_op.add_column(sa.Column("window_start_ts", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("window_end_ts", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("calendar_device_sync", schema=None) as batch_op:
        batch_op.drop_column("window_end_ts")
        batch_op.drop_column("window_start_ts")
