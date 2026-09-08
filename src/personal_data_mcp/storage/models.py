"""Finance MCP tables, per technical design 8.2.

This database is the authority on external side effects. The Agent API keeps a
projection for the client, but whether a Feishu record exists is decided here
and nowhere else.

The execution table is the whole safety design in one row. Its state is written
before the network call, not after, because the only question that matters after
a crash is "might the request have reached Feishu?" and a row written afterwards
cannot answer it.
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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from personal_agent_core.sqlite import (
    NAMING_CONVENTION,
    EncryptedEnvelope,
    UtcTimestamp,
)


#: Execution states, per technical design 7.6. `submitting` is entered before
#: the HTTP request leaves, so a crash can never be read as "never sent".
EXECUTION_STATES: Final[tuple[str, ...]] = (
    "prepared",
    "submitting",
    "commit_unknown",
    "reconciling_same_client_token",
    "committed_unverified",
    "succeeded",
    "failed_safe",
    "needs_manual_review",
    "cancelled_pre_submit",
)

TERMINAL_EXECUTION_STATES: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed_safe", "needs_manual_review", "cancelled_pre_submit"}
)

#: Past this point the request may already have reached the fact source. No
#: transition may return to `prepared`, and no new idempotency key may be minted.
POST_SUBMIT_STATES: Final[frozenset[str]] = frozenset(
    {
        "submitting",
        "commit_unknown",
        "reconciling_same_client_token",
        "committed_unverified",
    }
)

DUPLICATE_CHECK_STATUSES: Final[tuple[str, ...]] = (
    "awaiting_decision",
    "write_anyway",
    "dismissed",
    "expired",
)

SCHEMA_SNAPSHOT_STATUSES: Final[tuple[str, ...]] = ("valid", "drifted")

TABLE_KINDS: Final[tuple[str, ...]] = ("expense", "income", "family_fund")

#: What a person reported finding in the ledger for an execution parked at
#: `needs_manual_review`. The same vocabulary as the Agent side (DEV-040), so the
#: two halves of one review never disagree about what a conclusion means. It is a
#: report, never a state transition: `state` stays `needs_manual_review` forever,
#: because a human observation must not overwrite what the system could prove.
MANUAL_RESOLUTIONS: Final[tuple[str, ...]] = (
    "confirmed_written",
    "confirmed_not_written",
)


def _in_set(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ToolExecution(Base):
    """One attempt to cause one external side effect.

    `idempotency_key` is the primary key rather than a surrogate id: the whole
    contract is that one key means at most one external record, and making it
    the identity removes any way to end up with two rows for one key.
    """

    __tablename__ = "tool_executions"

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="prepared")
    #: Compare-and-swap guard. A resumed worker holding a stale version loses.
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: The Feishu client_token, persisted before the first submit so a retry
    #: after a lost response replays the same token instead of creating a
    #: second record.
    client_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    encrypted_payload: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    encrypted_result: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Single-worker lease for crash recovery. Durable, unlike a process lock.
    recovery_lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_lease_until: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    #: A person's report about the ledger, for an execution that ended at
    #: `needs_manual_review`. It is a flag beside the state, never instead of it:
    #: `state` still means what the *system* proved, and this records only what a
    #: human saw. It is what lets the observe alert stop once someone has looked.
    manual_resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_resolved_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )

    __table_args__ = (
        CheckConstraint(_in_set("state", EXECUTION_STATES), name="state"),
        CheckConstraint("state_version >= 1", name="state_version_positive"),
        CheckConstraint(
            "(recovery_lease_owner IS NULL) = (recovery_lease_until IS NULL)",
            name="lease_owner_and_expiry_together",
        ),
        # A row that reached the source must record when, so recovery can bound
        # how long it has been unresolved.
        CheckConstraint(
            "state IN ('prepared', 'failed_safe', 'cancelled_pre_submit') "
            "OR submitted_at IS NOT NULL",
            name="post_submit_states_record_submission_time",
        ),
        CheckConstraint(
            "manual_resolution IS NULL OR "
            + _in_set("manual_resolution", MANUAL_RESOLUTIONS),
            name="manual_resolution",
        ),
        # A conclusion and its timestamp are one fact; neither half may exist
        # alone, and only a review-parked execution may carry one.
        CheckConstraint(
            "(manual_resolution IS NULL) = (manual_resolved_at IS NULL)",
            name="manual_resolution_pairs_with_its_time",
        ),
        CheckConstraint(
            "manual_resolution IS NULL OR state = 'needs_manual_review'",
            name="manual_resolution_only_for_review",
        ),
        Index("ix_tool_executions_state", "state"),
    )


class ExternalReceipt(Base):
    """Proof from the fact source. Without a row here, nothing succeeded."""

    __tablename__ = "external_receipts"

    receipt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(
        ForeignKey("tool_executions.idempotency_key", ondelete="RESTRICT"),
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    table_kind: Mapped[str] = mapped_column(Text, nullable=False)
    record_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    #: Set only after the written fields are read back and compared.
    verified_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )

    execution: Mapped["ToolExecution"] = relationship()

    __table_args__ = (
        CheckConstraint(_in_set("table_kind", TABLE_KINDS), name="table_kind"),
        # One execution yields at most one record per table, so a replay that
        # created a second record would be rejected rather than recorded.
        UniqueConstraint(
            "idempotency_key", "table_kind", name="idempotency_key_table_kind"
        ),
    )


class SchemaSnapshot(Base):
    __tablename__ = "schema_snapshots"

    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ledger_year: Mapped[int] = mapped_column(Integer, nullable=False)
    config_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    validated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _in_set("status", SCHEMA_SNAPSHOT_STATUSES), name="status"
        ),
        Index("ix_schema_snapshots_ledger_year", "ledger_year"),
    )


class FxEvidence(Base):
    """What rate was used, from which provider, quoted on which date.

    Stored as text so the Decimal value survives exactly; a float column would
    quietly change the number that justified an amount.
    """

    __tablename__ = "fx_evidence"

    evidence_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(
        ForeignKey("tool_executions.idempotency_key", ondelete="RESTRICT"),
        nullable=False,
    )
    base_currency: Mapped[str] = mapped_column(Text, nullable=False)
    quote_currency: Mapped[str] = mapped_column(Text, nullable=False)
    rate: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    quote_date: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    execution: Mapped["ToolExecution"] = relationship()

    __table_args__ = (
        CheckConstraint("provider = 'frankfurter_ecb'", name="provider_is_pinned"),
        CheckConstraint("quote_currency = 'CNY'", name="quote_is_cny"),
    )


class DuplicateCheck(Base):
    """A same-day exact match handed to Henson for a decision.

    Candidate record ids and the display projection are sealed: they point at
    real ledger rows and contain personal fields, so neither belongs in
    plaintext audit storage.
    """

    __tablename__ = "duplicate_checks"

    check_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: The request that raised this check. It is how the Agent asks the control
    #: plane "was my write blocked, and by which check?" without the id ever
    #: travelling on the model-facing MCP result. Not unique: a blocked write
    #: creates no execution row, so the same key can legitimately be retried and
    #: raise another check.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    intent_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_candidate_record_ids: Mapped[dict[str, Any]] = mapped_column(
        EncryptedEnvelope, nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="awaiting_decision"
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            _in_set("status", DUPLICATE_CHECK_STATUSES), name="status"
        ),
        CheckConstraint(
            "(status = 'awaiting_decision') = (decided_at IS NULL)",
            name="decided_at_matches_status",
        ),
        Index("ix_duplicate_checks_idempotency_key", "idempotency_key"),
    )


class ResourceLock(Base):
    """A durable lease, used to serialise family fund updates.

    Taken and committed in a short transaction before any network call: holding
    a SQLite write transaction across HTTP would block every other writer for
    the length of a Feishu round trip.
    """

    __tablename__ = "resource_locks"

    lock_key: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    lease_until: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)


class AuditEvent(Base):
    """Hash-chained audit trail.

    The chain detects accidental tampering and gaps. Technical design 8.4 is
    explicit that it does not defend against an attacker who already holds root
    and the signing keys, so it is not treated as one.
    """

    __tablename__ = "audit_events"

    #: Database-assigned append order. Timestamps and UUIDs can collide or sort
    #: differently from insertion order, so neither is safe as a chain position.
    sequence: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_summary: Mapped[str] = mapped_column(Text, nullable=False)
    prev_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (Index("ix_audit_events_trace_id", "trace_id"),)


class AuditChainAnchor(Base):
    """Independent tail witness for detecting audit-table truncation.

    The hash links inside ``audit_events`` detect edits and middle deletions but
    cannot, by themselves, prove that the last row still exists: the remaining
    prefix is a valid chain. This singleton is updated in the same SQLite
    transaction as every append and witnesses the expected row count and tail.
    It is not a defence against an attacker rewriting the whole database; it is
    the missing protection against accidental or partial audit-table truncation.
    """

    __tablename__ = "audit_chain_anchor"

    anchor_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tail_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("anchor_id = 1", name="singleton"),
        CheckConstraint("event_count >= 1", name="event_count_positive"),
    )


class CalendarEvent(Base):
    """One event of the Apple-calendar mirror, per the calendar domain PRD.

    The iPhone owns the calendar; this row is what the phone last reported, so
    the table is named after that relationship: a mirror, not a second fact
    source. `(calendar_identifier, event_identifier)` is the composite
    identity because EventKit scopes `eventIdentifier` per calendar store
    source, and the pair is what an upsert arbitrates on.

    Sensitivity split: timestamps and identifiers are plaintext because the
    window filter needs a real index over them; title/notes/location are
    personal text and travel through restic backups, so they are sealed
    envelopes like every other business content in this database.
    """

    __tablename__ = "calendar_events"

    #: The AAD row identity for the three sealed columns. The composite
    #: business key is fine for lookups but two columns cannot name one AAD
    #: string, so the row carries a surrogate for sealing.
    row_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    calendar_identifier: Mapped[str] = mapped_column(Text, primary_key=True)
    event_identifier: Mapped[str] = mapped_column(Text, primary_key=True)
    #: Epoch seconds, UTC. Plaintext and indexed: the window filter and the
    #: sort order are the query's whole shape.
    start_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    title: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    notes: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    location: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    #: Tombstone. The row is kept so a late stale chunk cannot be mistaken for
    #: a new event, and queries exclude it by default.
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: The device's own per-event `last_modified` (EventKit-free approximation
    #: the device may still send). Arbitrates field merges *between* snapshots;
    #: never the sweep.
    last_modified_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The snapshot instant of the upload that last wrote this row. This is
    #: the sweep's arbiter: a row is "part of" snapshot N when this equals
    #: N's instant, and a tombstone's version is the deleting snapshot's
    #: instant, so only a newer snapshot's assertion clears it.
    snapshot_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    #: When this row last heard from the device. `data_as_of` is the max of it.
    synced_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    created_by_agent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Which enrolled device last uploaded this row.
    device_id: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("start_ts <= end_ts", name="start_before_or_equal_end"),
        Index("ix_calendar_events_start_ts", "start_ts"),
        Index("ix_calendar_events_end_ts", "end_ts"),
    )


class CalendarDeviceSync(Base):
    """One device's calendar-sync watermark, per the snapshot-versioned mirror.

    A row is written only when the device completes a whole window snapshot
    (`window_complete=true` on a batch whose `snapshot_as_of` is newer than
    the stored watermark). It answers two questions the event rows cannot:

    - **Arbitration**: a snapshot older than the watermark may upsert rows
      but may never tombstone anything — the sweep is a property of a
      *completed, current* snapshot, not of any single batch.
    - **Freshness**: `data_as_of` / `mirror_stale` read this table, so a
      partial upload (or a device that has never finished a snapshot) reads
      as honestly stale instead of freshly wrong. An empty window still
      completes, so an observed empty calendar is a real observation.
    """

    __tablename__ = "calendar_device_sync"

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: The `snapshot_as_of` instant of the newest completed snapshot.
    #: Epoch seconds, UTC — the version every sweep and freshness check
    #: arbitrates on.
    watermark_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    #: When this device last completed a snapshot (upload wall clock).
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
