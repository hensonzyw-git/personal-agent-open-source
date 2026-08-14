"""decision authority and dock projection persistence

`DAL-013` (`DAL-T-DOCK-001`). The Decision Dock projection emits a
`decision.created` operation event when it persists the visible frontier. The
`operation_events.event_type` check constraint frozen in `0001` only knew the
three database operations, so this revision widens it to include the new event.
It also persists the closed Dock rank inputs on ``decisions`` and adds a
monotonic ``projection_version`` to the projection row; neither may be supplied
as unverified operation-command facts.

The revision adds the decision authority/rank columns, a monotonic card
projection version, and widens the event constraint. SQLite cannot `ALTER` a
check constraint in place, so the event table is rebuilt. That rebuild is
spelled explicitly rather than through `batch_alter_table` because the `0001`
constraint name was frozen with the convention's full `ck_operation_events_`
prefix, and batch mode re-applies the convention to that already-prefixed name
and then cannot find the constraint to drop.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EVENT_TYPES = (
    "'database.migrated', 'database.retention_applied', "
    "'database.encrypted_roundtrip', 'decision.created'"
)
_PRIOR_EVENT_TYPES = (
    "'database.migrated', 'database.retention_applied', "
    "'database.encrypted_roundtrip'"
)


def _rebuild(event_types: str) -> None:
    op.create_table(
        'operation_events_new',
        sa.Column('seq', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('operation_event_id', sa.Text(), nullable=False),
        sa.Column('event_type', sa.Text(), nullable=False),
        sa.Column('operation_id', sa.Text(), nullable=False),
        sa.Column('occurred_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"event_type IN ({event_types})",
            name='ck_operation_events_event_type',
        ),
        sa.PrimaryKeyConstraint('seq', name='pk_operation_events'),
        sa.UniqueConstraint(
            'operation_event_id', name='uq_operation_events_operation_event_id'
        ),
    )
    op.execute(
        "INSERT INTO operation_events_new "
        "(seq, operation_event_id, event_type, operation_id, occurred_at, detail) "
        "SELECT seq, operation_event_id, event_type, operation_id, occurred_at, detail "
        "FROM operation_events"
    )
    op.drop_table('operation_events')
    op.rename_table('operation_events_new', 'operation_events')


def upgrade() -> None:
    with op.batch_alter_table('decisions', recreate='always') as batch_op:
        batch_op.add_column(
            sa.Column('root_id', sa.Text(), nullable=False,
                      server_default=sa.text("'legacy-root'"))
        )
        batch_op.add_column(
            sa.Column('safety_or_irreversible', sa.Boolean(), nullable=False,
                      server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column('blocking_scope', sa.Text(), nullable=False,
                      server_default=sa.text("'none'"))
        )
        batch_op.add_column(
            sa.Column('depends_on_json', sa.Text(), nullable=False,
                      server_default=sa.text("'[]'"))
        )
        batch_op.add_column(
            sa.Column('expires_at', personal_agent_core.sqlite.UtcTimestamp(),
                      nullable=True)
        )
        batch_op.add_column(sa.Column('superseded_by', sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            'blocking_scope',
            "blocking_scope IN ('global', 'local', 'none')",
        )
    op.execute("UPDATE decisions SET root_id = decision_id WHERE root_id = 'legacy-root'")

    with op.batch_alter_table(
        'decision_card_projections', recreate='always'
    ) as batch_op:
        batch_op.add_column(
            sa.Column('projection_version', sa.Integer(), nullable=False,
                      server_default=sa.text('1'))
        )
    _rebuild(_EVENT_TYPES)


def downgrade() -> None:
    # ``decision.created`` is a derived projection event that revision 0002
    # cannot represent.  Prune it explicitly before restoring 0002's closed
    # event vocabulary; all aggregate/audit facts remain intact.
    op.execute("DELETE FROM operation_events WHERE event_type = 'decision.created'")
    _rebuild(_PRIOR_EVENT_TYPES)
    with op.batch_alter_table(
        'decision_card_projections', recreate='always'
    ) as batch_op:
        batch_op.drop_column('projection_version')
    with op.batch_alter_table('decisions', recreate='always') as batch_op:
        # Pass the convention token, not the already-expanded reflected name.
        # Otherwise Alembic applies the convention twice and looks for
        # ``ck_decisions_ck_decisions_blocking_scope``.
        batch_op.drop_constraint('blocking_scope', type_='check')
        batch_op.drop_column('superseded_by')
        batch_op.drop_column('expires_at')
        batch_op.drop_column('depends_on_json')
        batch_op.drop_column('blocking_scope')
        batch_op.drop_column('safety_or_irreversible')
        batch_op.drop_column('root_id')
