"""intake binding + reconciler arbitration (2026-09-07 review F4/F7/F3)

Three schema pieces for the dual-branch review remediation
(`docs/项目整体评价与双分支独立审查_2026-09-07.md`):

1. `worker_jobs.intake_key` (nullable) + partial unique index. F4 (P1): the
   intake producer's find-or-create raced (lookup and insert in separate
   transactions, `new_id()` per insert), so two concurrent identical intakes
   both read "no job" and both enqueued. The intake key is
   `f"intake:{feature_id}"` — the same identity as the feature transition's
   idempotency key. A partial unique index (WHERE intake_key IS NOT NULL)
   arbitrates at the database: a racing insert loses deterministically, and
   jobs enqueued without an intake key (the operator/test seeding path) are
   untouched. Deleting a job row frees its key, so the interrupted-first-run
   recovery re-enqueue still works.

2. `feature_intake_requests`. F7 (P2): the task body previously existed only
   inside the feature-id hash — nothing could recover what the operator
   submitted, and the worker's coder prompt never saw the task text. The body
   is operator-submitted task text bounded to 8192 chars at the API, not a
   credential; the DAL package has no protected object store (only the
   `EvidenceRecord` digest+locator model), so the row carries the text itself
   plus its SHA-256 for the worker-side binding fence. Written in the same
   transaction as the job insert (F4 uniqueness and F7 persistence land
   atomically).

3. Partial unique index on `external_effects(owner_aggregate_id)` WHERE the
   row holds a live reconciler claim. F3 (P1): the SINGLE_RECONCILER_CLAIM
   guard's `active_claim_count` fact was computed outside the retriable
   transaction, so two concurrent reconciliation starts for two different
   unknown effects of the same owner both read zero live claims and both
   committed. The index is the arbiter at the write site: the loser's UPDATE
   violates the partial unique constraint inside `run_write_transaction`
   (IntegrityError is provably never retried — `is_snapshot_conflict` matches
   only OperationalError). The STILL-UNKNOWN state change vacates the index
   slot automatically (the predicate requires state='reconciling'), so there
   is no release path to forget. Residual gap, stated honestly: the guard's
   enumeration also forbids a feature-owned claim concurrent with a
   recovery-case-owned claim of the same feature; those carry different
   `owner_aggregate_id` values and are not caught by this index.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

import personal_agent_core.sqlite


revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('worker_jobs', sa.Column('intake_key', sa.Text(), nullable=True))
    op.create_index(
        'uq_worker_jobs_intake_key',
        'worker_jobs',
        ['intake_key'],
        unique=True,
        sqlite_where=sa.text('intake_key IS NOT NULL'),
    )
    # Backfill (round-2 review finding 3): jobs enqueued by the pre-0010
    # intake producer carry no intake_key, so a post-migration replay of the
    # same intake found "no job by key" and enqueued a second one for the
    # same feature. The identity of a pre-0010 intake job is derivable — the
    # producer derived it from the feature — so each feature's OLDEST job is
    # stamped with f"intake:{feature_id}"; later jobs for the same feature
    # (a multi-phase rerun, not an intake replay) keep NULL and remain
    # outside the intake idempotency set, preserving the one-intake-episode
    # semantics. No task body can be recovered for these rows (the old world
    # never persisted one); the body table stays empty for them, and a
    # replay returns the backfilled job without rewriting history.
    op.execute(
        "UPDATE worker_jobs SET intake_key = 'intake:' || feature_id "
        "WHERE intake_key IS NULL AND job_id = ("
        "    SELECT j2.job_id FROM worker_jobs j2"
        "    WHERE j2.feature_id = worker_jobs.feature_id"
        "    ORDER BY j2.created_at ASC, j2.job_id ASC LIMIT 1)"
    )

    op.create_table(
        'feature_intake_requests',
        sa.Column('intake_key', sa.Text(), nullable=False),
        sa.Column('feature_id', sa.Text(), nullable=False),
        sa.Column('task_description', sa.Text(), nullable=False),
        sa.Column('task_description_sha256', sa.Text(), nullable=False),
        sa.Column('toolchain_ref', sa.Text(), nullable=False),
        sa.Column('recorded_at', personal_agent_core.sqlite.UtcTimestamp(), nullable=False),
        # CHECK names follow the ORM naming convention exactly
        # (ck_<table>_<bare name>) so the migrated and metadata-built schemas
        # agree byte for byte — test_db_contract's parity rule. op.f() would
        # double-prefix and land the table on the legacy exemption list.
        sa.CheckConstraint(
            "length(task_description) > 0",
            name='task_description',
        ),
        sa.CheckConstraint(
            "(length(task_description_sha256) = 64 "
            "AND task_description_sha256 NOT GLOB '*[^0-9a-f]*')",
            name='task_description_sha256_hex',
        ),
        sa.ForeignKeyConstraint(
            ['feature_id'], ['features.feature_id'],
            name='fk_feature_intake_requests_feature_id_features',
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('intake_key', name='pk_feature_intake_requests'),
    )

    op.create_index(
        'uq_external_effects_one_reconciler_per_owner',
        'external_effects',
        ['owner_aggregate_id'],
        unique=True,
        sqlite_where=sa.text(
            "state = 'reconciling' AND executor_id = 'reconciler'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_external_effects_one_reconciler_per_owner',
        table_name='external_effects',
    )
    op.drop_table('feature_intake_requests')
    op.drop_index('uq_worker_jobs_intake_key', table_name='worker_jobs')
    op.drop_column('worker_jobs', 'intake_key')
