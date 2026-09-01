"""State-machine tables — revision `0002_state_machine` (DAL-009).

The table set is not chosen; it is derived. Every spec in the frozen
`transition-spec-registry` declares an `atomic_write_set`, and the union of
those members across the 254 specs reachable at G1 is exactly what has to be
writable. Each member names a real change to a real row here — there is no
generic "write log", because a row that the code inserts merely because it was
told to would prove nothing about whether the business fact was recorded.

Several members act on the same entity at different points in its life
(`decision_create` / `decision_resolve` / `decision_consume` /
`decision_supersede`), which is why those are one table with a status rather
than four tables.

This revision re-cuts the `0002` boundary agreed under decision D1(B): the
entities here are the ones DAL-009's tests actually consume. `plan`, `task`,
`run`, `job`, `review`, `provider_attempt`, `artifact`, `test_receipt` and
`github_binding` are still absent, because nothing tests them yet — which was
the whole point of splitting by consuming task.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent_core.sqlite import UtcTimestamp

from personal_agent_dal.storage.models import Base, _hex_of_length, _in_set


#: `RecoveryCaseState`, contract §3.6. `verified` and `cancelled` are terminal.
RECOVERY_CASE_STATES: Final[tuple[str, ...]] = (
    "investigating",
    "awaiting_decision",
    "approved",
    "executing",
    "verifying",
    "verified",
    "blocked",
    "cancelled",
)

#: `ExternalEffectState`, contract §3.6.
EXTERNAL_EFFECT_STATES: Final[tuple[str, ...]] = (
    "intent_recorded",
    "claimed",
    "dispatch_started",
    "confirmed_completed",
    "confirmed_not_executed",
    "unknown",
    "reconciling",
)

#: A decision's life. `superseded` is not `resolved`: a card replaced by a newer
#: version was never acted on, and conflating the two would let a stale card
#: count as an answer.
DECISION_STATUSES: Final[tuple[str, ...]] = (
    "open",
    "resolved",
    "consumed",
    "superseded",
)

#: Decision Dock rank, contract §3.5. Fixed, and not re-derivable by a client.
DOCK_RANKS: Final[tuple[int, ...]] = (0, 1, 2, 3, 4)

#: Notification priority, contract §3.5.2. Independent of `dock_rank`: the rank
#: is display ordering, this is the notification strategy.
NOTIFICATION_PRIORITIES: Final[tuple[str, ...]] = ("immediate", "normal")

#: Notification batch states, contract §3.7.
NOTIFICATION_BATCH_STATES: Final[tuple[str, ...]] = (
    "open",
    "ready",
    "closed",
    "superseded",
    "cancelled",
)

#: Notification delivery states, contract §3.7. `claimed` is present here and
#: deliberately absent from `outbox_events.delivery_state`: delivery is its own
#: state machine, not the outbox's.
NOTIFICATION_DELIVERY_STATES: Final[tuple[str, ...]] = (
    "pending",
    "claimed",
    "delivering",
    "delivered",
    "retry_wait",
    "dead_letter",
    "cancelled",
)


class TransitionReceipt(Base):
    """One receipt per transition command, successful or refused.

    Refusals are **not** stored: every deny oracle in the registry carries an
    empty `allowed_write_set`, so a denied command leaves nothing behind and
    the receipt is only the caller's answer. The check constraint makes that
    structural rather than a convention a later handler could forget.

    `idempotency_key` is unique because a replayed transition command must
    return the original receipt instead of transitioning twice.
    """

    __tablename__ = "transition_receipts"

    receipt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_id: Mapped[str] = mapped_column(Text, nullable=False)
    command_type: Mapped[str] = mapped_column(Text, nullable=False)
    #: Null for a creation transition: `create_feature` has no prior state,
    #: and writing the string "none" would put a pseudo-state into a column
    #: whose other values are all real ones.
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_code: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    request_payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("receipt_code = 'APPLIED'", name="only_applied_persists"),
        CheckConstraint(
            "aggregate_type IN ('feature', 'recovery_case', 'external_effect')",
            name="aggregate_type",
        ),
        CheckConstraint(
            _hex_of_length("request_payload_sha256", 64, nullable=False),
            name="request_payload_sha256_hex",
        ),
        Index("ix_transition_receipts_aggregate", "aggregate_type", "aggregate_id"),
    )


class Decision(Base):
    """A structured decision the state machine stopped on.

    A decision is persisted in the same transaction as the stop (§3.5); the
    notification is only an outbox delivery and owns no state. `is_incident`
    marks the policy-incident decisions created by the unapproved observed
    paths, which block progress rather than asking a question.
    """

    __tablename__ = "decisions"

    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_incident: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    root_id: Mapped[str] = mapped_column(Text, nullable=False)
    safety_or_irreversible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    blocking_scope: Mapped[str] = mapped_column(Text, nullable=False)
    depends_on_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_priority: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(_in_set("status", DECISION_STATUSES), name="status"),
        CheckConstraint(
            "blocking_scope IN ('global', 'local', 'none')",
            name="blocking_scope",
        ),
        CheckConstraint(
            _in_set("notification_priority", NOTIFICATION_PRIORITIES),
            name="notification_priority",
        ),
        Index("ix_decisions_feature_id", "feature_id"),
    )


class DecisionCardProjection(Base):
    """The Decision Dock's current view of one decision version.

    Timeline history is immutable; this is the projection of the actionable
    frontier only (§3.5). The dock rank is stored, not computed by the client,
    because the server and iOS must not be able to define two orderings.
    """

    __tablename__ = "decision_card_projections"

    projection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("decisions.decision_id"), nullable=False
    )
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actionable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    display_state: Mapped[str] = mapped_column(Text, nullable=False)
    dock_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("dock_rank BETWEEN 0 AND 4", name="dock_rank"),
        UniqueConstraint(
            "decision_id", "decision_version", name="decision_id_version"
        ),
    )


class Approval(Base):
    """A consume-once authorisation bound to exact state and artifacts (§3.3).

    Consumption is a compare-and-swap on `consumed_by_command_id IS NULL`, so
    two commands racing for one approval produce exactly one winner and one
    `APPROVAL_INVALID`. An approval is not a bearer token and is never valid
    for a second business effect.
    """

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(Text, primary_key=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_feature_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_state: Mapped[str] = mapped_column(Text, nullable=False)
    state_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    replay_policy: Mapped[str] = mapped_column(Text, nullable=False)
    consumed_by_command_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("replay_policy = 'consume_once'", name="replay_policy"),
        CheckConstraint(
            "(consumed_by_command_id IS NULL) = (consumed_at IS NULL)",
            name="consumption_is_all_or_nothing",
        ),
        Index("ix_approvals_feature_id", "feature_id"),
    )


class ApprovalActionReceipt(Base):
    """Immutable proof that an approval was consumed by a specific action.

    This is the evidence a later out-of-band observation is checked against —
    `valid_from <= effect_at < expires_at` measured at the external fact's own
    timestamp, never at observation time. It authorises nothing further; it is
    not a capability and cannot be consumed again.
    """

    __tablename__ = "approval_action_receipts"

    receipt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    approval_id: Mapped[str] = mapped_column(
        ForeignKey("approvals.approval_id"), nullable=False, unique=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consumed_state_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    repository_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    pull_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    consumed_by_command_id: Mapped[str] = mapped_column(Text, nullable=False)
    consume_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    receipt_sha256: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(_hex_of_length("receipt_sha256", 64, nullable=False),
                        name="receipt_sha256_hex"),
    )


class Capability(Base):
    """A narrowly scoped, epoch-bound permission issued from one approval.

    Epochs are what make revocation real: bumping the feature's
    `capability_epoch` invalidates every capability issued under the old one
    without having to find and edit each row.
    """

    __tablename__ = "capabilities"

    capability_id: Mapped[str] = mapped_column(Text, primary_key=True)
    approval_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False)
    uses_consumed: Mapped[int] = mapped_column(Integer, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("uses_consumed >= 0 AND uses_consumed <= max_uses",
                        name="uses_within_max"),
        Index("ix_capabilities_feature_id", "feature_id"),
    )


class Lease(Base):
    """A worker's time-bounded claim on a job. A lease is not a success."""

    __tablename__ = "leases"

    lease_id: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        UniqueConstraint("job_id", "epoch", name="job_id_epoch"),
        Index("ix_leases_feature_id", "feature_id"),
    )


