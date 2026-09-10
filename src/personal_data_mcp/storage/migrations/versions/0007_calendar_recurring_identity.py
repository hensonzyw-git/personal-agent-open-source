"""recurring-event identity, all-day dates, over-limit flags, calendar directory

Three things the mirror could not express before (design 5.2, 6, 7, 2.1):

**Identity.** EventKit expands every occurrence of a recurring event into its
own `EKEvent`: one `eventIdentifier`, many `startDate`s. A two-column key
therefore collapses a whole series onto whichever occurrence arrived last.
The primary key becomes `(calendar_identifier, event_identifier, start_ts)`.
The rebuild is lossless -- each existing row enters under its own `start_ts`,
which was already the pair's de facto discriminator for non-recurring events.

**All-day dates.** An all-day event is a date, not an instant. Storing only
epoch seconds and converting on read is how a Tokyo all-day event renders as
the previous day in Shanghai. All-day rows gain `all_day_start_date` /
`all_day_end_date`, and the query renders them from those columns only. A
timed event gains `timezone`, its own zone, so it renders as local time with a
label instead of being folded into Shanghai.

**Honesty about the derivation.** Rows inherited from the v1 upload shape have
no dates at all; the migration derives them in Asia/Shanghai (the old display
behaviour, reproduced exactly) and sets `date_anchor_unknown=true`, because a
derived date is not evidence of the event's own local date. External-app
all-day events stay unknown for the same reason even on the v2 shape.

`timezone` is null on every all-day row, including derived ones: an all-day
event has no anchor zone to record (Henson 2026-09-10), and the row is
constrained to keep it that way.

Also here: the over-limit flags (a device that meets a note larger than the
threshold uploads it as null *and* says so), `calendar_device_sync.sync_epoch`
(the mirror-rebuild epoch the barrier version compares against, design 14.2 --
the column lands with the schema its reader will be built on), and the
`calendar_directory` table the create-routing rule looks calendars up in.

The downgrade is a schema-level inverse and is **lossy**, deliberately: the
two-column key cannot hold a recurring series, so shadowed occurrences are
dropped rather than silently merged into a row that never existed. It is a
development convenience only -- production rollback keeps the 0007 schema and
resets the mirror's data through the barrier version, precisely because a
row-by-row revert cannot be correct (design 14.2).

Revision ID: 0007_calendar_recurring_identity
Revises: 0006_calendar_window_coverage
Create Date: 2026-09-10
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0007_calendar_recurring_identity"
down_revision: str | None = "0006_calendar_window_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Asia/Shanghai has had no DST since 1991, so a fixed offset is exact for
#: every instant this table can hold; the derivation must reproduce the old
#: Shanghai rendering of a v1 row, which is what `+8 hours` does.
_SHANGHAI_OFFSET: str = "+8 hours"


def upgrade() -> None:
    op.create_table(
        "calendar_events_rebuilt",
        sa.Column("row_key", sa.Text(), nullable=False),
        sa.Column("calendar_identifier", sa.Text(), nullable=False),
        sa.Column("event_identifier", sa.Text(), nullable=False),
        sa.Column("start_ts", sa.Integer(), nullable=False),
        sa.Column("end_ts", sa.Integer(), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("all_day_start_date", sa.Text(), nullable=True),
        sa.Column("all_day_end_date", sa.Text(), nullable=True),
        sa.Column(
            "date_anchor_unknown",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("title", personal_agent_core.sqlite.EncryptedEnvelope(), nullable=True),
        sa.Column("notes", personal_agent_core.sqlite.EncryptedEnvelope(), nullable=True),
        sa.Column(
            "location", personal_agent_core.sqlite.EncryptedEnvelope(), nullable=True
        ),
        sa.Column(
            "title_over_limit",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "location_over_limit",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "notes_over_limit",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("last_modified_ts", sa.Integer(), nullable=False),
        sa.Column("snapshot_ts", sa.Integer(), nullable=False),
        sa.Column(
            "synced_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False
        ),
        sa.Column("created_by_agent", sa.Boolean(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.CheckConstraint("start_ts <= end_ts", name="start_before_or_equal_end"),
        sa.CheckConstraint(
            "all_day = 0 OR (all_day_start_date IS NOT NULL "
            "AND all_day_end_date IS NOT NULL AND timezone IS NULL)",
            name="all_day_rows_carry_dates_and_no_zone",
        ),
        sa.PrimaryKeyConstraint(
            "calendar_identifier",
            "event_identifier",
            "start_ts",
            name="pk_calendar_events",
        ),
        sa.UniqueConstraint("row_key", name="uq_calendar_events_row_key"),
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO calendar_events_rebuilt (
                row_key, calendar_identifier, event_identifier, start_ts, end_ts,
                all_day, timezone, all_day_start_date, all_day_end_date,
                date_anchor_unknown, title, notes, location,
                title_over_limit, location_over_limit, notes_over_limit,
                is_deleted, last_modified_ts, snapshot_ts, synced_at,
                created_by_agent, device_id
            )
            SELECT
                row_key, calendar_identifier, event_identifier, start_ts, end_ts,
                all_day,
                NULL,
                CASE WHEN all_day
                     THEN strftime('%Y-%m-%d', start_ts, 'unixepoch', '{_SHANGHAI_OFFSET}')
                END,
                CASE WHEN all_day
                     THEN strftime('%Y-%m-%d', end_ts, 'unixepoch', '{_SHANGHAI_OFFSET}')
                END,
                CASE WHEN all_day THEN 1 ELSE 0 END,
                title, notes, location,
                0, 0, 0,
                is_deleted, last_modified_ts, snapshot_ts, synced_at,
                created_by_agent, device_id
              FROM calendar_events
            """
        )
    )
    # Dropping the old table is what frees the index names; the canonical
    # indexes are then created on the renamed table so the model's metadata
    # and the migrated schema agree on every name.
    op.drop_table("calendar_events")
    op.rename_table("calendar_events_rebuilt", "calendar_events")
    op.create_index("ix_calendar_events_start_ts", "calendar_events", ["start_ts"])
    op.create_index("ix_calendar_events_end_ts", "calendar_events", ["end_ts"])

    op.create_table(
        "calendar_directory",
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("calendar_identifier", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_title", sa.Text(), nullable=True),
        sa.Column("allows_content_modifications", sa.Boolean(), nullable=False),
        sa.Column("is_subscribed", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "device_id", "calendar_identifier", name="pk_calendar_directory"
        ),
    )
    with op.batch_alter_table("calendar_device_sync", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "sync_epoch", sa.Integer(), nullable=False, server_default="1"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("calendar_device_sync", schema=None) as batch_op:
        batch_op.drop_column("sync_epoch")
    op.drop_table("calendar_directory")

    op.create_table(
        "calendar_events_rebuilt",
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
        sa.Column(
            "synced_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False
        ),
        sa.Column("created_by_agent", sa.Boolean(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.CheckConstraint("start_ts <= end_ts", name="start_before_or_equal_end"),
        sa.PrimaryKeyConstraint(
            "calendar_identifier", "event_identifier", name="pk_calendar_events"
        ),
        sa.UniqueConstraint("row_key", name="uq_calendar_events_row_key"),
    )
    # One row per (calendar, identifier): the newest assertion wins, and the
    # occurrences it shadows are dropped with it.
    op.execute(
        sa.text(
            """
            INSERT INTO calendar_events_rebuilt (
                row_key, calendar_identifier, event_identifier, start_ts, end_ts,
                all_day, title, notes, location, is_deleted, last_modified_ts,
                snapshot_ts, synced_at, created_by_agent, device_id
            )
            SELECT
                row_key, calendar_identifier, event_identifier, start_ts, end_ts,
                all_day, title, notes, location, is_deleted, last_modified_ts,
                snapshot_ts, synced_at, created_by_agent, device_id
              FROM calendar_events AS survivor
             WHERE survivor.row_key = (
                SELECT candidate.row_key
                  FROM calendar_events AS candidate
                 WHERE candidate.calendar_identifier = survivor.calendar_identifier
                   AND candidate.event_identifier = survivor.event_identifier
                 ORDER BY candidate.snapshot_ts DESC,
                          candidate.last_modified_ts DESC,
                          candidate.row_key DESC
                 LIMIT 1
             )
            """
        )
    )
    op.drop_table("calendar_events")
    op.rename_table("calendar_events_rebuilt", "calendar_events")
    op.create_index("ix_calendar_events_start_ts", "calendar_events", ["start_ts"])
    op.create_index("ix_calendar_events_end_ts", "calendar_events", ["end_ts"])
