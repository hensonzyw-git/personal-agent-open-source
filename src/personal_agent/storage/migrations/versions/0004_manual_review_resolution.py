"""record what a human concluded about a `needs_manual_review` operation

DEV-040 §13.2. `needs_manual_review` is terminal and, until now, had no exit of
any kind: the daily-review push run on 2026-08-04 surfaced it as a dead end, and
the breakpoint drill the same day stranded four more rows there.

The exit added here is deliberately **not** a state transition. Terminal states
have no outgoing edges (see `operation_state`'s module docstring), and that rule
is what lets recovery skip a terminal row without re-deriving it. What was
missing is not another accounting outcome but a record that a person looked:
so this mirrors `cancel_requested` / `client_detached`, which are flags for the
same reason -- the accounting outcome is the `state` and its evidence, and a
human saying "I checked the ledger" is neither.

Three constraints keep the pair honest:

- the resolution and its timestamp are one fact, so neither half may exist alone;
- only an operation that actually ended at `needs_manual_review` may carry one,
  so this can never become a general-purpose annotation on a successful write;
- the value is a closed set, so a future caller cannot invent a third meaning
  that readers would have to guess at.

Revision ID: 0004_manual_review_resolution
Revises: 0003_cap001_timeline
Create Date: 2026-08-04
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0004_manual_review_resolution"
down_revision: str | None = "0003_cap001_timeline"
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
    # history of an operation that was never reviewed.
    with op.batch_alter_table("operations", schema=None) as batch_op:
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
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.drop_constraint("manual_resolution_only_for_review", type_="check")
        batch_op.drop_constraint(
            "manual_resolution_pairs_with_its_time", type_="check"
        )
        batch_op.drop_constraint("manual_resolution", type_="check")
        batch_op.drop_column("manual_resolved_at")
        batch_op.drop_column("manual_resolution")