class ExternalEffect(Base):
    """One attempt to change something outside this database (§3.6).

    The unique `(effect_scope_key, remote_idempotency_key)` is the constraint
    that stops a retry from becoming a second real-world action: a rearm reuses
    this row and increments `attempt` rather than inventing a second key.
    """

    __tablename__ = "external_effects"

    effect_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    owner_aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    effect_scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    remote_idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    executor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    executor_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    capability_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    receipt_refs_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_read_refs_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "effect_scope_key", "remote_idempotency_key", name="scope_key_remote_key"
        ),
        CheckConstraint(_in_set("state", EXTERNAL_EFFECT_STATES), name="state"),
        CheckConstraint(
            "origin IN ('dal_dispatched', 'out_of_band_human')", name="origin"
        ),
        CheckConstraint(
            "owner_aggregate_type IN ('feature', 'recovery_case')",
            name="owner_aggregate_type",
        ),
        CheckConstraint(
            "origin <> 'out_of_band_human' OR ("
            "executor_id IS NULL AND executor_epoch IS NULL "
            "AND claim_expires_at IS NULL AND state = 'confirmed_completed')",
            name="observed_effects_have_no_executor",
        ),
        Index("ix_external_effects_owner", "owner_aggregate_type",
              "owner_aggregate_id"),
    )


