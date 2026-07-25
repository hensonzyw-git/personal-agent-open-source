"""attribute a duplicate check to the request that raised it

The `duplicate_check_id` that `write anyway` needs must reach the Agent without
travelling on the model-facing MCP result (design 5.2). The Agent reads it from
the internal control plane instead, keyed by the idempotency key it sent -- so
the check has to record that key.

Existing rows predate the column and cannot be attributed to a request after the
fact. Rather than invent a key for them, they are given the empty string, which
matches no real idempotency key and therefore can never be returned by the
control-plane lookup.

Revision ID: 0002_duplicate_check_idempotency_key
Revises: 0001_finance_baseline
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_duplicate_check_idempotency_key"
down_revision: str | None = "0001_finance_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("duplicate_checks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "idempotency_key",
                sa.Text(),
                nullable=False,
                server_default="",
            )
        )
        batch_op.create_index(
            "ix_duplicate_checks_idempotency_key",
            ["idempotency_key"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("duplicate_checks", schema=None) as batch_op:
        batch_op.drop_index("ix_duplicate_checks_idempotency_key")
        batch_op.drop_column("idempotency_key")
