"""record what a person found in the ledger for a review-parked execution

`DEV-040` gave the *Agent* operation a `manual_resolution` flag beside
`needs_manual_review`, but the Finance execution has no such field, so the
observe alert (`write_needs_manual_review`) could never clear: the execution's
state is terminal, and a human's conclusion had nowhere to be written. A
category correction that hit a read-back mismatch on 2026-08-16 stranded two
executions this way -- Henson fixed the categories in Feishu by hand, but the
system still counted them as "parked for manual review".

This mirrors the Agent side's shape and constraints:

- the value is a closed set (`confirmed_written` / `confirmed_not_written`), so
  a future caller cannot invent a third meaning;
- the resolution and its timestamp are one fact, so neither half may exist alone;
- only an execution that actually ended at `needs_manual_review` may carry one,
  so this can never become a general-purpose annotation on a success or a
  `failed_safe` row.

`state` is deliberately unchanged: a human observation is recorded beside the
state, never instead of it.

Revision ID: 0004_manual_review_resolution
Revises: 0003_audit_chain_anchor
Create Date: 2026-08-20
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0004_manual_review_resolution"
down_revision: str | None = "0003_audit_chain_anchor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RESOLUTION = (
    "manual_resolution IN ('confirmed_written', 'confirmed_not_written')"
    " OR manual_resolution IS NULL"
)
_PAIRED = "(manual_resolution IS NULL) = (manual_resolved_at IS NULL)"
_ONLY_REVIEW = "manual_resolution IS NULL OR state = 'needs_manual_review'"


def upgrade() -> None:
    # Nullable with no default: every existing row means "nobody has looked yet",
    # which is exactly what NULL says. No backfill, so this cannot rewrite the
    # history of an execution that was never reviewed.
    with op.batch_alter_table("tool_executions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("manual_resolution", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "manual_resolved_at",
                personal_agent_core.sqlite.UtcTimestamp(),
                nullable=True,
            )
        )
        batch_op.create_check_constraint("manual_resolution", _RESOLUTION)
        batch_op.create_check_constraint(
            "manual_resolution_pairs_with_its_time", _PAIRED
        )
        batch_op.create_check_constraint(
            "manual_resolution_only_for_review", _ONLY_REVIEW
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_executions", schema=None) as batch_op:
        batch_op.drop_constraint(
            "manual_resolution_only_for_review", type_="check"
        )
        batch_op.drop_constraint(
            "manual_resolution_pairs_with_its_time", type_="check"
        )
        batch_op.drop_constraint("manual_resolution", type_="check")
        batch_op.drop_column("manual_resolved_at")
        batch_op.drop_column("manual_resolution")