class RecoveryCase(Base):
    """A compensation investigation. Compensation is not rollback (§3.6)."""

    __tablename__ = "recovery_cases"

    recovery_case_id: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(_in_set("state", RECOVERY_CASE_STATES), name="state"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "reason_code IS NULL OR reason_code IN ("
            "'RECOVERY_READBACK_UNKNOWN', 'RECOVERY_POLICY_STALE', "
            "'RECOVERY_EFFECT_UNKNOWN')",
            name="reason_code",
        ),
        Index("ix_recovery_cases_feature_id", "feature_id"),
    )


class RecoveryCaseSourceEffect(Base):
    """The effects a recovery case was opened for. Immutable once created."""

    __tablename__ = "recovery_case_source_effects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recovery_case_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_cases.recovery_case_id"), nullable=False
    )
    effect_id: Mapped[str] = mapped_column(Text, nullable=False)
    effect_version: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("recovery_case_id", "effect_id", name="case_effect"),
    )


class EvidenceRecord(Base):
    """A protected-evidence reference, never the evidence body itself.

    The body lives in the protected object store; what is kept here is the
    digest and the opaque locator, so a guard can compare fields against the
    resolved document without this database ever holding it.
    """

    __tablename__ = "evidence_records"

    evidence_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    protected_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('evidence', 'authoritative_receipt', "
            "'authoritative_post_read')",
            name="kind",
        ),
        CheckConstraint(_hex_of_length("payload_sha256", 64, nullable=False),
                        name="payload_sha256_hex"),
        Index("ix_evidence_records_subject", "subject_aggregate_type",
              "subject_aggregate_id"),
    )


class ImpactReport(Base):
    """What already happened outside, preserved across a cancel or a block.

    Cancellation stops what has not started; it never rewrites a fact. This
    table is where the inventory of already-real effects survives that.
    """

    __tablename__ = "impact_reports"

    impact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    impact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    effect_inventory_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('impact', 'impact_report')", name="kind"),
        CheckConstraint(_hex_of_length("impact_sha256", 64, nullable=False),
                        name="impact_sha256_hex"),
        Index("ix_impact_reports_feature_id", "feature_id"),
    )


class NotificationBatch(Base):
    """A fixed-window batch of normal-priority decisions, contract §3.5.2/§3.7.

    The window's `flush_at` is fixed at the first member's entry and never
    extended by later membership. An `immediate` decision closes the batch and
    flushes it at once; the batch itself never refuses, it only aggregates.
    """

    __tablename__ = "notification_batches"

    batch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    flush_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    maximum_items: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _in_set("state", NOTIFICATION_BATCH_STATES), name="state"
        ),
        UniqueConstraint("batch_id", "channel", "payload_sha256", name="batch_channel_payload"),
    )


