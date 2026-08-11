"""DAL workflow tables — revision `0001_core_spine`.

The table set is derived in `docs/dal/DAL008_数据库Schema设计草案_v0.1.md` from
five obligations rather than from an entity list: one winner per concurrent
write (CAS `version`), one business effect per intent (`operation_receipts`),
facts that cannot be edited after the fact (append-only `audit_events`), no
sensitive plaintext at rest (AEAD envelope columns), and no silent deletion
(`retention_tombstones`).

Per decision D1 this revision carries only the spine the DAL-008 oracles
exercise plus the single aggregate they act on. The remaining §3.1 entities
land in `0002`/`0003`, each alongside the tests that consume them, so no field
is frozen into a migration before something checks it.

Two invariants are triggers rather than service-layer guards (decision D3):
`merged`/`deployed` may never become `cancelled`, and a terminal feature may
never change state. Both describe external facts that already happened, and a
guard that lives only in Python is one refactor away from not existing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DDL,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from personal_agent_core.sqlite import (
    NAMING_CONVENTION,
    EncryptedEnvelope,
    UtcTimestamp,
)


class Base(DeclarativeBase):
    """The DAL declarative base, on its own metadata.

    A DAL-specific `MetaData` is what keeps this schema from ever being merged
    into another service's autogenerate run. The naming convention is shared
    deliberately: constraint names that shift between runs make a rollback diff
    unreadable, and the convention is what holds them still.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


#: `dal.feature-state/1.0`, contract §2.2. Terminal: `completed`, `cancelled`.
FEATURE_STATES: Final[tuple[str, ...]] = (
    "intake",
    "planning",
    "awaiting_plan_review",
    "approved",
    "coding",
    "verifying",
    "reviewing",
    "fixing",
    "verified",
    "awaiting_merge",
    "merged",
    "deployed",
    "completed",
    "blocked_requirement",
    "blocked_usage",
    "blocked_auth",
    "blocked_test",
    "blocked_external_prerequisite",
    "blocked_unknown",
    "reconciliation_required",
    "needs_human",
    "paused",
    "cancelled",
)

#: States that MUST carry a checkpoint (contract §2.2). `reconciliation_required`
#: is deliberately absent: §2.2 names only `blocked_*`, `needs_human` and
#: `paused`, and widening the rule here would be inventing contract.
CHECKPOINT_REQUIRED_STATES: Final[tuple[str, ...]] = (
    "blocked_requirement",
    "blocked_usage",
    "blocked_auth",
    "blocked_test",
    "blocked_external_prerequisite",
    "blocked_unknown",
    "needs_human",
    "paused",
)

#: `dal.reason-code/1.0`: the 17 feature reasons of §2.2.1 plus the three
#: RecoveryCase reasons of §3.6, which travel on the feature they stopped.
FEATURE_REASON_CODES: Final[tuple[str, ...]] = (
    "REQUIREMENT_MISSING",
    "USAGE_LIMIT",
    "AUTH_REQUIRED",
    "TEST_BLOCKED",
    "EXTERNAL_PREREQUISITE",
    "TRANSIENT_RETRY_EXHAUSTED",
    "PROVIDER_CONTRACT_FAILURE",
    "POLICY_FAILURE",
    "BUDGET_LIMIT",
    "REVIEW_LOOP_LIMIT",
    "GIT_CONFLICT",
    "STATE_DRIFT",
    "POST_EFFECT_EXCEPTION",
    "RECOVERY_REQUIRED",
    "UNKNOWN_NO_EXTERNAL_INTENT",
    "EXTERNAL_RESULT_UNKNOWN",
    "USER_PAUSE",
    "RECOVERY_READBACK_UNKNOWN",
    "RECOVERY_POLICY_STALE",
    "RECOVERY_EFFECT_UNKNOWN",
)

#: Service-level operation events (decision G1(a)). These are **not** aggregate
#: `EventEnvelope` types: `database` is not an aggregate, and §3.4's frozen
#: enum covers aggregates only. Keeping them in their own namespace is what
#: stops §3.4's unknown-type quarantine from swallowing the service's own
#: bookkeeping.
OPERATION_EVENT_TYPES: Final[tuple[str, ...]] = (
    "database.migrated",
    "database.retention_applied",
    "database.encrypted_roundtrip",
)


