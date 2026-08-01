"""witness the audit tail outside the append-only event table

Revision ID: 0003_audit_chain_anchor
Revises: 0002_duplicate_check_idempotency_key
Create Date: 2026-08-01
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = "0003_audit_chain_anchor"
down_revision: str | None = "0002_duplicate_check_idempotency_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_chain_anchor",
        sa.Column("anchor_id", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("tail_hash", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            personal_agent_core.sqlite.UtcTimestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "anchor_id = 1", name=op.f("ck_audit_chain_anchor_singleton")
        ),
        sa.CheckConstraint(
            "event_count >= 1",
            name=op.f("ck_audit_chain_anchor_event_count_positive"),
        ),
        sa.PrimaryKeyConstraint("anchor_id", name=op.f("pk_audit_chain_anchor")),
    )
    # Establish the current valid prefix as the migration baseline. Future
    # append transactions update this witness atomically, so deleting a tail
    # event without also rewriting the witness becomes detectable.
    op.execute(
        sa.text(
            """
            INSERT INTO audit_chain_anchor
                (anchor_id, event_count, tail_hash, updated_at)
            SELECT 1,
                   (SELECT COUNT(*) FROM audit_events),
                   event_hash,
                   created_at
              FROM audit_events
             ORDER BY sequence DESC
             LIMIT 1
            """
        )
    )


def downgrade() -> None:
    op.drop_table("audit_chain_anchor")