class NotificationDelivery(Base):
    """One delivery of a notification batch, contract §3.7.

    Its state machine is independent of the outbox: a delivery is claimed
    (compare-and-swap on `claim_epoch`), marked `delivering`, and then either
    `delivered`, `retry_wait` (bounded backoff), `dead_letter` (attempt limit
    exhausted) or `cancelled` (every decision no longer valid).
    """

    __tablename__ = "notification_deliveries"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    batch_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    claim_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        UtcTimestamp, nullable=True
    )
    provider_receipt: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _in_set("state", NOTIFICATION_DELIVERY_STATES), name="state"
        ),
        CheckConstraint("claim_epoch >= 1", name="claim_epoch_positive"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        Index("ix_notification_deliveries_batch_id", "batch_id"),
    )


#: The one legal value of `commit_capabilities.action` (DAL-004 §5). A
#: one-time commit capability authorises exactly one candidate commit.
COMMIT_CAPABILITY_ACTIONS: Final[tuple[str, ...]] = ("commit_candidate",)

#: The frozen state set for the consume CAS (DAL-004 §5: sign, consume, and
#: receipt persistence all go through a capability version CAS). `issued` is
#: the only live state; `consumed` and `superseded` are terminal and refused
#: by the pure gate's liveness classes.
COMMIT_CAPABILITY_STATES: Final[tuple[str, ...]] = (
    "issued",
    "consumed",
    "superseded",
)


class CommitCapability(Base):
    """The persistent one-time commit capability row (DAL-031, R09-A3).

    The column set is the field-by-field mapping from
    `DAL_R09-A3_commit-capability_2026-08-31.md` §5 onto DAL-004 §5's
    persistent model. It extends what the pure gate's binding already
    validates (never loosens it) with exactly the fields that mapping
    declared missing: `action`, `repository_id`,
    `artifact_or_diff_sha256`, `policy_version`, `consumed_at` and a
    row-level `schema_version`.

    `state_version` is the CAS column. Every consume carries the version it
    expects to replace and moves `issued -> consumed` with the consuming
    identity in the same statement, so two racing consumes produce exactly
    one winner and one loser that re-reads a dead row. Consumption,
    the external-effect intent, the audit append and the outbox row are
    written in the caller's one transaction (DAL-004 §5: they must be
    atomic); this model only owns the capability row itself.
    """

    __tablename__ = "commit_capabilities"

    capability_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: The CAS column: the version the next mutation expects to replace.
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    approval_id: Mapped[str] = mapped_column(Text, nullable=False)
    feature_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    repository_id: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_paths_json: Mapped[str] = mapped_column(Text, nullable=False)
    refs: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The digest of the artifact or diff the capability was issued against.
    #: Distinct from `result_sha` (a git tree SHA); the two must never be
    #: conflated (evidence §5 mapping row `artifact_or_diff_sha256`).
    artifact_or_diff_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    base_sha: Mapped[str] = mapped_column(Text, nullable=False)
    result_sha: Mapped[str] = mapped_column(Text, nullable=False)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    capability_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False)
    uses_consumed: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    issue_idempotency_key: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    trailers_json: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consumed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _in_set("state", COMMIT_CAPABILITY_STATES), name="state"
        ),
        CheckConstraint(
            _in_set("action", COMMIT_CAPABILITY_ACTIONS), name="action"
        ),
        CheckConstraint("state_version >= 1", name="state_version_positive"),
        CheckConstraint(
            "uses_consumed >= 0 AND uses_consumed <= max_uses",
            name="uses_within_max",
        ),
        CheckConstraint("max_uses = 1", name="max_uses_is_one"),
        CheckConstraint(
            "(consumed_by IS NULL) = (consumed_at IS NULL)",
            name="consumption_is_all_or_nothing",
        ),
        CheckConstraint(
            "(consumed_by IS NULL) = (state = 'issued') OR "
            "(state = 'consumed' AND consumed_by IS NOT NULL)",
            name="state_matches_consumption",
        ),
        CheckConstraint(
            _hex_of_length("base_sha", 40, nullable=False), name="base_sha_hex"
        ),
        CheckConstraint(
            _hex_of_length("result_sha", 40, nullable=False), name="result_sha_hex"
        ),
        CheckConstraint(
            _hex_of_length("artifact_or_diff_sha256", 64, nullable=False),
            name="artifact_or_diff_sha256_hex",
        ),
        CheckConstraint(
            "lease_epoch >= 0 AND capability_epoch >= 0",
            name="epochs_non_negative",
        ),
        CheckConstraint("expires_at >= 0", name="expires_at_non_negative"),
        Index("ix_commit_capabilities_feature_id", "feature_id"),
    )