def _in_set(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def _hex_of_length(column: str, length: int, *, nullable: bool) -> str:
    """A lowercase-hex digest check.

    Written as an explicit GLOB rather than a length test alone: a 64-character
    string of anything would otherwise satisfy a column the contract binds to a
    SHA-256, and a digest that is not a digest fails much later and much less
    legibly.
    """
    check = f"length({column}) = {length} AND {column} NOT GLOB '*[^0-9a-f]*'"
    return f"({column} IS NULL OR ({check}))" if nullable else f"({check})"


class Feature(Base):
    """The workflow aggregate, and the CAS root of the state machine.

    The column set is not a modelling choice: §3.2.1 fixes the exact fields
    that go into `state_sha256`, and the server recomputes that digest from
    these rows when it issues a decision projection and again when it consumes
    an approval. A column added, dropped or renamed here changes what the
    device signed against.
    """

    __tablename__ = "features"

    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: The CAS column. Every update carries the version it expects to replace.
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    state: Mapped[str] = mapped_column(Text, nullable=False)
    checkpoint_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_owner: Mapped[str | None] = mapped_column(Text, nullable=True)

    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    repository_id: Mapped[str] = mapped_column(Text, nullable=False)
    base_sha: Mapped[str] = mapped_column(Text, nullable=False)
    result_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_verified_sha: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision_frontier_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    capability_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    external_effect_inventory_sha256: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(_in_set("state", FEATURE_STATES), name="state"),
        CheckConstraint(
            f"reason_code IS NULL OR {_in_set('reason_code', FEATURE_REASON_CODES)}",
            name="reason_code",
        ),
        CheckConstraint(
            "reason_owner IS NULL OR reason_owner IN ('feature', 'recovery_case')",
            name="reason_owner",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            f"NOT ({_in_set('state', CHECKPOINT_REQUIRED_STATES)}) "
            "OR checkpoint_state IS NOT NULL",
            name="checkpoint_present_when_stopped",
        ),
        CheckConstraint(
            "checkpoint_state IS NULL OR checkpoint_state "
            "NOT IN ('completed', 'cancelled')",
            name="checkpoint_is_not_terminal",
        ),
        CheckConstraint(_hex_of_length("base_sha", 40, nullable=False),
                        name="base_sha_hex"),
        CheckConstraint(_hex_of_length("result_sha", 40, nullable=True),
                        name="result_sha_hex"),
        CheckConstraint(_hex_of_length("last_verified_sha", 40, nullable=True),
                        name="last_verified_sha_hex"),
        CheckConstraint(_hex_of_length("artifact_sha256", 64, nullable=True),
                        name="artifact_sha256_hex"),
        CheckConstraint(
            _hex_of_length("external_effect_inventory_sha256", 64, nullable=False),
            name="inventory_sha256_hex",
        ),
    )


class Event(Base):
    """Aggregate events, per §3.4's `EventEnvelope`.

    `encrypted_payload` is the service's one sealed column (decision D2/E1).
    Event payloads are where repository content and model output actually reach
    the database — plans, diffs and artifacts live in the protected object
    store, and every other column here is a digest or an opaque locator.
    `payload_sha256` is taken over the **plaintext** canonical bytes, so the
    digest stays comparable across a key rotation that changes every byte of
    the envelope.
    """

    __tablename__ = "events"

    #: Database-assigned append order. Timestamps collide and UUIDs do not
    #: sort, so neither can stand in for insertion order when a restart has to
    #: rebuild what happened in which order.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)

    command_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    causation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    encrypted_payload: Mapped[dict[str, Any] | None] = mapped_column(
        EncryptedEnvelope, nullable=True
    )
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "event_type",
            name="aggregate_version_event_type",
        ),
        CheckConstraint(
            "actor_type IN ('service', 'human', 'worker')", name="actor_type"
        ),
        CheckConstraint(_hex_of_length("payload_sha256", 64, nullable=False),
                        name="payload_sha256_hex"),
        Index("ix_events_occurred_at", "occurred_at"),
    )


class EventQuarantine(Base):
    """Unknown event types, isolated rather than applied (§3.4).

    §3.4 requires an unknown type to be persisted, alerted on, and kept away
    from the aggregate. Without a table that requirement is only a comment: the
    natural implementation of "reject unknown types" is to raise and lose the
    envelope, which is exactly the evidence an operator needs.
    """

    __tablename__ = "event_quarantine"

    quarantine_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    observed_event_type: Mapped[str] = mapped_column(Text, nullable=False)
    envelope_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    alerted_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(_hex_of_length("envelope_sha256", 64, nullable=False),
                        name="envelope_sha256_hex"),
    )


class OperationEvent(Base):
    """Service-level operation events (decision G1(a)).

    Separate from `events` because the subject is the service itself, not an
    aggregate: `database.migrated` has no `aggregate_version` to attach to and
    must not advance one. `detail` is plain JSON on purpose — a revision id, a
    cutoff and a row count carry nothing sensitive, and sealing them would make
    an operator's first diagnostic step require a key.
    """

    __tablename__ = "operation_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_event_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(_in_set("event_type", OPERATION_EVENT_TYPES),
                        name="event_type"),
    )


