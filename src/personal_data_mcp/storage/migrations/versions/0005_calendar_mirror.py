"""Apple-calendar mirror for device-reported events

The iPhone owns the calendar (EventKit sandbox); this table is what the phone
last reported through `calendar.ingest_events`. Text fields (title, notes,
location) are sealed envelopes like every other business content in this
database — the table travels through the restic backup chain, so a plaintext
personal note would leak off-machine. Timestamps and identifiers stay
plaintext and indexed because the window filter and sort are the query's whole
shape.

`(calendar_identifier, event_identifier)` is the composite identity: the pair
is what an upsert arbitrates on. `row_key` is a surrogate the sealing AAD can
name, since two columns cannot form one AAD string.

`calendar_device_sync` holds one watermark per device: the newest
`snapshot_as_of` that arrived with `window_complete=true`. Snapshot versioning
(review 2026-09-08, R2/R3/R10) replaced per-event `last_modified` as the
arbiter — EventKit exposes no per-event modification time, so the snapshot
instant is the only honest version.

Revision ID: 0005_calendar_mirror
Revises: 0004_manual_review_resolution
Create Date: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0005_calendar_mirror"
down_revision: str | None = "0004_manual_review_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("row_key", sa.Text(), nullable=False),
        sa.Column("calendar_identifier", sa.Text(), nullable=False),
        sa.Column("event_identifier", sa.Text(), nullable=False),
        sa.Column("start_ts", sa.Integer(), nullable=False),
        sa.Column("end_ts", sa.Integer(), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False),
        sa.Column("title", personal_agent_core.sqlite.EncryptedEnvelope(), nullable=True),
        sa.Column("notes", personal_agent_core.sqlite.EncryptedEnvelope(), nullable=True),
        sa.Column(
            "location", personal_agent_core.sqlite.EncryptedEnvelope(), nullable=True
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("last_modified_ts", sa.Integer(), nullable=False),
        sa.Column("snapshot_ts", sa.Integer(), nullable=False),
        sa.Column("synced_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column("created_by_agent", sa.Boolean(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.CheckConstraint("start_ts <= end_ts", name="start_before_or_equal_end"),
        sa.PrimaryKeyConstraint(
            "calendar_identifier", "event_identifier", name="pk_calendar_events"
        ),
        sa.UniqueConstraint("row_key", name="uq_calendar_events_row_key"),
    )
    with op.batch_alter_table("calendar_events", schema=None) as batch_op:
        batch_op.create_index("ix_calendar_events_start_ts", ["start_ts"])
        batch_op.create_index("ix_calendar_events_end_ts", ["end_ts"])
    # Per-device watermark of the newest *completed* window snapshot. It is
    # the version sweeps and freshness checks arbitrate on: without it, a
    # last batch would tombstone from its own membership alone (deleting
    # earlier batches' events) and a partial upload would read as fresh.
    op.create_table(
        "calendar_device_sync",
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("watermark_ts", sa.Integer(), nullable=False),
        sa.Column("updated_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.PrimaryKeyConstraint("device_id", name="pk_calendar_device_sync"),
    )


def downgrade() -> None:
    op.drop_table("calendar_device_sync")
    with op.batch_alter_table("calendar_events", schema=None) as batch_op:
        batch_op.drop_index("ix_calendar_events_end_ts")
        batch_op.drop_index("ix_calendar_events_start_ts")
    op.drop_table("calendar_events")
