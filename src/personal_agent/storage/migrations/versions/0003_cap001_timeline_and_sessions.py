"""split the user-visible Timeline from server-managed semantic Sessions

`CAP-001`, cross-cutting design §5.1 and §15. The user keeps one continuous
chat; the server gains the structure it needs to bound a model input: an
ordered canonical Timeline, automatic Sessions, and immutable context
checkpoints over closed ranges of the archive.

The data half lives in `personal_agent.storage.cap001_migration`; this revision
owns the schema and the order of operations. Two things about that order are
load-bearing:

- the preflight runs against the *old* schema, before any DDL, so a database
  that is not quiescent is left exactly as it was found;
- the new event columns are added nullable, backfilled, and only then tightened
  to `NOT NULL`. A single statement cannot do that on SQLite, and pretending
  otherwise would leave a database that neither version can open.

A database that already holds conversation events can only be migrated with the
operator's key material and a verified backup, supplied through
`config.attributes`. A fresh database needs neither: there is nothing to seal,
nothing to alias, and nothing to restore.

Revision ID: 0003_cap001_timeline
Revises: 0002_notification_outbox_constraints
Create Date: 2026-07-27
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite
from personal_agent.storage.cap001_migration import (
    Cap001MigrationError,
    MigrationInputs,
    apply_plan,
    build_plan,
    preflight,
    restore_legacy_conversations,
    verify_event_aad,
    verify_restore_fixture,
    verify_upgrade,
)


revision: str = "0003_cap001_timeline"
down_revision: str | None = "0002_notification_outbox_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SESSION_STATUS = "status IN ('open', 'closed')"
_RELATION_KIND = (
    "relation_kind IN ('new_topic', 'resumes', 'corrects_boundary', 'legacy')"
)
_BOUNDARY_REASON = (
    "boundary_reason IS NULL OR boundary_reason IN ("
    "'explicit_reset', 'explicit_resume', 'explicit_correction', "
    "'task_boundary', 'idle_and_unrelated', 'previous_closed')"
)
_CHECKPOINT_STATUS = (
    "status IN ('building', 'active', 'superseded', 'invalid')"
)


def _inputs() -> MigrationInputs | None:
    attributes = op.get_context().config.attributes
    supplied = attributes.get("cap001_inputs")
    if supplied is not None and not isinstance(supplied, MigrationInputs):
        raise Cap001MigrationError(
            "config.attributes['cap001_inputs'] must be a MigrationInputs"
        )
    return supplied


def _require_inputs(
    *, event_count: int, legacy_conversation_count: int
) -> MigrationInputs:
    inputs = _inputs()
    if inputs is None:
        raise Cap001MigrationError(
            "this database has legacy Timeline state "
            f"({legacy_conversation_count} conversation(s), "
            f"{event_count} event(s)); migrating it needs the Agent data key "
            "ring and identifier key"
        )
    return inputs


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(tz=timezone.utc)

    # 1. Maintenance-mode preflight, against the schema as it stands.
    event_count = preflight(connection)
    legacy_conversation_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM conversations")
    ).scalar_one()
    needs_aliases = legacy_conversation_count > 0
    inputs = (
        _require_inputs(
            event_count=event_count,
            legacy_conversation_count=legacy_conversation_count,
        )
        if event_count or needs_aliases
        else _inputs()
    )
    if event_count:
        assert inputs is not None
        if inputs.backup_path is None:
            raise Cap001MigrationError(
                "a verified pre-migration backup is required before migrating "
                "existing conversation events"
            )
        verify_restore_fixture(
            connection,
            inputs.backup_path,
            keyring=inputs.keyring,
        )
        verify_event_aad(connection, inputs.keyring)

    # 2. Schema.
    _create_new_tables()
    _extend_conversations()
    _add_event_columns()

    # 3. Data.
    plan = build_plan(connection)
    if plan.legacy_conversation_ids:
        assert inputs is not None
        apply_plan(
            connection,
            plan,
            keyring=inputs.keyring,
            identifier_key=inputs.identifier_key,
            now=now,
        )
    else:
        # A fresh database still gets its one canonical Timeline, so
        # `GET /v1/capabilities` has a stable value to return from the start.
        connection.execute(
            sa.text(
                "INSERT INTO conversations (conversation_id, created_at, "
                "last_event_at, next_sequence, is_canonical) "
                "VALUES (:cid, :created, NULL, 1, 1)"
            ),
            {
                "cid": plan.canonical_id,
                "created": personal_agent_core.sqlite.UtcTimestamp()
                .process_bind_param(now, None),
            },
        )

    # 4. Tighten the backfilled columns and add the ordering contract.
    _tighten_event_columns()

    # 5. Verify what is actually in the database now.
    if plan.legacy_conversation_ids:
        assert inputs is not None
        verify_upgrade(
            connection,
            plan,
            keyring=inputs.keyring,
            identifier_key=inputs.identifier_key,
        )


def downgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(tz=timezone.utc)
    legacy = connection.execute(
        sa.text(
            "SELECT "
            "(SELECT COUNT(*) FROM conversation_events "
            " WHERE encrypted_legacy_conversation_id IS NOT NULL) + "
            "(SELECT COUNT(*) FROM conversation_aliases "
            " WHERE encrypted_legacy_conversation_id IS NOT NULL)"
        )
    ).scalar_one()
    if legacy:
        inputs = _inputs()
        if inputs is None:
            raise Cap001MigrationError(
                "this database holds migrated events; the downgrade needs the "
                "Agent data key ring to recover their original conversations"
            )
        _loosen_event_columns()
        restore_legacy_conversations(
            connection, keyring=inputs.keyring, now=now
        )
    else:
        _loosen_event_columns()

    _drop_event_columns()
    _restore_conversations()
    _drop_new_tables()


# -- schema steps ---------------------------------------------------------


def _create_new_tables() -> None:
    op.create_table(
        "conversation_aliases",
        sa.Column("alias_hmac", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        # Runtime lookup uses only `alias_hmac`. This sealed value exists solely
        # so downgrade can reconstruct an eventless legacy conversation; it is
        # never exposed to clients or used for alias resolution.
        sa.Column(
            "encrypted_legacy_conversation_id",
            personal_agent_core.sqlite.EncryptedEnvelope(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            personal_agent_core.sqlite.UtcTimestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name=op.f("fk_conversation_aliases_conversation_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "alias_hmac", name=op.f("pk_conversation_aliases")
        ),
    )
    op.create_table(
        "context_sessions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("boundary_reason", sa.Text(), nullable=True),
        sa.Column("parent_session_id", sa.Text(), nullable=True),
        sa.Column("relation_kind", sa.Text(), nullable=False),
        sa.Column("classifier_version", sa.Text(), nullable=True),
        sa.Column(
            "opened_at",
            personal_agent_core.sqlite.UtcTimestamp(),
            nullable=False,
        ),
        sa.Column(
            "closed_at",
            personal_agent_core.sqlite.UtcTimestamp(),
            nullable=True,
        ),
        sa.Column(
            "last_event_at",
            personal_agent_core.sqlite.UtcTimestamp(),
            nullable=True,
        ),
        sa.CheckConstraint(
            _SESSION_STATUS, name=op.f("ck_context_sessions_status")
        ),
        sa.CheckConstraint(
            _RELATION_KIND, name=op.f("ck_context_sessions_relation_kind")
        ),
        sa.CheckConstraint(
            _BOUNDARY_REASON, name=op.f("ck_context_sessions_boundary_reason")
        ),
        sa.CheckConstraint(
            "(relation_kind IN ('resumes', 'corrects_boundary')) "
            "= (parent_session_id IS NOT NULL)",
            name=op.f("ck_context_sessions_lineage_requires_parent"),
        ),
        sa.CheckConstraint(
            "parent_session_id IS NULL OR parent_session_id <> session_id",
            name=op.f("ck_context_sessions_parent_is_not_self"),
        ),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)",
            name=op.f("ck_context_sessions_closed_at_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name=op.f("fk_context_sessions_conversation_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_session_id"],
            ["context_sessions.session_id"],
            name=op.f("fk_context_sessions_parent_session_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_context_sessions")),
    )
    op.create_index(
        "uq_context_sessions_open",
        "context_sessions",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_context_sessions_parent", "context_sessions", ["parent_session_id"]
    )
    op.create_table(
        "context_checkpoints",
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("covered_from_sequence", sa.Integer(), nullable=False),
        sa.Column("covered_through_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "encrypted_payload",
            personal_agent_core.sqlite.EncryptedEnvelope(),
            nullable=False,
        ),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("compactor_version", sa.Text(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            personal_agent_core.sqlite.UtcTimestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            _CHECKPOINT_STATUS, name=op.f("ck_context_checkpoints_status")
        ),
        sa.CheckConstraint(
            "covered_from_sequence > 0",
            name=op.f("ck_context_checkpoints_covered_from_positive"),
        ),
        sa.CheckConstraint(
            "covered_through_sequence >= covered_from_sequence",
            name=op.f("ck_context_checkpoints_covered_range_monotonic"),
        ),
        sa.CheckConstraint(
            "estimated_tokens >= 0",
            name=op.f("ck_context_checkpoints_estimated_tokens_positive"),
        ),
        sa.CheckConstraint(
            "parent_checkpoint_id IS NULL "
            "OR parent_checkpoint_id <> checkpoint_id",
            name=op.f("ck_context_checkpoints_parent_is_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["context_sessions.session_id"],
            name=op.f("fk_context_checkpoints_session_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_checkpoint_id"],
            ["context_checkpoints.checkpoint_id"],
            name=op.f("fk_context_checkpoints_parent_checkpoint_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "checkpoint_id", name=op.f("pk_context_checkpoints")
        ),
    )
    op.create_index(
        "uq_context_checkpoints_active",
        "context_checkpoints",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "context_checkpoint_sources",
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_hmac", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name=op.f("ck_context_checkpoint_sources_ordinal_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["checkpoint_id"],
            ["context_checkpoints.checkpoint_id"],
            name=op.f("fk_context_checkpoint_sources_checkpoint_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "checkpoint_id",
            "ordinal",
            name=op.f("pk_context_checkpoint_sources"),
        ),
    )
    op.create_index(
        "ix_context_checkpoint_sources_hmac",
        "context_checkpoint_sources",
        ["source_hmac"],
    )


def _extend_conversations() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "next_sequence",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_canonical",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint("next_sequence_positive", "next_sequence >= 1")
    op.create_index(
        "uq_conversations_canonical",
        "conversations",
        ["is_canonical"],
        unique=True,
        sqlite_where=sa.text("is_canonical = 1"),
    )


def _add_event_columns() -> None:
    with op.batch_alter_table("conversation_events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "encrypted_legacy_conversation_id",
                personal_agent_core.sqlite.EncryptedEnvelope(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("timeline_sequence", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("session_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("turn_id", sa.Text(), nullable=True))


def _tighten_event_columns() -> None:
    with op.batch_alter_table("conversation_events", schema=None) as batch_op:
        batch_op.alter_column(
            "timeline_sequence", existing_type=sa.Integer(), nullable=False
        )
        batch_op.alter_column(
            "session_id", existing_type=sa.Text(), nullable=False
        )
        batch_op.alter_column("turn_id", existing_type=sa.Text(), nullable=False)
        batch_op.create_check_constraint(
            "timeline_sequence_positive", "timeline_sequence > 0"
        )
        batch_op.create_unique_constraint(
            "conversation_id_timeline_sequence",
            ["conversation_id", "timeline_sequence"],
        )
        batch_op.create_foreign_key(
            op.f("fk_conversation_events_session_id"),
            "context_sessions",
            ["session_id"],
            ["session_id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_conversation_events_session_id", "conversation_events", ["session_id"]
    )
    op.create_index(
        "ix_conversation_events_turn_id", "conversation_events", ["turn_id"]
    )
    op.create_index(
        "ix_conversation_events_operation_id",
        "conversation_events",
        ["operation_id"],
    )


def _loosen_event_columns() -> None:
    op.drop_index(
        "ix_conversation_events_operation_id", table_name="conversation_events"
    )
    op.drop_index(
        "ix_conversation_events_turn_id", table_name="conversation_events"
    )
    op.drop_index(
        "ix_conversation_events_session_id", table_name="conversation_events"
    )
    with op.batch_alter_table("conversation_events", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("fk_conversation_events_session_id"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            "conversation_id_timeline_sequence", type_="unique"
        )
        batch_op.drop_constraint("timeline_sequence_positive", type_="check")
        batch_op.alter_column(
            "timeline_sequence", existing_type=sa.Integer(), nullable=True
        )
        batch_op.alter_column(
            "session_id", existing_type=sa.Text(), nullable=True
        )
        batch_op.alter_column("turn_id", existing_type=sa.Text(), nullable=True)


def _drop_event_columns() -> None:
    with op.batch_alter_table("conversation_events", schema=None) as batch_op:
        batch_op.drop_column("turn_id")
        batch_op.drop_column("session_id")
        batch_op.drop_column("timeline_sequence")
        batch_op.drop_column("encrypted_legacy_conversation_id")


def _restore_conversations() -> None:
    op.drop_index("uq_conversations_canonical", table_name="conversations")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_constraint("next_sequence_positive", type_="check")
        batch_op.drop_column("is_canonical")
        batch_op.drop_column("next_sequence")


def _drop_new_tables() -> None:
    op.drop_index(
        "ix_context_checkpoint_sources_hmac",
        table_name="context_checkpoint_sources",
    )
    op.drop_table("context_checkpoint_sources")
    op.drop_index(
        "uq_context_checkpoints_active", table_name="context_checkpoints"
    )
    op.drop_table("context_checkpoints")
    op.drop_index("ix_context_sessions_parent", table_name="context_sessions")
    op.drop_index("uq_context_sessions_open", table_name="context_sessions")
    op.drop_table("context_sessions")
    op.drop_table("conversation_aliases")
