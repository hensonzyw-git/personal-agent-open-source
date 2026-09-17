"""effect confirm receipts — the crash-window discriminator (R09-B R3-1)

Adds `effect_confirm_receipts`: the executor's persisted evidence that a
`dispatch_started` effect is a *confirmed* park — the outward write returned a
closed success — rather than an open crash window (dispatch marker committed,
fate unproven).

Why this table exists. The dispatch marker stamps `claim_expires_at`, and the
expiry sweep (`_recover_expired_dispatches`) moved every expired marker to
`unknown` — including markers whose outward write had already returned a
confirmed read-back. A normal successful push therefore degraded to
`reconciliation_required` fifteen minutes later (round-3 review finding R3-1,
2026-09-04). The registry is untouched: a confirmed write still parks in
`dispatch_started` awaiting its owner root (axiom 6 — the engine never claims
success on the adapter's word alone), so there is no lifecycle edge to hook a
confirm onto. The discriminator is operator-owned persistence instead, the
exact precedent of `effect_dispatch_targets` (0008): written by the dispatch
composition after a confirmed read-back, read by the recovery sweep.

Fail-closed: a crash between the confirmed read-back and this write leaves no
receipt, so the sweep recovers that effect to `unknown` and the read-only
read-back proves what landed — the pre-existing crash path, unchanged. The
sweep only honours a receipt whose target fingerprint still matches the
effect's own binding; a stale or tampered receipt suppresses nothing.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'effect_confirm_receipts',
        sa.Column('effect_id', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('target_fingerprint', sa.Text(), nullable=False),
        sa.Column('composition_key', sa.Text(), nullable=False),
        sa.Column('confirmed_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        sa.CheckConstraint(
            "action IN ('push_branch', 'create_pull_request', 'write_check_run')",
            name=op.f('ck_effect_confirm_receipts_action'),
        ),
        sa.CheckConstraint(
            # The frozen helper's shape, inline, as in 0008 — outer
            # parentheses included, so the migrated and metadata-built
            # schemas agree byte for byte (R4-3).
            "(length(target_fingerprint) = 64 "
            "AND target_fingerprint NOT GLOB '*[^0-9a-f]*')",
            name=op.f('ck_effect_confirm_receipts_fingerprint'),
        ),
        sa.CheckConstraint(
            "length(composition_key) > 0",
            name=op.f('ck_effect_confirm_receipts_composition_key'),
        ),
        sa.ForeignKeyConstraint(
            ['effect_id'], ['external_effects.effect_id'],
            name=op.f('fk_effect_confirm_receipts_effect_id'),
        ),
        sa.PrimaryKeyConstraint('effect_id', name=op.f('pk_effect_confirm_receipts')),
    )


def downgrade() -> None:
    op.drop_table('effect_confirm_receipts')
