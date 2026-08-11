"""bind one safe Finance retry to one proven zero-write operation

Revision ID: 0005_finance_safe_retry
Revises: 0004_manual_review_resolution
Create Date: 2026-08-11
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0005_finance_safe_retry"
down_revision: str | None = "0004_manual_review_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing operations are not reinterpreted: NULL means no retry lineage
    # was established under this contract.
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("retry_of_operation_id", sa.Text(), nullable=True)
        )
        batch_op.create_foreign_key(
            "retry_source_operation",
            "operations",
            ["retry_of_operation_id"],
            ["operation_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "retry_source_consumed_once", ["retry_of_operation_id"]
        )
        batch_op.create_check_constraint(
            "retry_source_is_not_self",
            "retry_of_operation_id IS NULL OR "
            "retry_of_operation_id <> operation_id",
        )


def downgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.drop_constraint("retry_source_is_not_self", type_="check")
        batch_op.drop_constraint("retry_source_consumed_once", type_="unique")
        batch_op.drop_constraint("retry_source_operation", type_="foreignkey")
        batch_op.drop_column("retry_of_operation_id")
