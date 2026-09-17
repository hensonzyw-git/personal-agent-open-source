"""effect dispatch targets — the executor's protected payload record (R09-B F5)

Adds `effect_dispatch_targets`: the durable per-effect target record the
dispatch executor derives its write from. The `external_effects` row freezes
the *binding* (owner, scope key, remote key, target fingerprint) but not the
*target body*; without a persisted payload a restarted composition would have
to trust the operator's request for the very fields the intent was supposed to
have frozen (whole-track review finding 5, 2026-09-04). One row per effect,
written in the same transaction that records the intent, keyed by
`effect_id` so a rearm's new attempt replaces the row for the same effect.

No business authority is added here: the frozen transition registry is
untouched, and the executor only ever reads this table — the lifecycle edges
still run through the engine's external_effect specs.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'effect_dispatch_targets',
        sa.Column('effect_id', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('target_fingerprint', sa.Text(), nullable=False),
        sa.Column('recorded_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint(
            "action IN ('push_branch', 'create_pull_request', 'write_check_run')",
            name=op.f('ck_effect_dispatch_targets_action'),
        ),
        sa.CheckConstraint(
            # The frozen helper's shape, inline: an explicit GLOB, not a bare
            # length test — a 64-character non-hex string would satisfy the
            # column the intent fingerprint binds to. The outer parentheses
            # are the helper's exact rendering, so the migrated and
            # metadata-built schemas agree byte for byte (R4-3).
            "(length(target_fingerprint) = 64 "
            "AND target_fingerprint NOT GLOB '*[^0-9a-f]*')",
            name=op.f('ck_effect_dispatch_targets_fingerprint'),
        ),
        sa.ForeignKeyConstraint(
            ['effect_id'], ['external_effects.effect_id'],
            name=op.f('fk_effect_dispatch_targets_effect_id'),
        ),
        sa.PrimaryKeyConstraint('effect_id', name=op.f('pk_effect_dispatch_targets')),
    )


def downgrade() -> None:
    op.drop_table('effect_dispatch_targets')
