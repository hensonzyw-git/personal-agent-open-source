"""Agent API tables, per technical design 8.1.

This database holds the client-facing projection: devices, requests, operations,
conversations, reviews and the notification outbox. It is deliberately not the
authority on whether money moved. Finance MCP owns external side-effect state,
and technical design 5.2.2 forbids inferring a ledger outcome by comparing
timestamps across the two databases.

Three invariants are expressed as constraints rather than as prose, because
prose does not survive a refactor:

- one client request maps to at most one operation, via a unique
  `(device_id, client_request_id)`;
- an operation's state is drawn from the fixed set in technical design 5.2, and
  `cancel_requested` / `client_detached` are separate flags rather than states,
  so a disconnect can never be recorded as a cancelled write;
- one review card exists per review date.

Each child carries a many-to-one `relationship()`. A `ForeignKey` alone does
not tell the unit of work which row to insert first: SQLAlchemy falls back to
mapper sort order, which is alphabetical, so an `api_requests` insert would be
attempted before the `devices` row it points at.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

from personal_agent_core.sqlite import (
    NAMING_CONVENTION,
    EncryptedEnvelope,
    UtcTimestamp,
)


#: Authoritative operation states. `policy_denied` is a reason code on
#: `failed_safe`, not a state of its own.
OPERATION_STATES: Final[tuple[str, ...]] = (
    "accepted",
    "interpreting",
    "waiting_for_clarification",
    "waiting_for_duplicate_decision",
    "dispatching",
    "source_in_progress",
    "verifying",
    "succeeded",
    "failed_safe",
    "needs_manual_review",
    "cancelled_pre_submit",
)

#: States after which no further transition is allowed.
TERMINAL_OPERATION_STATES: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed_safe", "needs_manual_review", "cancelled_pre_submit"}
)

#: States the recovery scan must reconcile against Finance MCP on startup.
RECOVERABLE_OPERATION_STATES: Final[tuple[str, ...]] = (
    "dispatching",
    "source_in_progress",
    "verifying",
)

DEVICE_STATUSES: Final[tuple[str, ...]] = ("active", "revoked")
REVIEW_STATUSES: Final[tuple[str, ...]] = ("pending", "deferred", "reviewed")

#: What a human concluded after checking the ledger for an operation that ended
#: at `needs_manual_review`. Deliberately not an accounting outcome: the pair is
#: "the record is there" / "it is not", recorded as a reviewed observation
#: alongside the terminal state rather than replacing it. Closing the loop is a
#: human act; deciding the books is still evidence's job.
MANUAL_RESOLUTIONS: Final[tuple[str, ...]] = (
    "confirmed_written",
    "confirmed_not_written",
)

#: The closed vocabulary a device reports for an action it executed. The phone
#: is the fact source for its own write, so this is testimony rather than a
#: claim to be checked -- and it is kept verbatim on the operation because the
#: four values are not interchangeable downstream:
#:
#: - `created` and `duplicate` both mean the event exists, and both settle with
#:   the EventKit id in `safe_result`, but only `duplicate` says the *device*
#:   found the event already there -- which is the one case 「仍要创建」 may
#:   override (design 3.3). A column that stored only success could not tell
#:   them apart, and an override offered on a `created` would write a second
#:   copy of an event the user already has.
#: - `denied` and `failed` are the device's own zero-write testimony: EventKit
#:   refused before any write could exist, which is what lets the operation
#:   settle `failed_safe` instead of being parked for review.
DEVICE_REPORT_RESULTS: Final[tuple[str, ...]] = (
    "created",
    "duplicate",
    "denied",
    "failed",
)

#: The reports that say the event exists on the phone.
DEVICE_REPORT_WRITES: Final[frozenset[str]] = frozenset(
    {"created", "duplicate"}
)

#: What the push provider has said, which is never what the user has done.
#: `provider_accepted` means APNs took the notification, nothing more; only an
#: explicit `/ack` moves the review itself to `reviewed` (design 7.7 step 6).
#: `undeliverable` is a permanent refusal (a rejected token, or attempts
#: exhausted); `pending` is queued or awaiting its next attempt.
NOTIFICATION_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "provider_accepted",
    "undeliverable",
)

#: `CAP-001`. A Session is a continuous execution segment and is never
#: reopened: continuing a topic creates a *new* Session that points back at the
#: old one, so a closed segment's history can never be rewritten after the fact.
SESSION_STATUSES: Final[tuple[str, ...]] = ("open", "closed")

#: How a Session relates to the one it names as parent (design 5.1).
SESSION_RELATION_KINDS: Final[tuple[str, ...]] = (
    "new_topic",
    "resumes",
    "corrects_boundary",
    "legacy",
)

#: The closed reason set of the Boundary Record (design 6.2). It is `NULL` for
#: a Timeline's first Session and for a `legacy` Session created by migration:
#: neither is the outcome of a boundary decision, and inventing a reason for
#: them would make the audit read as though a classifier had run.
SESSION_BOUNDARY_REASONS: Final[tuple[str, ...]] = (
    "explicit_reset",
    "explicit_resume",
    "explicit_correction",
    "task_boundary",
    "idle_and_unrelated",
    "idle_timeout",
    "completed_tool_unrelated",
    "previous_closed",
)

#: `CAP-001` checkpoint lifecycle (design 5.2). `building` never reaches the
#: Context Builder; a build that loses the compare-and-swap becomes `invalid`
#: rather than overwriting a newer version.
CHECKPOINT_STATUSES: Final[tuple[str, ...]] = (
    "building",
    "active",
    "superseded",
    "invalid",
)

#: Media object lifecycle (multimodal design 5.2, option-1 revision). A media
#: object is one persisted image; the state names the *upload* progress, not
#: processing, because under option 1 the server never derives content. The
#: `normalizing` state the pre-revision design carried was deleted with the
#: decoder: nothing recomputes the object, so there is no step to name.
MEDIA_OBJECT_STATES: Final[tuple[str, ...]] = (
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

#: States a client may still make progress from. Everything else is either
#: terminal or waits on the reaper, and `create` must not reuse the id.
MEDIA_OBJECT_LIVE_STATES: Final[tuple[str, ...]] = (
    "pending",
    "uploading",
    "uploaded",
    "ready",
    "bound",
)

#: Terminal states: tombstoned, never revived, never re-issued.
MEDIA_OBJECT_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {"deleted", "expired", "rejected"}
)

#: One upload attempt's own lifecycle (design 5.3). Attempts exist so a lost
#: response, a failed validation or a superseded writer can be cleaned up
#: idempotently without touching the persisted image; they are *not* a second
#: lease system -- the real mutual exclusion is the §4.1 lock, and §5.1 says
#: explicitly not to copy it into the database.
#:
#: `sealed` is the point of no return for content: the seal record (chunk
#: count, total bytes, whole-stream hash) is stored and the staging file has
#: been fsynced, so this attempt's bytes are complete and comparable. Publishing
#: is a separate step and may be done by a later attempt after a crash.
MEDIA_ATTEMPT_STATES: Final[tuple[str, ...]] = (
    "claimed",
    "sealed",
    "published",
    "abandoned",
    "cleaned",
)

#: How a media object is used by a message. `origin` is the message that first
#: used the object; `reuse` is a later legitimate use (design §5.1 terminology).
#: This describes the *use relation* only -- it says nothing about storage, and
#: at most one `origin` binding may exist per object.
MEDIA_BINDING_ROLES: Final[tuple[str, ...]] = ("origin", "reuse")


def _in_set(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint names.

    Autogenerated migrations are unusable for rollback review if constraint
    names shift between runs, so the naming convention is fixed here.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: P-256 X9.63 uncompressed point, base64url. A public key, not a secret.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    device_key_thumbprint: Mapped[str] = mapped_column(Text, nullable=False)
    #: APNs tokens identify a device to a third party, so they are sealed.
    encrypted_push_token: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_tools_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )

    __table_args__ = (
        CheckConstraint(_in_set("status", DEVICE_STATUSES), name="status"),
        CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) "
            "OR (status = 'revoked' AND revoked_at IS NOT NULL)",
            name="revoked_at_matches_status",
        ),
    )


class EnrollmentCode(Base):
    __tablename__ = "enrollment_codes"

    code_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    grants_device_manage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"

    challenge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    nonce_hash: Mapped[str] = mapped_column(Text, nullable=False)

    failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )

    device: Mapped["Device"] = relationship()

    __table_args__ = (
        CheckConstraint("failed_attempts >= 0", name="failed_attempts_non_negative"),
        Index("ix_auth_challenges_device_id", "device_id"),
    )


class Conversation(Base):
    """The canonical Timeline. `CAP-001` gives it a sequence allocator.

    The physical table keeps its `conversations` name during the compatibility
    period (design 4.2): renaming the public field is an API major version, and
    a SQLite rebuild done purely for a rename would risk the archive for no
    user-visible gain. `is_canonical` is what a single-user deployment actually
    constrains -- a partial unique index allows exactly one canonical row, so an
    unknown client id cannot quietly become a second Timeline.
    """

    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    last_event_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    #: The next `timeline_sequence` to hand out. Allocation is a conditional
    #: UPDATE on this column, so two devices appending at the same instant
    #: cannot receive the same number; `created_at` and SQLite's `rowid` are
    #: deliberately *not* the pagination contract (design 5.1).
    next_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    is_canonical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    __table_args__ = (
        CheckConstraint("next_sequence >= 1", name="next_sequence_positive"),
        # SQLite honours the `WHERE` clause, so this bounds canonical rows at
        # one without preventing a downgrade from restoring legacy rows.
        Index(
            "uq_conversations_canonical",
            "is_canonical",
            unique=True,
            sqlite_where=text("is_canonical = 1"),
        ),
    )


class ConversationAlias(Base):
    """A pre-`CAP-001` conversation id, resolved only through an HMAC.

    Storing the alias in plaintext would defeat the point: these are historical
    identifiers of the owner's private chat. The HMAC lets a device that still
    remembers an old id resolve to the canonical Timeline. A sealed copy is
    retained only as migration rollback material, including for an eventless
    legacy conversation; runtime lookup never reads it.
    """

    __tablename__ = "conversation_aliases"

    alias_hmac: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    encrypted_legacy_conversation_id: Mapped[dict[str, Any] | None] = (
        mapped_column(EncryptedEnvelope, nullable=True)
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    conversation: Mapped["Conversation"] = relationship()


class ContextSession(Base):
    """One automatically-created semantic segment of the Timeline.

    The client never sends or stores a `session_id` (design 4.2.5). Sessions
    exist so a long topic can be compacted without the user losing a continuous
    Timeline, and so a new topic does not inherit an unrelated context.
    """

    __tablename__ = "context_sessions"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    boundary_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_sessions.session_id", ondelete="RESTRICT"),
        nullable=True,
    )
    relation_kind: Mapped[str] = mapped_column(
        Text, nullable=False, default="new_topic"
    )
    classifier_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    last_event_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )

    conversation: Mapped["Conversation"] = relationship()

    __table_args__ = (
        CheckConstraint(_in_set("status", SESSION_STATUSES), name="status"),
        CheckConstraint(
            _in_set("relation_kind", SESSION_RELATION_KINDS),
            name="relation_kind",
        ),
        CheckConstraint(
            "boundary_reason IS NULL OR "
            + _in_set("boundary_reason", SESSION_BOUNDARY_REASONS),
            name="boundary_reason",
        ),
        # `resumes` and `corrects_boundary` are meaningless without a target,
        # and a self-parent is the shortest possible lineage cycle.
        CheckConstraint(
            "(relation_kind IN ('resumes', 'corrects_boundary')) "
            "= (parent_session_id IS NOT NULL)",
            name="lineage_requires_parent",
        ),
        CheckConstraint(
            "parent_session_id IS NULL OR parent_session_id <> session_id",
            name="parent_is_not_self",
        ),
        CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)",
            name="closed_at_matches_status",
        ),
        # At most one open Session per Timeline. Two concurrent messages both
        # deciding to open one is a real race (F-D10); the index, not the
        # pre-check, is what makes only one of them win.
        Index(
            "uq_context_sessions_open",
            "conversation_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
        ),
        Index("ix_context_sessions_parent", "parent_session_id"),
    )


class ConversationEvent(Base):
    """Retained permanently by Henson's decision, always sealed.

    Permanent retention is not permission to feed the archive to a model. Only
    explicit export and deletion are supported in Phase 1.

    `CAP-001` adds the ordering and grouping keys: `timeline_sequence` is the
    stable pagination contract, `session_id` the semantic segment, and `turn_id`
    groups one user message with the result of the operation it started.
    """

    __tablename__ = "conversation_events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Only set by migration, for events that predate the canonical Timeline.
    #: Sealed, because it is a historical identifier of the owner's own chat and
    #: because a downgrade has to be able to recover it exactly.
    encrypted_legacy_conversation_id: Mapped[dict[str, Any] | None] = (
        mapped_column(EncryptedEnvelope, nullable=True)
    )
    timeline_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("context_sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
    )
    turn_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    encrypted_content: Mapped[dict[str, Any]] = mapped_column(
        EncryptedEnvelope, nullable=False
    )
    operation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    conversation: Mapped["Conversation"] = relationship()
    session: Mapped["ContextSession"] = relationship()

    __table_args__ = (
        Index("ix_conversation_events_conversation_id", "conversation_id"),
        # The pagination and checkpoint-range contract in one constraint.
        UniqueConstraint(
            "conversation_id",
            "timeline_sequence",
            name="conversation_id_timeline_sequence",
        ),
        CheckConstraint("timeline_sequence > 0", name="timeline_sequence_positive"),
        Index("ix_conversation_events_session_id", "session_id"),
        Index("ix_conversation_events_turn_id", "turn_id"),
        Index("ix_conversation_events_operation_id", "operation_id"),
    )


class ContextCheckpoint(Base):
    """One immutable compaction of a Session's context (design 5.2).

    A checkpoint never replaces the raw archive; it summarises a closed range of
    it. `source_hash` binds the summary to that exact range, so a correct
    summary of one stretch of history can never be grafted onto another.
    """

    __tablename__ = "context_checkpoints"

    checkpoint_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("context_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_checkpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_checkpoints.checkpoint_id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="building")
    covered_from_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_through_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    encrypted_payload: Mapped[dict[str, Any]] = mapped_column(
        EncryptedEnvelope, nullable=False
    )
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    compactor_version: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    session: Mapped["ContextSession"] = relationship()

    __table_args__ = (
        CheckConstraint(_in_set("status", CHECKPOINT_STATUSES), name="status"),
        CheckConstraint(
            "covered_from_sequence > 0", name="covered_from_positive"
        ),
        CheckConstraint(
            "covered_through_sequence >= covered_from_sequence",
            name="covered_range_monotonic",
        ),
        CheckConstraint("estimated_tokens >= 0", name="estimated_tokens_positive"),
        CheckConstraint(
            "parent_checkpoint_id IS NULL "
            "OR parent_checkpoint_id <> checkpoint_id",
            name="parent_is_not_self",
        ),
        # Exactly one active checkpoint per Session. Two concurrent Compactor
        # runs are expected; the loser must not be able to replace the winner.
        Index(
            "uq_context_checkpoints_active",
            "session_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
    )


class ContextCheckpointSource(Base):
    """The ordered sources a checkpoint was built from, as HMACs only.

    Deletion propagation needs to find every checkpoint derived from a deleted
    event, which requires a stable per-source key; it does not require the
    source id itself, so the row never holds one.
    """

    __tablename__ = "context_checkpoint_sources"

    checkpoint_id: Mapped[str] = mapped_column(
        ForeignKey("context_checkpoints.checkpoint_id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_hmac: Mapped[str] = mapped_column(Text, nullable=False)

    checkpoint: Mapped["ContextCheckpoint"] = relationship()

    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        Index("ix_context_checkpoint_sources_hmac", "source_hmac"),
    )


class ApiRequest(Base):
    __tablename__ = "api_requests"

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    encrypted_request_payload: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    device: Mapped["Device"] = relationship()

    __table_args__ = (
        # The whole point of client idempotency: the same key from the same
        # device can only ever name one request.
        UniqueConstraint(
            "device_id", "client_request_id", name="device_id_client_request_id"
        ),
    )


class Operation(Base):
    __tablename__ = "operations"

    operation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("api_requests.request_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: Same UUIDv4 the client sent; also the Feishu client_token downstream.
    idempotency_key: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    tool: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Immediate prior operation this one is allowed to retry. The source must
    #: be a terminal, proven-zero-write failure; application code enforces that
    #: semantic rule, while the unique constraint makes consumption one-shot.
    retry_of_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.operation_id", ondelete="RESTRICT", name="retry_source_operation"),
        nullable=True,
    )

    api_request: Mapped["ApiRequest"] = relationship()

    state: Mapped[str] = mapped_column(Text, nullable=False)
    #: Compare-and-swap guard so a resumed worker cannot overwrite newer state.
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Kept apart from `state`: a client giving up never means the write did.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    client_detached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    duplicate_check_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: `G1`: the written ledger row the iOS receipt card draws its fields from.
    #:
    #: Deliberately a second column rather than more content inside
    #: `safe_result`. For a governed write `safe_result` *is* the external record
    #: id, and the whole receipt projection turns on that: overloading it would
    #: make the strongest claim this system makes -- "this row exists in the
    #: ledger" -- depend on parsing. The two facts are also not equally certain.
    #: The record id is proof; these fields are presentation, and an
    #: `idempotent_replay` legitimately has the first without the second.
    #:
    #: Sealed, because it is the first place ledger content -- a name and an
    #: exact amount -- would otherwise sit in this database as plaintext. The
    #: Timeline's own copy already travels inside `encrypted_content`, and the
    #: write audit forbids `exact_amount` outright; a clear column here would be
    #: the one exposure the rest of the design took care to avoid.
    encrypted_result_record: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    #: The device action issued for this operation (`calendar.create_event`),
    #: sealed on the row in the same committed transition that parks the
    #: operation at `source_in_progress` (review R6, 2026-09-08).
    #:
    #: Why it exists: the chat response used to be the action's only delivery
    #: channel, and a request that timed out at 202 lost the action while the
    #: operation stayed parked. With the seal, the operation projection is the
    #: one delivery door -- the 200 reply, the by-id poll and a replay all
    #: converge on the same parked-state read -- and a settled operation
    #: refuses to hand the action over, so a stale read can never re-execute a
    #: finished write.
    #:
    #: Sealed because the event fields are the user's personal schedule and
    #: the model-authorised intent itself; the same exposure rule that governs
    #: `encrypted_result_record` applies, and the Timeline's copy already
    #: travels inside `encrypted_content`.
    encrypted_device_action: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    #: The resolved tool call this operation was authorised to make -- the
    #: *attested* arguments, sealed on the same transition that issues the
    #: device action (design 3.3).
    #:
    #: It is what an override resumes from: 「仍要创建」 must re-issue the write
    #: the user originally authorised, never a re-derived one, so the arguments
    #: have to outlive the turn that produced them -- and outlive *settlement*,
    #: which is why this is not the device-action seal beside it (cleared when
    #: the operation leaves `source_in_progress`; a duplicate is only discovered
    #: after the phone reports). It is not `api_requests
    #: .encrypted_request_payload` either: that column holds the chat request
    #: the turn replays, and writing an intent there would cost the request its
    #: own idempotent replay.
    #:
    #: Sealed because the arguments name the user's calendar, title and time,
    #: and bound by AAD to this operation so a ciphertext cannot be lifted onto
    #: another row.
    encrypted_request: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    #: The operation an override was derived from. Kept so the override is
    #: visible in the audit as *two* operations -- the original write and the
    #: deliberate second one -- rather than one silent pass. See
    #: `parent_operation_derives_once` for why the promise is a constraint.
    parent_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.operation_id", ondelete="RESTRICT", name="parent_operation"),
        nullable=True,
    )
    #: What the phone reported it did with the action. The verbatim value, not a
    #: boolean, because the four reports are not interchangeable: only
    #: `duplicate` may be overridden, and both success reports otherwise look
    #: identical on this row (state `succeeded`, EventKit id in `safe_result`).
    device_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Which frozen action plan this operation belongs to, and where in it
    #: (design 4.1). One message that asks for several events is interpreted
    #: once, and the whole list is written down before any of it is issued, so a
    #: crash halfway through cannot re-ask the model and get a different list.
    #: The key is the message's own idempotency key, and the items are read back
    #: in index order.
    #:
    #: Both halves are written together or not at all: an index without its plan
    #: would be a position in a list nobody can find, and a plan without its
    #: index could not be ordered. The pair is unique because the derived item
    #: key is `uuid5(plan, index)` -- a promise the derivation keeps today and
    #: which the constraint keeps if the derivation ever changes.
    plan_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What a human concluded after looking at the ledger, for an operation that
    #: ended at `needs_manual_review`. A *flag*, not a state, for the same reason
    #: `cancel_requested` is one: the accounting outcome belongs to the state
    #: machine and its evidence, and a person saying "I checked" is neither. The
    #: state stays terminal, so recovery still never re-drives the operation, and
    #: nothing here is ever read back as proof that a write happened.
    manual_resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_resolved_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(_in_set("state", OPERATION_STATES), name="state"),
        CheckConstraint("state_version >= 1", name="state_version_positive"),
        CheckConstraint(
            "retry_of_operation_id IS NULL OR "
            "retry_of_operation_id <> operation_id",
            name="retry_source_is_not_self",
        ),
        UniqueConstraint(
            "retry_of_operation_id",
            name="retry_source_consumed_once",
        ),
        # Business fields may only accompany a state that could have reached the
        # ledger. Not a general annotation slot: a `failed_safe` row carrying a
        # name and an amount would render as a receipt for a write that never
        # happened. `needs_manual_review` qualifies because it can hold a record
        # id too -- the write may well have landed, which is why someone is
        # being asked to look.
        CheckConstraint(
            "encrypted_result_record IS NULL"
            " OR state IN ('succeeded', 'needs_manual_review')",
            name="result_record_only_where_written",
        ),
        # An undelivered device action may only sit on an operation parked at
        # `source_in_progress` — the one state whose meaning is "the write was
        # authorised and handed off, the executor may act". A settled state
        # carrying a live action would let a stale read re-arm a finished
        # write, so settlement clears the seal (`transition_operation` does it
        # centrally when leaving `source_in_progress`) and this constraint
        # makes any future settlement path that forgets to fail loudly
        # instead of silently re-arming an action. (`failed_safe` never holds
        # one: the pre-submit recovery walk to `failed_safe` is exactly the
        # case where no response ever carried the action anywhere. Review R6,
        # 2026-09-08.)
        CheckConstraint(
            "encrypted_device_action IS NULL OR state = 'source_in_progress'",
            name="device_action_only_while_parked",
        ),
        # An operation may be derived from at most one parent and may not be its
        # own parent. Today the derived key is
        # `uuid5(namespace, "<parent>:calendar-override")`, which could not
        # produce two children or a self-parent -- but design 3.3's promise
        # ("一次 duplicate → 至多一条派生 operation") is what makes a double tap
        # harmless, and a promise should not rest on a derivation a later change
        # could alter.
        CheckConstraint(
            "parent_operation_id IS NULL OR parent_operation_id <> operation_id",
            name="parent_operation_is_not_self",
        ),
        UniqueConstraint(
            "parent_operation_id", name="parent_operation_derives_once"
        ),
        CheckConstraint(
            "device_result IS NULL OR "
            + _in_set("device_result", DEVICE_REPORT_RESULTS),
            name="device_result",
        ),
        # A plan membership is one fact in two columns: an index with no plan
        # names a position in a list nobody can find, and a plan with no index
        # cannot be ordered. A negative index is not a position at all.
        CheckConstraint(
            "(plan_key IS NULL) = (plan_index IS NULL)"
            " AND (plan_index IS NULL OR plan_index >= 0)",
            name="plan_membership_is_whole",
        ),
        UniqueConstraint("plan_key", "plan_index", name="plan_item_once"),
        CheckConstraint(
            _in_set("manual_resolution", MANUAL_RESOLUTIONS)
            + " OR manual_resolution IS NULL",
            name="manual_resolution",
        ),
        # A resolution and its timestamp are one fact; neither half is meaningful
        # without the other, and a half-written pair would make "has this been
        # reviewed?" unanswerable.
        CheckConstraint(
            "(manual_resolution IS NULL) = (manual_resolved_at IS NULL)",
            name="manual_resolution_pairs_with_its_time",
        ),
        # Only an operation a human was actually asked to look at may carry one.
        CheckConstraint(
            "manual_resolution IS NULL OR state = 'needs_manual_review'",
            name="manual_resolution_only_for_review",
        ),
        Index("ix_operations_state", "state"),
    )


class ConnectorCatalog(Base):
    __tablename__ = "connector_catalog"

    connector_id: Mapped[str] = mapped_column(Text, primary_key=True)
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    catalog_hash: Mapped[str] = mapped_column(Text, nullable=False)
    health_status: Mapped[str] = mapped_column(Text, nullable=False)
    last_connected_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    last_discovered_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )


class DailyReview(Base):
    __tablename__ = "daily_reviews"

    review_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: Unique so catch-up runs cannot create a second card for the same day.
    review_date: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    #: The Timeline event holding this card's frozen value snapshot, or `None`
    #: when none has been sealed yet (a review built before migration 0007, or
    #: one whose event append is still owed after a crash between the review-row
    #: commit and the append).
    timeline_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(_in_set("status", REVIEW_STATUSES), name="status"),
        CheckConstraint(
            "review_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
            name="review_date_is_iso",
        ),
    )


class DailyReviewItem(Base):
    """A pointer to an external record, never a copy of its values.

    The card must show what Feishu holds now, so that a manual correction on the
    computer is visible immediately. Caching amounts here would show stale ones.
    """

    __tablename__ = "daily_review_items"

    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("daily_reviews.review_id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    record_id: Mapped[str] = mapped_column(Text, nullable=False)

    review: Mapped["DailyReview"] = relationship()
    committed_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "review_id",
            "tool",
            "record_id",
            name="review_id_tool_record_id",
        ),
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"), nullable=False
    )
    review_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    device: Mapped["Device"] = relationship()
    #: APNs accepting a push is not the user having seen it.
    provider_status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            _in_set("provider_status", NOTIFICATION_STATUSES),
            name="provider_status",
        ),
        # One notification per review per device: a retry updates the row it
        # already has, so a redelivery can never become a second push.
        UniqueConstraint("review_id", "device_id", name="review_id_device_id"),
    )


class DeletionManifest(Base):
    """What a restore must re-apply so deleted data does not come back.

    The object id is sealed: a manifest that names conversations in plaintext
    would leak exactly what the user asked to remove.
    """

    __tablename__ = "deletion_manifest"

    entry_id: Mapped[str] = mapped_column(Text, primary_key=True)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_object_id: Mapped[dict[str, Any]] = mapped_column(
        EncryptedEnvelope, nullable=False
    )
    deleted_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    backup_expiry_after: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )


class DalResumeProposal(Base):
    __tablename__ = "dal_resume_proposals"
    proposal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    device_id: Mapped[str] = mapped_column(Text, ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)


class DalResumeDecision(Base):
    __tablename__ = "dal_resume_decisions"
    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_id: Mapped[str] = mapped_column(Text, ForeignKey("dal_resume_proposals.proposal_id", ondelete="RESTRICT"), nullable=False)
    device_id: Mapped[str] = mapped_column(Text, ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    key_thumbprint: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    claims: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)


class DalResumeDelivery(Base):
    __tablename__ = "dal_resume_deliveries"
    decision_id: Mapped[str] = mapped_column(Text, ForeignKey("dal_resume_decisions.decision_id", ondelete="RESTRICT"), primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('queued','accepted','rejected','expired','delivery_unknown')", name='delivery_status'),
        CheckConstraint('attempts >= 0', name='delivery_attempts'),
        CheckConstraint("status <> 'expired' OR attempts = 0", name='expired_never_attempted'),
        CheckConstraint("status <> 'delivery_unknown' OR attempts > 0", name='unknown_attempted'),
        CheckConstraint("(status = 'accepted' AND approval_id IS NOT NULL AND attempts > 0) OR (status <> 'accepted' AND approval_id IS NULL)", name='delivery_evidence'),
    )

class MediaObject(Base):
    """One persisted image from a chat message (multimodal design 5.1).

    Under the adopted option 1 the server never decodes and never derives
    content: the bytes it seals during `PUT` *are* the persisted image. That
    single fact drives most of this table's shape, and every place it shows is
    marked below, because the pre-revision design carried fields that only made
    sense next to a normalizer.

    Ownership is the device, as everywhere else in this schema. The system is
    single-user by construction, so no user column exists here or anywhere
    else; `device_id` is what an authorization check actually tests.

    Sealed-versus-plaintext split (§5.1): hashes and the storage reference are
    sealed, because a photo's digest is a fingerprint of that photo and a
    storage path names it. State, relationship keys, MIME and sizes stay in
    plaintext -- they are what the state machine and the reaper must query
    without opening envelopes.
    """

    __tablename__ = "media_objects"

    media_id: Mapped[str] = mapped_column(Text, primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False
    )

    #: The `Idempotency-Key` of the request that created this object, or NULL
    #: for a row no keyed request made (a test, a restore, a future server-side
    #: producer). `api_requests` calls the same value `client_request_id`; this
    #: is that key, and the partial unique index below keeps one key from one
    #: device naming two objects.
    client_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    retention_class: Mapped[str] = mapped_column(Text, nullable=False)

    state: Mapped[str] = mapped_column(Text, nullable=False)
    #: Compare-and-swap guard, same contract as `operations.state_version`: a
    #: writer that lost its claim must not transition newer state.
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    #: Which writer currently holds the upload claim, and until when. The
    #: deadline bounds *this attempt*; the real mutual exclusion is the §4.1
    #: lock, so an expired deadline lets a new attempt take over rather than
    #: being a lease the reaper trusts on its own.
    owner_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claim_deadline: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )

    #: When an unbound object stops being resumable. `uploaded` past this
    #: deadline may be taken over by a new attempt or terminated; it is never
    #: retried forever (§5.2).
    expires_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)

    #: What the client declared at `create`, and what the bounded header probe
    #: actually read. Both are plaintext: an image MIME type is not personal
    #: data, and the whole point of keeping them apart is that a mismatch
    #: rejects the object, which the API must be able to test cheaply.
    declared_mime: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_mime: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Measured by the server while streaming, never taken from the client.
    #: **Option-1 revision**: this is the size of the sealed upload's plaintext,
    #: which is also the persisted image's size. The pre-revision design kept a
    #: separate "upload" pair beside this one; with no normalizer those two
    #: measured the same bytes, so they are one pair here. See the migration for
    #: the note that the field list in design §5.1 still reads as two.
    content_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: **Client-declared, not server-measured** (option-1 revision). The design
    #: deleted the decoder, so the server has no way to read real pixel
    #: dimensions; these are the values from the client's declaration and must
    #: never be presented as a server measurement. They are still checked
    #: against the configured pixel ceiling -- that check is on the declaration,
    #: and the server does not claim to have verified it.
    declared_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    declared_height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Which key version sealed this object. Recovery needs the right one.
    key_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Sealed: the client's declared content hash, the server-measured content
    #: hash, and where the persisted image lives. The measured hash is over the
    #: plaintext bytes of the sealed upload (§5.1 option-1 revision); backup
    #: transport uses a different, ciphertext-side SHA-256 and the two must not
    #: be mixed.
    encrypted_declared_sha256: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    encrypted_content_sha256: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    encrypted_storage_ref: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    #: When the upload was sealed (staging fsynced, seal record stored).
    uploaded_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    #: When the persisted image was published to its final path.
    ready_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    #: Tombstone time. Never reused, and the row outlives the bytes so a reader
    #: sees a tombstone rather than an absence.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)

    device: Mapped["Device"] = relationship()
    #: `passive_deletes` on both children because the delete semantics are
    #: declared on the foreign keys, and they differ on purpose: attempts
    #: cascade with the object, bindings refuse to let it go while a message
    #: still uses it. Without this the ORM loads the children and tries to null
    #: their foreign key instead, which turns the RESTRICT into an incidental
    #: NOT NULL failure and leaves the real rule unstated in the model.
    attempts: Mapped[list["MediaAttempt"]] = relationship(
        back_populates="media_object",
        order_by="MediaAttempt.attempt_number",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    bindings: Mapped[list["MediaBinding"]] = relationship(
        back_populates="media_object", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(_in_set("purpose", ("chat_image",)), name="purpose"),
        CheckConstraint(
            _in_set("retention_class", ("timeline_media",)), name="retention_class"
        ),
        CheckConstraint(_in_set("state", MEDIA_OBJECT_STATES), name="state"),
        CheckConstraint("state_version >= 1", name="state_version_positive"),
        CheckConstraint(
            "declared_size IS NULL OR declared_size >= 0", name="declared_size_non_negative"
        ),
        CheckConstraint(
            "content_size IS NULL OR content_size >= 0", name="content_size_non_negative"
        ),
        CheckConstraint(
            "declared_width IS NULL OR declared_width > 0", name="declared_width_positive"
        ),
        CheckConstraint(
            "declared_height IS NULL OR declared_height > 0",
            name="declared_height_positive",
        ),
        CheckConstraint(
            "current_attempt_number IS NULL OR current_attempt_number >= 1",
            name="current_attempt_number_positive",
        ),
        Index(
            "uq_media_objects_client_request",
            "device_id",
            "client_request_id",
            unique=True,
            sqlite_where=text("client_request_id IS NOT NULL"),
        ),
        Index("ix_media_objects_device_id", "device_id"),
        Index("ix_media_objects_state", "state"),
        # The reaper's two scans: unbound objects past their deadline, and
        # tombstones whose bytes have not been reaped yet.
        Index("ix_media_objects_expires_at", "expires_at"),
    )


class MediaAttempt(Base):
    """One upload attempt for a media object (design 5.3).

    Attempts are the cleanup unit. A lost `PUT` response, a rejected object, a
    superseded writer and a crash between seal and publish all leave staging
    bytes behind, and each of those is owned by exactly one attempt -- so
    cleanup can be idempotent per attempt without ever touching the persisted
    image.

    The seal record is what makes recovery safe rather than trusting: it is
    stored when the staging file is fsynced, and a later attempt that finds an
    already-published final file adopts it only if the final bytes authenticate
    and match this record's whole-stream hash. Different ciphertext for the
    same plaintext is normal (random nonces) and must not read as corruption.
    """

    __tablename__ = "media_attempts"

    attempt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    media_id: Mapped[str] = mapped_column(
        ForeignKey("media_objects.media_id", ondelete="CASCADE"), nullable=False
    )
    #: Monotonic per object. Referenced by `media_objects.current_attempt_number`
    #: rather than by a foreign key, which would make the two tables mutually
    #: dependent and leave SQLite unable to build either.
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    owner_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_deadline: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )

    #: Sealed: where this attempt's staging bytes live, and the seal record
    #: (authenticated chunk count, total bytes, order and whole-stream hash).
    encrypted_staging_ref: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    encrypted_seal_record: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    sealed_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    #: When this attempt's staging bytes were confirmed gone (or found gone).
    cleaned_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)

    media_object: Mapped["MediaObject"] = relationship(back_populates="attempts")

    __table_args__ = (
        CheckConstraint(_in_set("state", MEDIA_ATTEMPT_STATES), name="state"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint("state_version >= 1", name="state_version_positive"),
        # A sealed attempt without a seal record would be adoptable-looking but
        # unverifiable, and the recovery path would have nothing to compare.
        CheckConstraint(
            "state IN ('claimed', 'abandoned') OR encrypted_seal_record IS NOT NULL",
            name="sealed_attempt_has_record",
        ),
        UniqueConstraint("media_id", "attempt_number", name="media_id_attempt_number"),
        # §5.3: at most one adopted content per object. Publishing is the
        # adoption, so at most one published attempt may exist.
        Index(
            "uq_media_attempts_published",
            "media_id",
            unique=True,
            sqlite_where=text("state = 'published'"),
        ),
        Index("ix_media_attempts_state", "state"),
    )


class MediaBinding(Base):
    """How a message uses a media object (design §5.1 and §6).

    `origin` is the message that first used the object, `reuse` a later
    legitimate use (a clarification chain, or a controlled safety retry). The
    role describes the *use relation* only: deleting the origin message fans
    out to the media object, while deleting a reuse message removes just that
    relation and leaves the persisted image alone. That asymmetry is why these
    are rows rather than a column on either side.

    `event_id` cascades, because a use relation cannot outlive the message that
    makes it; `media_id` does not, because destroying the object is a decision
    the fan-out makes explicitly, under the §6 lock, with a manifest entry.
    """

    __tablename__ = "media_bindings"

    binding_id: Mapped[str] = mapped_column(Text, primary_key=True)
    media_id: Mapped[str] = mapped_column(
        ForeignKey("media_objects.media_id", ondelete="RESTRICT"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_events.event_id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.operation_id", ondelete="RESTRICT"), nullable=True
    )

    role: Mapped[str] = mapped_column(Text, nullable=False)
    #: Position in the message's ordered `parts` array. The ordering is part of
    #: the contract (text before image), so two bindings may not claim the same
    #: slot.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    #: For a `reuse`, the operation whose use this one re-establishes. The
    #: server verifies the lineage before accepting it.
    source_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.operation_id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    media_object: Mapped["MediaObject"] = relationship(back_populates="bindings")
    event: Mapped["ConversationEvent"] = relationship()

    __table_args__ = (
        CheckConstraint(_in_set("role", MEDIA_BINDING_ROLES), name="role"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        # Exactly one origin per object: "首次使用该对象的那条消息" is singular
        # by definition, and a second origin would make the deletion fan-out
        # ambiguous about which message owns the object.
        Index(
            "uq_media_bindings_origin",
            "media_id",
            unique=True,
            sqlite_where=text("role = 'origin'"),
        ),
        UniqueConstraint("event_id", "ordinal", name="event_id_ordinal"),
        Index("ix_media_bindings_event_id", "event_id"),
        Index("ix_media_bindings_media_id", "media_id"),
        Index("ix_media_bindings_operation_id", "operation_id"),
    )


# Frozen Core definitions: concurrency guards intentionally use fresh SELECTs.
from personal_agent.storage.run_schema_v2 import register_run_tables

register_run_tables(Base.metadata)
