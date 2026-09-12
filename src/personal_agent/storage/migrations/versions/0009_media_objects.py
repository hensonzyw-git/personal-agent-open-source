"""add media objects, upload attempts and message bindings

Multimodal design §5.1 with the option-1 revision. Three tables, and that
revision is visible in all of them:

- `media_objects` is one persisted image. It carries no `normalizer_version`
  and no processing state, because the adopted option deleted the server-side
  decoder: the bytes sealed during `PUT` are the persisted image and nothing
  recomputes them. Two consequences are recorded here rather than left to be
  rediscovered. First, `content_size` / `encrypted_content_sha256` are the only
  measured pair -- §5.1's field list also names a separate "upload size/hash",
  but with no normalizer those two measured the same bytes, so keeping both
  would store one number twice and let the copies drift apart. Second,
  `declared_width` / `declared_height` are named for what they are: values from
  the client's declaration that the server cannot verify, because verifying
  them needs the decoder that was removed. They are still checked against the
  configured ceiling; that check is on the declaration, and no code may present
  these as measurements.

- `media_attempts` owns the staging bytes. Each attempt records its own seal
  (chunk count, total bytes, order, whole-stream hash) so recovery can compare
  instead of trust, and a partial unique index enforces §5.3's "at most one
  adopted content per media_id". Attempts are the cleanup unit, not a second
  lease system: §5.1 says explicitly not to copy the real lock into the
  database.

- `media_bindings` records use relations. A partial unique index enforces
  exactly one `origin` per object, which is what makes §6's deletion fan-out
  unambiguous about which message owns the object.

`media_objects.current_attempt_number` is a plain integer rather than a foreign
key to `media_attempts.attempt_id`: the two tables would otherwise reference
each other, and SQLite cannot alter a table to add a constraint, so neither
could be built.

No table is dropped and no column is altered, so the downgrade only has to
refuse when it would destroy rows. §5.1 notes the revision number is whatever
the chain head is at merge time; if the chain moves before this lands, only
this file's name and `revision` change.

Revision ID: 0009_media_objects
Revises: 0008_session_automatic_boundaries
Create Date: 2026-09-10
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0009_media_objects"
down_revision: str | None = "0008_session_automatic_boundaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBJECT_STATES = (
    "pending",
    "uploading",
    "uploaded",
    "ready",
    "bound",
    "deleting",
    "reaping",
    "deleted",
    "expired",
    "rejected",
)
_ATTEMPT_STATES = ("claimed", "sealed", "published", "abandoned", "cleaned")
_BINDING_ROLES = ("origin", "reuse")


def _in_set(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


def upgrade() -> None:
    op.create_table(
        "media_objects",
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("retention_class", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("owner_token", sa.Text(), nullable=True),
        sa.Column("current_attempt_number", sa.Integer(), nullable=True),
        sa.Column(
            "claim_deadline", personal_agent_core.sqlite.UtcTimestamp(), nullable=True
        ),
        sa.Column(
            "expires_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True
        ),
        sa.Column("declared_mime", sa.Text(), nullable=True),
        sa.Column("actual_mime", sa.Text(), nullable=True),
        sa.Column("declared_size", sa.Integer(), nullable=True),
        sa.Column("content_size", sa.Integer(), nullable=True),
        sa.Column("declared_width", sa.Integer(), nullable=True),
        sa.Column("declared_height", sa.Integer(), nullable=True),
        sa.Column("key_version", sa.Text(), nullable=True),
        sa.Column(
            "encrypted_declared_sha256",
            personal_agent_core.sqlite.EncryptedEnvelope(),
            nullable=True,
        ),
        sa.Column(
            "encrypted_content_sha256",
            personal_agent_core.sqlite.EncryptedEnvelope(),
            nullable=True,
        ),
        sa.Column(
            "encrypted_storage_ref",
            personal_agent_core.sqlite.EncryptedEnvelope(),
            nullable=True,
        ),
        sa.Column(
            "created_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False
        ),
        sa.Column(
            "uploaded_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True
        ),
        sa.Column("ready_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True),
        sa.Column(
            "deleted_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True
        ),
        sa.CheckConstraint(
            _in_set("purpose", ("chat_image",)),
            name=op.f("ck_media_objects_purpose"),
        ),
        sa.CheckConstraint(
            _in_set("retention_class", ("timeline_media",)),
            name=op.f("ck_media_objects_retention_class"),
        ),
        sa.CheckConstraint(
            _in_set("state", _OBJECT_STATES), name=op.f("ck_media_objects_state")
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name=op.f("ck_media_objects_state_version_positive"),
        ),
        sa.CheckConstraint(
            "declared_size IS NULL OR declared_size >= 0",
            name=op.f("ck_media_objects_declared_size_non_negative"),
        ),
        sa.CheckConstraint(
            "content_size IS NULL OR content_size >= 0",
            name=op.f("ck_media_objects_content_size_non_negative"),
        ),
        sa.CheckConstraint(
            "declared_width IS NULL OR declared_width > 0",
            name=op.f("ck_media_objects_declared_width_positive"),
        ),
        sa.CheckConstraint(
            "declared_height IS NULL OR declared_height > 0",
            name=op.f("ck_media_objects_declared_height_positive"),
        ),
        sa.CheckConstraint(
            "current_attempt_number IS NULL OR current_attempt_number >= 1",
            name=op.f("ck_media_objects_current_attempt_number_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
            name=op.f("fk_media_objects_device_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("media_id", name=op.f("pk_media_objects")),
    )
    op.create_index("ix_media_objects_device_id", "media_objects", ["device_id"])
    op.create_index("ix_media_objects_state", "media_objects", ["state"])
    op.create_index("ix_media_objects_expires_at", "media_objects", ["expires_at"])

    op.create_table(
        "media_attempts",
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("owner_token", sa.Text(), nullable=True),
        sa.Column(
            "claim_deadline", personal_agent_core.sqlite.UtcTimestamp(), nullable=True
        ),
        sa.Column(
            "encrypted_staging_ref",
            personal_agent_core.sqlite.EncryptedEnvelope(),
            nullable=True,
        ),
        sa.Column(
            "encrypted_seal_record",
            personal_agent_core.sqlite.EncryptedEnvelope(),
            nullable=True,
        ),
        sa.Column(
            "created_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False
        ),
        sa.Column(
            "updated_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False
        ),
        sa.Column(
            "sealed_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True
        ),
        sa.Column(
            "cleaned_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=True
        ),
        sa.CheckConstraint(
            _in_set("state", _ATTEMPT_STATES), name=op.f("ck_media_attempts_state")
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name=op.f("ck_media_attempts_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name=op.f("ck_media_attempts_state_version_positive"),
        ),
        # A sealed attempt with no seal record would look adoptable and be
        # unverifiable, leaving the recovery path nothing to compare against.
        sa.CheckConstraint(
            "state IN ('claimed', 'abandoned') OR encrypted_seal_record IS NOT NULL",
            name=op.f("ck_media_attempts_sealed_attempt_has_record"),
        ),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_objects.media_id"],
            name=op.f("fk_media_attempts_media_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_media_attempts")),
        # Short name, not `op.f(...)`: the `uq` naming convention carries no
        # `%(constraint_name)s` token, so an explicit name survives verbatim
        # and must match `models.py` byte for byte. A migrated database and a
        # `create_all` one would otherwise disagree about the constraint name.
        sa.UniqueConstraint(
            "media_id", "attempt_number", name="media_id_attempt_number"
        ),
    )
    op.create_index(
        "uq_media_attempts_published",
        "media_attempts",
        ["media_id"],
        unique=True,
        sqlite_where=sa.text("state = 'published'"),
    )
    op.create_index("ix_media_attempts_state", "media_attempts", ["state"])

    op.create_table(
        "media_bindings",
        sa.Column("binding_id", sa.Text(), nullable=False),
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_operation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", personal_agent_core.sqlite.UtcTimestamp(), nullable=False
        ),
        sa.CheckConstraint(
            _in_set("role", _BINDING_ROLES), name=op.f("ck_media_bindings_role")
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_media_bindings_ordinal_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_objects.media_id"],
            name=op.f("fk_media_bindings_media_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["conversation_events.event_id"],
            name=op.f("fk_media_bindings_event_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name=op.f("fk_media_bindings_operation_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_operation_id"],
            ["operations.operation_id"],
            name=op.f("fk_media_bindings_source_operation_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("binding_id", name=op.f("pk_media_bindings")),
        sa.UniqueConstraint("event_id", "ordinal", name="event_id_ordinal"),
    )
    op.create_index(
        "uq_media_bindings_origin",
        "media_bindings",
        ["media_id"],
        unique=True,
        sqlite_where=sa.text("role = 'origin'"),
    )
    op.create_index("ix_media_bindings_event_id", "media_bindings", ["event_id"])
    op.create_index("ix_media_bindings_media_id", "media_bindings", ["media_id"])
    op.create_index(
        "ix_media_bindings_operation_id", "media_bindings", ["operation_id"]
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("media_bindings", "media_attempts", "media_objects"):
        held = connection.execute(
            sa.text(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608 -- fixed names
        ).scalar_one_or_none()
        if held is not None:
            raise RuntimeError(
                f"cannot downgrade 0009: {table} holds rows this would destroy"
            )
    op.drop_index("ix_media_bindings_operation_id", table_name="media_bindings")
    op.drop_index("ix_media_bindings_media_id", table_name="media_bindings")
    op.drop_index("ix_media_bindings_event_id", table_name="media_bindings")
    op.drop_index("uq_media_bindings_origin", table_name="media_bindings")
    op.drop_table("media_bindings")
    op.drop_index("ix_media_attempts_state", table_name="media_attempts")
    op.drop_index("uq_media_attempts_published", table_name="media_attempts")
    op.drop_table("media_attempts")
    op.drop_index("ix_media_objects_expires_at", table_name="media_objects")
    op.drop_index("ix_media_objects_state", table_name="media_objects")
    op.drop_index("ix_media_objects_device_id", table_name="media_objects")
    op.drop_table("media_objects")
