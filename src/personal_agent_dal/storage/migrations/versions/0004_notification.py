"""notification batch and delivery tables plus decision notification priority

`DAL-013` (`DAL-T-BATCH-001` / `DAL-T-NOTIFY-001`). Adds the two notification
tables the batch/delivery state machines write to, the independent
`notification_priority` field on `decisions` (§3.5.2 — orthogonal to
`dock_rank`, which remains display-only), and widens `operation_events.event_type`
to the `notification.*` events those operations emit.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EVENT_TYPES = (
    "'database.migrated', 'database.retention_applied', "
    "'database.encrypted_roundtrip', 'decision.created', "
    "'notification.batch_flushed', 'notification.delivery_created', "
    "'notification.delivery_claimed', 'notification.delivery_started', "
    "'notification.delivery_succeeded', 'notification.delivery_failed', "
    "'notification.delivery_retry_scheduled', "
    "'notification.delivery_dead_lettered'"
)
_PRIOR_EVENT_TYPES = (
    "'database.migrated', 'database.retention_applied', "
    "'database.encrypted_roundtrip', 'decision.created'"
)


def _rebuild_operation_events(event_types: str) -> None:
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
    op.create_table(
        'notification_batches',
        sa.Column('batch_id', sa.Text(), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('opened_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column('flush_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column('maximum_items', sa.Integer(), nullable=False),
        sa.Column('channel', sa.Text(), nullable=False),
        sa.Column('payload_sha256', sa.Text(), nullable=False),
        sa.Column('created_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint(
            "state IN ('open', 'ready', 'closed', 'superseded', 'cancelled')",
            name='ck_notification_batches_state',
        ),
        sa.PrimaryKeyConstraint('batch_id', name='pk_notification_batches'),
        sa.UniqueConstraint(
            'batch_id', 'channel', 'payload_sha256',
            name='uq_notification_batches_batch_channel_payload',
        ),
    )
    op.create_table(
        'notification_deliveries',
        sa.Column('delivery_id', sa.Text(), nullable=False),
        sa.Column('batch_id', sa.Text(), nullable=False),
        sa.Column('batch_version', sa.Integer(), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('claim_epoch', sa.Integer(), nullable=False),
        sa.Column('attempt_id', sa.Text(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=True),
        sa.Column('provider_receipt', sa.Text(), nullable=True),
        sa.Column('payload_sha256', sa.Text(), nullable=False),
        sa.Column('created_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.Column('updated_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'delivering', 'delivered', "
            "'retry_wait', 'dead_letter', 'cancelled')",
            name='ck_notification_deliveries_state',
        ),
        sa.CheckConstraint('claim_epoch >= 1', name='ck_notification_deliveries_claim_epoch_positive'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_notification_deliveries_attempt_count_non_negative'),
        sa.PrimaryKeyConstraint('delivery_id', name='pk_notification_deliveries'),
    )
    op.create_index(
        'ix_notification_deliveries_batch_id',
        'notification_deliveries',
        ['batch_id'],
    )

    with op.batch_alter_table('decisions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('notification_priority', sa.Text(), nullable=False,
                      server_default=sa.text("'normal'"))
        )
        batch_op.create_check_constraint(
            'notification_priority',
            "notification_priority IN ('immediate', 'normal')",
        )

    _rebuild_operation_events(_EVENT_TYPES)


def downgrade() -> None:
    _rebuild_operation_events(_PRIOR_EVENT_TYPES)
    with op.batch_alter_table('decisions', schema=None) as batch_op:
        batch_op.drop_constraint('notification_priority', type_='check')
        batch_op.drop_column('notification_priority')
    op.drop_index('ix_notification_deliveries_batch_id', table_name='notification_deliveries')
    op.drop_table('notification_deliveries')
    op.drop_table('notification_batches')