class OperationReceiptRow(Base):
    """The idempotency ledger: one row per applied operation.

    `receipt_code` is constrained to `APPLIED` alone, which is the structural
    form of the rule in §3 of the design draft: the frozen oracles give
    `cas_conflict` and `idempotency_unique` an empty write set, so a refusal
    leaves nothing behind. Widening this set is a migration and a deliberate
    decision, not something a handler can do by passing a different code.
    """

    __tablename__ = "operation_receipts"

    operation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    operation_spec_id: Mapped[str] = mapped_column(Text, nullable=False)
    command_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_source_type: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_code: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    request_payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    response_payload_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("receipt_code = 'APPLIED'", name="only_applied_persists"),
        CheckConstraint(
            _hex_of_length("request_payload_sha256", 64, nullable=False),
            name="request_payload_sha256_hex",
        ),
        CheckConstraint(
            _hex_of_length("response_payload_sha256", 64, nullable=True),
            name="response_payload_sha256_hex",
        ),
    )


class MigrationReceipt(Base):
    """What the migration runner observed, revision by revision.

    Alembic's `alembic_version` says where the schema is now; it does not say
    when it moved, from where, or whether the integrity check that followed
    passed. A restore drill needs the second question answered.
    """

    __tablename__ = "migration_receipts"

    migration_receipt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    from_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_revision: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    applied_by: Mapped[str] = mapped_column(Text, nullable=False)
    integrity_check_result: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_migration_receipts_applied_at", "applied_at"),
    )


class RetentionTombstone(Base):
    """Proof that a row was deleted, without keeping the row.

    `sealed_id` is a digest of the deleted row's table and identifier, never
    the identifier itself: the point of retention is that the data is gone, and
    a tombstone holding a readable key would quietly undo that. The digest is
    still enough to answer "was this specific record deleted, and when".
    """

    __tablename__ = "retention_tombstones"

    tombstone_id: Mapped[str] = mapped_column(Text, primary_key=True)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_id: Mapped[str] = mapped_column(Text, nullable=False)
    cutoff: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operation_receipts.operation_id"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("table_name", "sealed_id", name="table_name_sealed_id"),
        CheckConstraint(_hex_of_length("sealed_id", 64, nullable=False),
                        name="sealed_id_hex"),
    )


class OutboxEvent(Base):
    """Outbound notification intent, committed with the state that caused it.

    Delivery failure changes this row and nothing else: §3.7 is explicit that a
    failed notification must not roll back the state transition it announces.
    """

    __tablename__ = "outbox_events"

    outbox_id: Mapped[str] = mapped_column(Text, primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_state: Mapped[str] = mapped_column(Text, nullable=False)
    available_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "topic",
            name="aggregate_version_topic",
        ),
        CheckConstraint(
            "delivery_state IN ('pending', 'delivering', 'delivered', "
            "'retry_wait', 'dead_letter', 'cancelled')",
            name="delivery_state",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
    )


class AuditEvent(Base):
    """Hash-chained, append-only audit trail (decision D4).

    The chain format matches the Finance service's deliberately: a restore
    drill that had to carry two different verifiers for two audit tables would
    end up verifying neither. As there, the chain detects accidental tampering
    and gaps; it is not a defence against an attacker who already holds root.
    """

    __tablename__ = "audit_events"

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
    """Independent tail witness against audit truncation.

    The links inside `audit_events` cannot prove the last row still exists: a
    truncated prefix is a perfectly valid chain. This singleton is updated in
    the same transaction as every append and witnesses the row count and tail.
    """

    __tablename__ = "audit_chain_anchor"

    anchor_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tail_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (CheckConstraint("anchor_id = 1", name="singleton"),)


#: Triggers, in creation order. Held as SQL so `create_all` and the Alembic
#: migration install exactly the same ones: a database built one way and a
#: database built the other must not differ in what they refuse.
TRIGGER_STATEMENTS: Final[tuple[tuple[str, str], ...]] = (
    (
        "audit_events_no_update",
        "CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    ),
    (
        "audit_events_no_delete",
        "CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    ),
    (
        "features_terminal_state_is_final",
        "CREATE TRIGGER features_terminal_state_is_final BEFORE UPDATE ON features "
        "WHEN OLD.state IN ('completed', 'cancelled') AND NEW.state <> OLD.state "
        "BEGIN SELECT RAISE(ABORT, 'feature is in a terminal state'); END",
    ),
    (
        "features_irreversible_not_cancellable",
        "CREATE TRIGGER features_irreversible_not_cancellable BEFORE UPDATE "
        "ON features "
        "WHEN OLD.state IN ('merged', 'deployed') AND NEW.state = 'cancelled' "
        "BEGIN SELECT RAISE(ABORT, "
        "'a merged or deployed feature cannot be cancelled'); END",
    ),
)


for _table, _trigger_names in (
    (AuditEvent, ("audit_events_no_update", "audit_events_no_delete")),
    (
        Feature,
        (
            "features_terminal_state_is_final",
            "features_irreversible_not_cancellable",
        ),
    ),
):
    for _name in _trigger_names:
        _sql = dict(TRIGGER_STATEMENTS)[_name]
        event.listen(
            _table.__table__, "after_create", DDL(_sql).execute_if(dialect="sqlite")
        )
