"""record deterministic Session boundaries for idle and completed tools.

Revision ID: 0008_session_automatic_boundaries
Revises: 0007_daily_review_timeline_event
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0008_session_automatic_boundaries"
down_revision: str | None = "0007_daily_review_timeline_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    "boundary_reason IS NULL OR boundary_reason IN "
    "('explicit_reset', 'explicit_resume', 'explicit_correction', "
    "'task_boundary', 'idle_and_unrelated', 'previous_closed')"
)
_NEW = (
    "boundary_reason IS NULL OR boundary_reason IN "
    "('explicit_reset', 'explicit_resume', 'explicit_correction', "
    "'task_boundary', 'idle_and_unrelated', 'idle_timeout', "
    "'completed_tool_unrelated', 'previous_closed')"
)


def upgrade() -> None:
    with op.batch_alter_table("context_sessions", schema=None) as batch_op:
        batch_op.drop_constraint("boundary_reason", type_="check")
        batch_op.create_check_constraint("boundary_reason", _NEW)


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.execute(sa.text(
        "SELECT 1 FROM context_sessions WHERE boundary_reason IN "
        "('idle_timeout', 'completed_tool_unrelated') LIMIT 1"
    )).scalar_one_or_none()
    if used is not None:
        raise RuntimeError("cannot downgrade Session boundary reasons in use")
    with op.batch_alter_table("context_sessions", schema=None) as batch_op:
        batch_op.drop_constraint("boundary_reason", type_="check")
        batch_op.create_check_constraint("boundary_reason", _OLD)
