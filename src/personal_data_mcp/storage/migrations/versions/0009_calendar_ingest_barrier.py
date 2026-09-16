"""Calendar ingest barrier: the ratchet row and the per-device rebuild state

Design 14.2's barrier, first half. The mirror-rebuild story needs somewhere to
keep three facts, and they do not live in the same place, because they do not
have the same lifetime.

`calendar_ingest_policy` is the *channel's* policy: one row, and the migration
inserts it. It holds the two switches the ingest gate reads before any write --
`min_ingest_protocol`, the lowest client protocol version still accepted, and
`ingest_mode`, the maintenance switch the rollback runbook throws before it
resets the mirror.

It is a table of its own rather than a column on `calendar_device_sync`
because a rebuild **empties** that table. A ratchet that disappears when the
rows it governed are wiped is not a ratchet, and `min_ingest_protocol` must be
true for every device at once, including the device that has not enrolled yet.
The row therefore never goes away: it is written here, and the reader raises if
it is missing rather than defaulting, because defaulting is the one way this
can fail open -- and failing open here means an old App's late packets write
into a mirror that was rebuilt without them (design 14.2, R6-F21/R7-F22).

`rebuild_pending` and `rebuild_instant` go on `calendar_device_sync` instead,
one row per device, because they describe *that device's* mirror. Neither is a
refusal predicate on its own: `rebuild_pending` is presentation state (the
「正在重建」 line the query renders) whose redundant v1 refusal is carried by
the ratchet once it clears, and `rebuild_instant` is warning material only --
a clock with zero skew lets a pre-rebuild window through any tolerance test,
so time is never allowed to decide (design 14.2, R6-F20).

Both downgrade paths are losses, and deliberately so. Dropping the policy
table drops the ratchet with it, so a version that resumes after descending to
0008 has no way to refuse a v1 upload; and the design's only supported rollback
target is a barrier-bearing version (R6-F21), which is what makes "descend and
keep refusing" a thing this schema never has to support.

Revision ID: 0009_calendar_ingest_barrier
Revises: 0008_calendar_directory_retirement
Create Date: 2026-09-11
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0009_calendar_ingest_barrier"
down_revision: str | None = "0008_calendar_directory_retirement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_ingest_policy",
        sa.Column("policy_id", sa.Integer(), nullable=False),
        sa.Column(
            "min_ingest_protocol",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "ingest_mode",
            sa.Text(),
            nullable=False,
            server_default="normal",
        ),
        sa.Column(
            "updated_at",
            personal_agent_core.sqlite.UtcTimestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "policy_id = 1", name=op.f("ck_calendar_ingest_policy_singleton")
        ),
        sa.CheckConstraint(
            "min_ingest_protocol >= 1",
            name=op.f("ck_calendar_ingest_policy_min_ingest_protocol_positive"),
        ),
        sa.CheckConstraint(
            "ingest_mode IN ('normal', 'maintenance')",
            name=op.f("ck_calendar_ingest_policy_ingest_mode_known"),
        ),
        sa.PrimaryKeyConstraint(
            "policy_id", name=op.f("pk_calendar_ingest_policy")
        ),
    )
    # The row is part of the schema, not of the deployment: a fresh database
    # gets it here, and no code path creates it, so `read_policy` can treat
    # "no row" as an implementation bug and raise. A database restored from an
    # older snapshot is migrated up to this revision like any other, so the
    # only way to be without the row is for something to have deleted it --
    # which is exactly the case that must not read as "v1 is fine again".
    op.get_bind().execute(
        sa.text(
            "INSERT INTO calendar_ingest_policy "
            "(policy_id, min_ingest_protocol, ingest_mode, updated_at) "
            "VALUES (1, 1, 'normal', :updated_at)"
        ),
        {
            "updated_at": personal_agent_core.sqlite.UtcTimestamp().process_bind_param(
                datetime.now(tz=timezone.utc), None
            )
        },
    )

    with op.batch_alter_table("calendar_device_sync", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "rebuild_pending", sa.Boolean(), nullable=False, server_default="0"
            )
        )
        batch_op.add_column(
            sa.Column("rebuild_instant", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("calendar_device_sync", schema=None) as batch_op:
        batch_op.drop_column("rebuild_instant")
        batch_op.drop_column("rebuild_pending")
    op.drop_table("calendar_ingest_policy")
