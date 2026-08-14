"""The deterministic transition engine.

The engine is an interpreter over the frozen registry. It resolves the one spec
that matches a command exactly, checks the spec's own allowlists and guard, and
then performs precisely the `atomic_write_set` that spec declares — all inside
one transaction. It contains no knowledge of any individual transition, which
is what makes "the registry is an exhaustive allowlist" (§2.3.1) true rather
than aspirational.

Four properties are load-bearing:

- **A model never sets the next state.** The `to_state` comes from the resolved
  spec, never from the command. The command may only name a `command_type` and
  a target discriminator, and if no spec matches that exact tuple the answer is
  `ILLEGAL_TRANSITION`.
- **A refusal writes nothing.** Every deny path raises before any write, and
  the whole unit is one real transaction, so the rollback is the guarantee.
- **State-preserving transitions still take a version.** §2.3.1 is explicit
  that an action which leaves `state` unchanged must still CAS the aggregate
  version; treating it as "an ordinary write that needs no contract" is how a
  concurrent writer slips past.
- **Terminal beats everything.** A command against a `completed` or
  `cancelled` feature is refused before the registry is consulted, so no spec
  can accidentally provide an exit from a terminal state.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Final

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine.binding import (
    ArtifactReader,
    build_state_binding,
    validate_artifact_binding,
    validate_state_binding,
)
from personal_agent_dal.machine.guards import GuardFacts, evaluate_guard
from personal_agent_dal.machine.registry import (
    ResolutionKey,
    guard_registry,
    jcs_sha256,
    transition_registry,
)
from personal_agent_dal.machine.transition_types import ReceiptCodes, TransitionRefused
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import (
    Approval,
    ApprovalActionReceipt,
    Capability,
    Decision,
    DecisionCardProjection,
    EvidenceRecord,
    ExternalEffect,
    ImpactReport,
    Lease,
    RecoveryCase,
    TransitionReceipt,
)
from personal_agent_dal.storage.models import (
    CHECKPOINT_REQUIRED_STATES,
    Event,
    Feature,
    OutboxEvent,
)


POLICY_VERSION: Final[str] = "dal-policy/1.0"
SERVICE_ACTOR: Final[str] = "workflow-service"
EVENT_SCHEMA_VERSION: Final[str] = "dal.event/1.0"

#: Terminal states per aggregate. Checked before the registry, so no spec can
#: be written that leaves one.
TERMINAL_STATES: Final[dict[str, frozenset[str]]] = {
    "feature": frozenset({"completed", "cancelled"}),
    "recovery_case": frozenset({"verified", "cancelled"}),
    "external_effect": frozenset({"confirmed_completed"}),
}

#: Receipt schema by aggregate, per contract §2.1.
RECEIPT_SCHEMAS: Final[dict[str, str]] = {
    "feature": "dal.transition-receipt/1.0",
    "recovery_case": "dal.recovery-transition-receipt/1.0",
    "external_effect": "dal.external-effect-transition-receipt/1.0",
}

_AGGREGATE_MODELS: Final[dict[str, Any]] = {
    "feature": Feature,
    "recovery_case": RecoveryCase,
    "external_effect": ExternalEffect,
}
_AGGREGATE_PK: Final[dict[str, str]] = {
    "feature": "feature_id",
    "recovery_case": "recovery_case_id",
    "external_effect": "effect_id",
}


@dataclass(frozen=True)
class TransitionCommand:
    """One request to move an aggregate. Carries no target state of its own."""

    aggregate_type: str
    aggregate_id: str
    command_type: str
    command_parameters: dict[str, Any]
    actor_type: str
    evidence_source_types: tuple[str, ...]
    evidence_schema_versions: tuple[str, ...]
    decision_action: str | None
    reason_code: str | None
    #: `None` for a creation command: there is no version to compare against yet.
    expected_version: int | None
    idempotency_key: str
    #: The full evidence documents the trusted resolver collected, each carrying
    #: the fields the guard validates under the ``evidence.*`` namespace. The
    #: engine validates their schema (required fields present and non-degenerate)
    #: before the guard's semantic binding checks — a null receipt ID is a
    #: schema defect, not a semantic mismatch, and ``None == None`` would pass
    #: the guard's ``equals_field`` check.
    evidence_documents: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_fixture(cls, body: dict[str, Any], *, idempotency_key: str) -> "TransitionCommand":
        return cls(
            aggregate_type=body["aggregate_type"],
            aggregate_id=body["aggregate_id"],
            command_type=body["command_type"],
            command_parameters=body.get("command_parameters") or {},
            actor_type=body["actor_type"],
            evidence_source_types=tuple(body.get("evidence_source_types") or ()),
            evidence_schema_versions=tuple(body.get("evidence_schema_versions") or ()),
            decision_action=body.get("decision_action"),
            reason_code=body.get("reason_code"),
            expected_version=body["expected_version"],
            idempotency_key=idempotency_key,
            evidence_documents=tuple(body.get("evidence_documents") or ()),
        )


@dataclass(frozen=True)
class TransitionOutcome:
    """What one transition command did."""

    receipt_code: str
    receipt_schema: str
    writes: tuple[str, ...]
    events: tuple[str, ...]
    from_state: str
    to_state: str
    reason_code: str | None
    reason_owner: str | None
    spec_id: str | None
    #: True when nothing was applied because this exact command was already
    #: applied before (§2.6): the receipt is the original one.
    duplicate: bool = False


@dataclass
class ApplyContext:
    """Everything a write-set applier may touch."""

    session: Session
    spec: dict[str, Any]
    command: TransitionCommand
    aggregate_id: str
    from_state: str
    to_state: str
    aggregate_version: int
    now: datetime
    request_payload_sha256: str
    artifact_reader: ArtifactReader | None = None
    event_id: str = field(default_factory=new_id)
    scratch: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Write-set appliers. Uniform signature: (ctx) -> None.
# --------------------------------------------------------------------------


def _w_aggregate(ctx: ApplyContext) -> None:
    """CAS the aggregate: state, reason, checkpoint and version, together.

    When the spec's `from_state` is null the aggregate does not exist yet, so
    this inserts it at version 1 instead. The insert is still the only place a
    feature can come into being, which is what keeps `create_feature` inside
    the same allowlist as every other transition.
    """
    if ctx.from_state is None:
        _insert_aggregate(ctx)
        return
    model = _AGGREGATE_MODELS[ctx.spec["aggregate_type"]]
    table = model.__table__
    pk = table.c[_AGGREGATE_PK[ctx.spec["aggregate_type"]]]

    values: dict[str, Any] = {
        "state": ctx.to_state,
        "version": ctx.command.expected_version + 1,
        "updated_at": ctx.now,
    }
    reason = ctx.spec["result_reason_code"]
    values["reason_code"] = reason
    if ctx.spec["aggregate_type"] == "feature":
        values["reason_owner"] = ctx.spec["result_reason_owner"]
        values["checkpoint_state"] = _checkpoint_for(ctx)

    result = ctx.session.execute(
        update(table)
        .where(pk == ctx.aggregate_id)
        .where(table.c.version == ctx.command.expected_version)
        .values(**values)
    )
    if result.rowcount != 1:
        raise TransitionRefused(
            ReceiptCodes.VERSION_CONFLICT,
            "another writer moved the aggregate first",
        )


def _insert_aggregate(ctx: ApplyContext) -> None:
    """Bring a new feature into existence, at version 1."""
    ctx.session.add(
        Feature(
            feature_id=ctx.aggregate_id,
            schema_version="dal.feature-state/1.0",
            version=1,
            state=ctx.to_state,
            checkpoint_state=None,
            reason_code=ctx.spec["result_reason_code"],
            reason_owner=ctx.spec["result_reason_owner"],
            plan_version=None,
            artifact_sha256=None,
            repository_id="repo-placeholder",
            base_sha="0" * 40,
            result_sha=None,
            last_verified_sha=None,
            decision_frontier_version=1,
            policy_version=POLICY_VERSION,
            capability_epoch=1,
            external_effect_inventory_sha256=hashlib.sha256(b"").hexdigest(),
            trace_id=ctx.aggregate_id,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
    )
    ctx.session.flush()


def _owner_feature_id(ctx: ApplyContext) -> str:
    """The feature that owns this command's decisions, approvals and leases.

    A recovery case is its own aggregate but it is not its own owner: the
    decision that authorises it, the approval it consumes and the capabilities
    it revokes all belong to the feature the case was opened for. Looking them
    up by the recovery case's id would find nothing and refuse a legitimate
    command.
    """
    if ctx.spec["aggregate_type"] == "feature":
        return ctx.aggregate_id
    cached = ctx.scratch.get("owner_feature_id")
    if cached is not None:
        return cached
    if ctx.spec["aggregate_type"] == "recovery_case":
        table = RecoveryCase.__table__
        owner = ctx.session.execute(
            select(table.c.feature_id).where(
                table.c.recovery_case_id == ctx.aggregate_id
            )
        ).scalar_one_or_none()
    else:
        table = ExternalEffect.__table__
        owner = ctx.session.execute(
            select(table.c.owner_aggregate_id).where(
                table.c.effect_id == ctx.aggregate_id
            )
        ).scalar_one_or_none()
    if owner is None:
        raise TransitionRefused(
            ReceiptCodes.ILLEGAL_TRANSITION, "aggregate has no owning feature"
        )
    ctx.scratch["owner_feature_id"] = owner
    return owner


def _companion_for(ctx: ApplyContext, aggregate_type: str) -> dict[str, Any] | None:
    """The companion transition of this root spec for one aggregate type."""
    for companion in ctx.spec["atomic_companion_transitions"]:
        if companion["aggregate_type"] == aggregate_type:
            return companion
    return None


def _checkpoint_for(ctx: ApplyContext) -> str | None:
    """The checkpoint to record, per §2.2 and §2.3.

    Entering a stopped state records where the work actually was. If the
    feature was *already* stopped, the existing checkpoint is kept: a
    checkpoint of `blocked_usage` would be an unusable resume target, and the
    contract requires the pre-block non-terminal state.
    """
    if ctx.to_state not in CHECKPOINT_REQUIRED_STATES:
        return None
    if ctx.from_state in CHECKPOINT_REQUIRED_STATES:
        return ctx.scratch.get("existing_checkpoint") or ctx.from_state
    return ctx.from_state


def _w_business_event(ctx: ApplyContext) -> None:
    payload = {"spec_id": ctx.spec["spec_id"], "to_state": ctx.to_state}
    ctx.session.add(
        Event(
            event_id=ctx.event_id,
            schema_version=EVENT_SCHEMA_VERSION,
            event_type=ctx.spec["event_type"],
            aggregate_type=ctx.spec["aggregate_type"],
            aggregate_id=ctx.aggregate_id,
            aggregate_version=(ctx.command.expected_version or 0) + 1,
            command_id=ctx.command.idempotency_key,
            causation_id=None,
            correlation_id=ctx.aggregate_id,
            actor_type=ctx.command.actor_type,
            actor_id=SERVICE_ACTOR,
            occurred_at=ctx.now,
            encrypted_payload=None,
            payload_sha256=hashlib.sha256(
                canonical_json(payload).encode("utf-8")
            ).hexdigest(),
        )
    )


def _w_transition_receipt(ctx: ApplyContext) -> None:
    ctx.session.add(
        TransitionReceipt(
            receipt_id=new_id(),
            idempotency_key=ctx.command.idempotency_key,
            aggregate_type=ctx.spec["aggregate_type"],
            aggregate_id=ctx.aggregate_id,
            aggregate_version=(ctx.command.expected_version or 0) + 1,
            spec_id=ctx.spec["spec_id"],
            command_type=ctx.command.command_type,
            from_state=ctx.from_state,
            to_state=ctx.to_state,
            receipt_code=ReceiptCodes.APPLIED,
            receipt_schema_version=ctx.spec["success_receipt_schema"],
            request_payload_sha256=ctx.request_payload_sha256,
            event_id=ctx.event_id,
            recorded_at=ctx.now,
        )
    )


def _w_audit(ctx: ApplyContext) -> None:
    append_audit_event(
        ctx.session,
        event_id=new_id(),
        trace_id=ctx.aggregate_id,
        event_type=ctx.spec["event_type"],
        # Spec id and states only. No evidence body, no payload, no identifier
        # beyond the aggregate's own opaque id.
        redacted_summary=f"{ctx.spec['spec_id']}: {ctx.from_state} -> {ctx.to_state}",
        now=ctx.now,
    )


def _w_notification_outbox(ctx: ApplyContext) -> None:
    ctx.session.add(
        OutboxEvent(
            outbox_id=new_id(),
            aggregate_type=ctx.spec["aggregate_type"],
            aggregate_id=ctx.aggregate_id,
            aggregate_version=(ctx.command.expected_version or 0) + 1,
            topic=ctx.spec["event_type"],
            payload_sha256=ctx.request_payload_sha256,
            delivery_state="pending",
            available_at=ctx.now,
            attempt_count=0,
            created_at=ctx.now,
        )
    )


def _new_decision(ctx: ApplyContext, *, incident: bool) -> Decision:
    decision_id = new_id()
    feature = ctx.session.execute(
        select(Feature).where(Feature.feature_id == _owner_feature_id(ctx))
    ).scalar_one()
    state_sha256 = jcs_sha256(build_state_binding(feature))
    decision = Decision(
        decision_id=decision_id,
        feature_id=_owner_feature_id(ctx),
        decision_version=1,
        action=ctx.spec["requires_decision_action"],
        reason_code=ctx.spec["result_reason_code"],
        status="open",
        priority=0 if incident else 4,
        artifact_sha256=feature.artifact_sha256,
        state_sha256=state_sha256,
        is_incident=incident,
        root_id=decision_id,
        safety_or_irreversible=incident,
        blocking_scope="global" if incident else "none",
        depends_on_json="[]",
        expires_at=ctx.now + timedelta(minutes=15),
        superseded_by=None,
        # §3.5.2: notification priority is independent of dock_rank. An
        # incident decision is safety_or_irreversible (or globally blocking),
        # so it is `immediate`; a normal decision batches.
        notification_priority="immediate" if incident else "normal",
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    ctx.session.add(decision)
    ctx.session.flush()
    ctx.scratch["decision_id"] = decision.decision_id
    return decision


def _w_decision_create(ctx: ApplyContext) -> None:
    _new_decision(ctx, incident=False)


def _w_decision(ctx: ApplyContext) -> None:
    _new_decision(ctx, incident=False)


def _w_incident_decision(ctx: ApplyContext) -> None:
    _new_decision(ctx, incident=True)


def _open_decision(ctx: ApplyContext) -> Decision:
    """The decision this command acts on. Its absence is a refusal, not a no-op."""
    table = Decision.__table__
    decision_id = ctx.session.execute(
        select(table.c.decision_id)
        .where(table.c.feature_id == _owner_feature_id(ctx))
        .where(table.c.status == "open")
        .order_by(table.c.created_at)
        .limit(1)
    ).scalar_one_or_none()
    if decision_id is None:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED, "no open decision to act on"
        )
    return ctx.session.get(Decision, decision_id)


def _decision_for_action(ctx: ApplyContext) -> Decision:
    """Resolve the exact client-observed decision, never an arbitrary open row."""

    decision_id = ctx.command.command_parameters.get("decision_id")
    submitted_version = ctx.command.command_parameters.get(
        "submitted_decision_version"
    )
    if not isinstance(decision_id, str) or not decision_id:
        raise TransitionRefused(
            ReceiptCodes.DECISION_STALE, "decision identity is required"
        )
    if not isinstance(submitted_version, int) or isinstance(submitted_version, bool):
        raise TransitionRefused(
            ReceiptCodes.DECISION_STALE, "decision version is required"
        )
    decision = ctx.session.execute(
        select(Decision).where(
            Decision.decision_id == decision_id,
            Decision.feature_id == _owner_feature_id(ctx),
        )
    ).scalar_one_or_none()
    if decision is None or decision.status != "open":
        raise TransitionRefused(
            ReceiptCodes.DECISION_STALE, "decision is not the current open decision"
        )
    if decision.decision_version != submitted_version:
        raise TransitionRefused(
            ReceiptCodes.DECISION_STALE,
            f"decision version {decision.decision_version} != "
            f"submitted {submitted_version}",
        )
    return decision


def _set_decision_status(ctx: ApplyContext, status: str) -> None:
    decision = _open_decision(ctx)
    decision.status = status
    decision.updated_at = ctx.now
    ctx.scratch["decision_id"] = decision.decision_id
    ctx.session.flush()


def _w_decision_resolve(ctx: ApplyContext) -> None:
    decision = _decision_for_action(ctx)
    if decision.expires_at is not None and ctx.now >= decision.expires_at:
        raise TransitionRefused(ReceiptCodes.DECISION_STALE, "decision has expired")
    decision.status = "resolved"
    decision.updated_at = ctx.now
    ctx.scratch["decision_id"] = decision.decision_id
    ctx.session.flush()


def _w_decision_consume(ctx: ApplyContext) -> None:
    _set_decision_status(ctx, "consumed")


def _w_decision_supersede(ctx: ApplyContext) -> None:
    _set_decision_status(ctx, "superseded")


def _w_decision_projection(ctx: ApplyContext) -> None:
    decision_id = ctx.scratch.get("decision_id")
    if decision_id is None:
        decision_id = _open_decision(ctx).decision_id
    decision = ctx.session.get(Decision, decision_id)
    projection = ctx.session.execute(
        select(DecisionCardProjection).where(
            DecisionCardProjection.decision_id == decision_id,
            DecisionCardProjection.decision_version == decision.decision_version,
        )
    ).scalar_one_or_none()
    actionable = decision.status == "open"
    if projection is None:
        ctx.session.add(
            DecisionCardProjection(
                projection_id=new_id(),
                decision_id=decision_id,
                decision_version=decision.decision_version,
                projection_version=1,
                actionable=actionable,
                display_state=ctx.to_state,
                dock_rank=4,
                created_at=ctx.now,
            )
        )
        return
    projection.projection_version += 1
    projection.actionable = actionable
    projection.display_state = ctx.to_state
    ctx.session.flush()


def _w_decision_action_receipt(ctx: ApplyContext) -> None:
    """The receipt that a decision action was taken. Recorded as evidence."""
    ctx.session.add(
        EvidenceRecord(
            evidence_id=new_id(),
            kind="evidence",
            subject_aggregate_type=ctx.spec["aggregate_type"],
            subject_aggregate_id=ctx.aggregate_id,
            evidence_schema_version="dal.decision-action-receipt/1.0",
            payload_sha256=ctx.request_payload_sha256,
            protected_ref=None,
            recorded_at=ctx.now,
        )
    )


def _w_approval_record(ctx: ApplyContext) -> None:
    # A command that names an existing `approval_id` acts on that approval;
    # recording a fresh unconsumed one would shadow it, and the subsequent
    # `approval_consume` CAS would then consume the new row and report
    # success against an approval that was already consumed, revoked or
    # expired. The named row is validated by `approval_consume`; nothing is
    # recorded here.
    named = ctx.command.command_parameters.get("approval_id")
    if named is not None:
        ctx.scratch["approval_id"] = named
        return
    decision = ctx.session.get(Decision, ctx.scratch.get("decision_id"))
    approval = Approval(
        approval_id=new_id(),
        action=ctx.spec["requires_decision_action"] or ctx.command.command_type,
        feature_id=_owner_feature_id(ctx),
        decision_id=decision.decision_id if decision is not None else None,
        decision_version=decision.decision_version if decision is not None else None,
        expected_feature_version=ctx.command.expected_version,
        expected_state=ctx.from_state,
        state_sha256=ctx.scratch["bound_state_sha256"],
        artifact_sha256=ctx.scratch.get("bound_artifact_sha256"),
        device_id="registered-device",
        subject_id="single-user",
        valid_from=ctx.now,
        expires_at=ctx.now + timedelta(minutes=15),
        idempotency_key=f"{ctx.command.idempotency_key}:approval",
        policy_version=POLICY_VERSION,
        replay_policy="consume_once",
        consumed_by_command_id=None,
        consumed_at=None,
        recorded_at=ctx.now,
    )
    ctx.session.add(approval)
    ctx.session.flush()
    ctx.scratch["approval_id"] = approval.approval_id


def _w_approval_consume(ctx: ApplyContext) -> None:
    """Consume-once, by CAS on `consumed_by_command_id IS NULL` (§3.3).

    A revoked or expired approval is ``APPROVAL_INVALID``, not
    ``POLICY_DENIED``: the caller is allowed to ask, but the approval itself
    is bad. An already-consumed approval is the same — a concurrent consumer
    won the race.
    """
    table = Approval.__table__
    approval_id = ctx.scratch.get("approval_id")
    # The command may name a specific approval to consume; otherwise the
    # oldest unconsumed one is picked.
    if approval_id is None:
        approval_id = ctx.command.command_parameters.get("approval_id")
    if approval_id is None:
        approval_id = ctx.session.execute(
            select(table.c.approval_id)
            .where(table.c.feature_id == _owner_feature_id(ctx))
            .where(table.c.consumed_by_command_id.is_(None))
            .order_by(table.c.recorded_at)
            .limit(1)
        ).scalar_one_or_none()
    if approval_id is None:
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID, "no unconsumed approval to consume"
        )

    # Check the approval's state before the CAS: an expired or already-
    # consumed approval is invalid even if the CAS would otherwise find it.
    # Revocation is modelled as consumption (a revoked approval has its
    # `consumed_by_command_id` set to the revocation marker), so the
    # consumed check covers the revoke_race scenario too.
    row = ctx.session.execute(
        select(
            table.c.consumed_by_command_id,
            table.c.expires_at,
        )
        .where(table.c.approval_id == approval_id)
        .where(table.c.feature_id == _owner_feature_id(ctx))
    ).first()
    if row is not None:
        consumed_by, expires_at = row
        if expires_at is not None and ctx.now > expires_at:
            raise TransitionRefused(
                ReceiptCodes.APPROVAL_INVALID, "approval has expired"
            )
        if consumed_by is not None:
            raise TransitionRefused(
                ReceiptCodes.APPROVAL_INVALID, "approval was already consumed"
            )
    result = ctx.session.execute(
        update(table)
        .where(table.c.approval_id == approval_id)
        .where(table.c.consumed_by_command_id.is_(None))
        .values(
            consumed_by_command_id=ctx.command.idempotency_key, consumed_at=ctx.now
        )
    )
    if result.rowcount != 1:
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID, "approval was already consumed"
        )
    ctx.scratch["approval_id"] = approval_id


def _approval_for_binding(ctx: ApplyContext) -> Approval:
    """Resolve the approval that will be consumed, without mutating it."""

    table = Approval.__table__
    approval_id = ctx.command.command_parameters.get("approval_id")
    query = select(Approval).where(
        table.c.feature_id == _owner_feature_id(ctx),
        table.c.consumed_by_command_id.is_(None),
    )
    if approval_id is not None:
        query = query.where(table.c.approval_id == approval_id)
    else:
        query = query.order_by(table.c.recorded_at).limit(1)
    approval = ctx.session.execute(query).scalar_one_or_none()
    if approval is None:
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID, "no unconsumed approval to validate"
        )
    return approval


def _validate_bound_authority(ctx: ApplyContext, declared: set[str]) -> None:
    """Recompute state/artifact authority before any transition write occurs."""

    if not declared.intersection({"decision_resolve", "approval_consume"}):
        return

    feature = ctx.session.execute(
        select(Feature).where(Feature.feature_id == _owner_feature_id(ctx))
    ).scalar_one()
    current_state_binding = build_state_binding(feature)
    current_state_sha256 = jcs_sha256(current_state_binding)
    ctx.scratch["bound_state_sha256"] = current_state_sha256
    ctx.scratch["bound_artifact_sha256"] = feature.artifact_sha256
    observed_state_sha256 = ctx.command.command_parameters.get(
        "observed_state_sha256"
    )

    if "decision_resolve" in declared:
        decision = _decision_for_action(ctx)
        if decision.state_sha256 is None:
            raise TransitionRefused(
                ReceiptCodes.DECISION_STALE,
                "decision has no protected state binding",
            )
        validate_state_binding(
            current_binding=current_state_binding,
            protected_binding_sha256=decision.state_sha256,
            observed_binding_sha256=observed_state_sha256,
        )

    if "approval_consume" not in declared:
        return
    approval_id = ctx.command.command_parameters.get("approval_id")
    approval = _approval_for_binding(ctx) if approval_id is not None else None
    if approval is None:
        # This command carries the human approval that will be recorded and
        # consumed in the same transaction. Bind that new row to the exact
        # pre-transition state the device observed.
        validate_state_binding(
            current_binding=current_state_binding,
            protected_binding_sha256=current_state_sha256,
            observed_binding_sha256=observed_state_sha256,
        )
    else:
        if approval.state_sha256 is None:
            raise TransitionRefused(
                ReceiptCodes.APPROVAL_INVALID,
                "approval has no protected state binding",
            )
        validate_state_binding(
            current_binding=current_state_binding,
            protected_binding_sha256=approval.state_sha256,
            observed_binding_sha256=observed_state_sha256,
        )

    current_artifact_sha256 = feature.artifact_sha256
    protected_artifact_sha256 = (
        approval.artifact_sha256 if approval is not None else current_artifact_sha256
    )
    if current_artifact_sha256 is None and protected_artifact_sha256 is None:
        return
    if (
        current_artifact_sha256 is None
        or protected_artifact_sha256 is None
        or not hmac.compare_digest(
            current_artifact_sha256, protected_artifact_sha256
        )
    ):
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID,
            "approval artifact digest is not current",
        )
    if ctx.artifact_reader is None:
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID,
            "protected artifact reader is unavailable",
        )
    try:
        artifact = ctx.artifact_reader(protected_artifact_sha256)
    except Exception as error:
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID,
            f"protected artifact read failed: {type(error).__name__}",
        ) from None
    validate_artifact_binding(
        current_artifact=artifact,
        protected_binding_sha256=protected_artifact_sha256,
        observed_binding_sha256=ctx.command.command_parameters.get(
            "observed_artifact_sha256"
        ),
    )


def _w_approval_action_receipt(ctx: ApplyContext) -> None:
    approval_id = ctx.scratch.get("approval_id")
    if approval_id is None:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED, "an action receipt needs a consumed approval"
        )
    body = {
        "approval_id": approval_id,
        "action": ctx.spec["requires_decision_action"],
        "feature_id": _owner_feature_id(ctx),
        "spec_id": ctx.spec["spec_id"],
    }
    ctx.session.add(
        ApprovalActionReceipt(
            receipt_id=new_id(),
            approval_id=approval_id,
            action=ctx.spec["requires_decision_action"] or ctx.command.command_type,
            feature_id=_owner_feature_id(ctx),
            decision_id=ctx.scratch.get("decision_id"),
            decision_version=1,
            consumed_state_sha256=None,
            repository_id=None,
            pull_request_id=None,
            head_sha=None,
            result_sha=None,
            device_id="registered-device",
            subject_id="single-user",
            policy_version=POLICY_VERSION,
            valid_from=ctx.now,
            expires_at=ctx.now + timedelta(minutes=15),
            consumed_by_command_id=ctx.command.idempotency_key,
            consume_event_id=ctx.event_id,
            recorded_at=ctx.now,
            receipt_sha256=hashlib.sha256(
                canonical_json(body).encode("utf-8")
            ).hexdigest(),
        )
    )


def _w_approval_action_receipt_ref(ctx: ApplyContext) -> None:
    """A reference to an already-issued action receipt, as bound evidence."""
    ctx.session.add(
        EvidenceRecord(
            evidence_id=new_id(),
            kind="authoritative_receipt",
            subject_aggregate_type=ctx.spec["aggregate_type"],
            subject_aggregate_id=ctx.aggregate_id,
            evidence_schema_version="dal.approval-action-receipt/1.0",
            payload_sha256=ctx.request_payload_sha256,
            protected_ref=None,
            recorded_at=ctx.now,
        )
    )


def _w_capability_epoch_increment(ctx: ApplyContext) -> None:
    """Revoke every outstanding capability at once, by moving the epoch."""
    table = Feature.__table__
    ctx.session.execute(
        update(table)
        .where(table.c.feature_id == _owner_feature_id(ctx))
        .values(capability_epoch=table.c.capability_epoch + 1)
    )


def _w_capability_issue(ctx: ApplyContext) -> None:
    capability = Capability(
        capability_id=new_id(),
        approval_id=ctx.scratch.get("approval_id"),
        feature_id=_owner_feature_id(ctx),
        action=ctx.spec["requires_decision_action"] or ctx.command.command_type,
        scope=ctx.spec["spec_id"],
        epoch=1,
        expires_at=ctx.now + timedelta(minutes=15),
        max_uses=1,
        uses_consumed=0,
        revoked_at=None,
        created_at=ctx.now,
    )
    ctx.session.add(capability)
    ctx.session.flush()
    ctx.scratch["capability_id"] = capability.capability_id


def _w_capability_consume(ctx: ApplyContext) -> None:
    table = Capability.__table__
    capability_id = ctx.scratch.get("capability_id") or ctx.session.execute(
        select(table.c.capability_id)
        .where(table.c.feature_id == _owner_feature_id(ctx))
        .where(table.c.revoked_at.is_(None))
        .where(table.c.uses_consumed < table.c.max_uses)
        .limit(1)
    ).scalar_one_or_none()
    if capability_id is None:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED, "no usable capability to consume"
        )
    result = ctx.session.execute(
        update(table)
        .where(table.c.capability_id == capability_id)
        .where(table.c.uses_consumed < table.c.max_uses)
        .values(uses_consumed=table.c.uses_consumed + 1)
    )
    if result.rowcount != 1:
        raise TransitionRefused(ReceiptCodes.POLICY_DENIED, "capability exhausted")


def _w_capability_revoke(ctx: ApplyContext) -> None:
    table = Capability.__table__
    result = ctx.session.execute(
        update(table)
        .where(table.c.feature_id == _owner_feature_id(ctx))
        .where(table.c.revoked_at.is_(None))
        .values(revoked_at=ctx.now)
    )
    if result.rowcount == 0:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED, "no live capability to revoke"
        )


def _w_lease_issue(ctx: ApplyContext) -> None:
    ctx.session.add(
        Lease(
            lease_id=new_id(),
            feature_id=_owner_feature_id(ctx),
            job_id=f"job-{ctx.event_id}",
            worker_id="home-mac-worker",
            epoch=1,
            expires_at=ctx.now + timedelta(minutes=15),
            heartbeat_at=None,
            revoked_at=None,
            created_at=ctx.now,
        )
    )


def _w_lease_revoke(ctx: ApplyContext) -> None:
    table = Lease.__table__
    result = ctx.session.execute(
        update(table)
        .where(table.c.feature_id == _owner_feature_id(ctx))
        .where(table.c.revoked_at.is_(None))
        .values(revoked_at=ctx.now)
    )
    if result.rowcount == 0:
        raise TransitionRefused(ReceiptCodes.POLICY_DENIED, "no live lease to revoke")


def _effect_for(ctx: ApplyContext) -> ExternalEffect:
    """The effect this command is acting on.

    Preference order matters: an effect this very command created, then one
    owned by the root aggregate, and only then one owned by the root's feature.
    A recovery case owns its own effects, so resolving straight to the feature
    would miss them and refuse a legitimate close.
    """
    scratched = ctx.scratch.get("effect_id")
    if scratched is not None:
        return ctx.session.get(ExternalEffect, scratched)
    table = ExternalEffect.__table__
    effect_id = ctx.session.execute(
        select(table.c.effect_id)
        .where(table.c.owner_aggregate_type == ctx.spec["aggregate_type"])
        .where(table.c.owner_aggregate_id == ctx.aggregate_id)
        .order_by(table.c.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if effect_id is None:
        effect_id = ctx.session.execute(
            select(table.c.effect_id)
            .where(table.c.owner_aggregate_id == _owner_feature_id(ctx))
            .order_by(table.c.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    if effect_id is None:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED, "no external effect for this owner"
        )
    return ctx.session.get(ExternalEffect, effect_id)


def _new_effect(ctx: ApplyContext, *, state: str, origin: str) -> ExternalEffect:
    """Create the command's effect, once.

    Several write sets name the effect more than once -- `external_effect`
    alongside `external_effect_intent`, or alongside `observed_external_effect`
    -- because they describe one act from two angles. Creating a row per member
    would collide on `(effect_scope_key, remote_idempotency_key)`, which is the
    very constraint that keeps one intent from becoming two real actions.
    """
    existing = ctx.scratch.get("effect_id")
    if existing is not None:
        return ctx.session.get(ExternalEffect, existing)
    effect = ExternalEffect(
        effect_id=new_id(),
        version=1,
        origin=origin,
        owner_aggregate_type=ctx.spec["aggregate_type"],
        owner_aggregate_id=ctx.aggregate_id,
        effect_scope_key=f"{ctx.aggregate_id}:{ctx.spec['spec_id']}",
        remote_idempotency_key=f"{ctx.command.idempotency_key}:effect",
        target_fingerprint=ctx.request_payload_sha256,
        state=state,
        attempt=1,
        executor_id=None if origin == "out_of_band_human" else "workflow-service",
        executor_epoch=None if origin == "out_of_band_human" else 1,
        claim_expires_at=None,
        capability_id=ctx.scratch.get("capability_id"),
        capability_epoch=None,
        receipt_refs_sha256=None,
        post_read_refs_sha256=None,
        impact_sha256=None,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    ctx.session.add(effect)
    ctx.session.flush()
    ctx.scratch["effect_id"] = effect.effect_id
    return effect


def _w_external_effect(ctx: ApplyContext) -> None:
    """The effect row itself.

    For an `external_effect` spec the effect *is* the aggregate, so this is the
    CAS that moves its lifecycle state -- `intent_recorded -> claimed ->
    dispatch_started` and the reconciliation edges. For a feature or recovery
    spec the same member means "bring an effect into existence" instead. One
    member, two roles, decided by the spec rather than by the caller.
    """
    if ctx.spec["aggregate_type"] == "external_effect":
        table = ExternalEffect.__table__
        result = ctx.session.execute(
            update(table)
            .where(table.c.effect_id == ctx.aggregate_id)
            .where(table.c.version == ctx.command.expected_version)
            .values(
                state=ctx.to_state,
                version=ctx.command.expected_version + 1,
                updated_at=ctx.now,
            )
        )
        if result.rowcount != 1:
            raise TransitionRefused(
                ReceiptCodes.VERSION_CONFLICT, "effect moved by another writer"
            )
        ctx.scratch["effect_id"] = ctx.aggregate_id
        ctx.scratch["effect_from_state"] = ctx.from_state
        ctx.scratch["effect_version"] = ctx.command.expected_version + 1
        return

    companion = _companion_for(ctx, "external_effect")
    if companion is not None and companion["from_state"] is not None:
        # The companion closes an effect that already exists; it must never
        # create a second one. §3.6 binds the companion's owner to the root
        # aggregate, so the effect is looked up by that owner.
        effect = _effect_owned_by_root(ctx, companion["from_state"])
        ctx.scratch["effect_from_state"] = effect.state
        effect.state = companion["to_state"]
        effect.version += 1
        effect.updated_at = ctx.now
        ctx.session.flush()
        ctx.scratch["effect_id"] = effect.effect_id
        ctx.scratch["effect_version"] = effect.version
        return
    _new_effect(ctx, state="intent_recorded", origin="dal_dispatched")
    ctx.scratch["effect_from_state"] = None
    ctx.scratch["effect_version"] = 1


def _effect_owned_by_root(ctx: ApplyContext, from_state: str) -> ExternalEffect:
    """The root aggregate's own effect in `from_state`. Owner binding is exact."""
    table = ExternalEffect.__table__
    effect_id = ctx.session.execute(
        select(table.c.effect_id)
        .where(table.c.owner_aggregate_type == ctx.spec["aggregate_type"])
        .where(table.c.owner_aggregate_id == ctx.aggregate_id)
        .where(table.c.state == from_state)
        .order_by(table.c.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if effect_id is None:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED,
            f"no {from_state} effect owned by this aggregate",
        )
    return ctx.session.get(ExternalEffect, effect_id)


def _effect_under_command(ctx: ApplyContext) -> ExternalEffect:
    """The effect this command acts on, whether it is the aggregate or not."""
    if ctx.spec["aggregate_type"] == "external_effect":
        effect = ctx.session.get(ExternalEffect, ctx.aggregate_id)
        if effect is None:
            raise TransitionRefused(
                ReceiptCodes.ILLEGAL_TRANSITION, "no such external effect"
            )
        return effect
    return _effect_for(ctx)


def _w_executor_claim(ctx: ApplyContext) -> None:
    """One executor, one epoch. A claim is not a dispatch and not a success."""
    effect = _effect_under_command(ctx)
    effect.executor_id = "home-mac-worker"
    effect.executor_epoch = (effect.executor_epoch or 0) + 1
    effect.claim_expires_at = ctx.now + timedelta(minutes=15)
    ctx.session.flush()


def _w_executor_claim_release(ctx: ApplyContext) -> None:
    """Release a claim that provably never dispatched (§3.6 rule 6)."""
    effect = _effect_under_command(ctx)
    effect.executor_id = None
    effect.claim_expires_at = None
    ctx.session.flush()


def _w_dispatch_marker(ctx: ApplyContext) -> None:
    """Persisted *before* the outward call. Means "may have arrived"."""
    effect = _effect_under_command(ctx)
    effect.claim_expires_at = ctx.now + timedelta(minutes=15)
    ctx.session.flush()


def _w_reconciler_claim(ctx: ApplyContext) -> None:
    """A single reconciler, so two cannot both decide an unknown outcome."""
    effect = _effect_under_command(ctx)
    effect.executor_id = "reconciler"
    effect.executor_epoch = (effect.executor_epoch or 0) + 1
    ctx.session.flush()


def _w_external_effect_intent(ctx: ApplyContext) -> None:
    """Record the intent to act outside, before anything is dispatched.

    A spec may list both `external_effect` and `external_effect_intent`; they
    describe one act, not two. Creating a second row would collide on
    `(effect_scope_key, remote_idempotency_key)` -- which is exactly the
    constraint that exists to stop one intent becoming two real-world actions.
    """
    if ctx.scratch.get("effect_id") is not None:
        return
    _new_effect(ctx, state="intent_recorded", origin="dal_dispatched")


def _w_observed_external_effect(ctx: ApplyContext) -> None:
    """An out-of-band human fact: confirmed on arrival, never dispatched.

    §3.6 forbids faking `intent_recorded`/`dispatch_started` for these, so if
    the generic member created the row first it is corrected here rather than
    duplicated: the effect DAL never dispatched must not look like one it did.
    """
    effect = _new_effect(ctx, state="confirmed_completed", origin="out_of_band_human")
    effect.origin = "out_of_band_human"
    effect.state = "confirmed_completed"
    effect.executor_id = None
    effect.executor_epoch = None
    effect.claim_expires_at = None
    ctx.session.flush()


def _w_external_effect_outcome(ctx: ApplyContext) -> None:
    effect = _effect_for(ctx)
    if (
        ctx.spec["aggregate_type"] != "external_effect"
        and ctx.scratch.get("effect_id") == effect.effect_id
    ):
        # The companion close already performed this effect's transition
        # earlier in the apply order; the receipt and event recorded it.
        # Raising the outcome here would move the same row a second time in
        # one atomic unit -- a second version the receipts never mention.
        return
    ctx.scratch["effect_id"] = effect.effect_id
    ctx.scratch["effect_from_state"] = effect.state
    effect.state = "confirmed_completed"
    effect.version += 1
    effect.updated_at = ctx.now
    ctx.session.flush()
    ctx.scratch["effect_version"] = effect.version


def _w_external_effect_inventory(ctx: ApplyContext) -> None:
    """Recompute the owner's effect inventory digest (§3.2.1).

    The inventory is the *feature's* full set of effects, "不按
    active/terminal/origin 过滤" — which includes effects owned by the
    feature's recovery cases. Querying only `owner_aggregate_id == feature_id`
    would miss exactly the effects a cancel/block decision most needs to see:
    a recovery case's in-flight effect is still one of the feature's external
    facts.
    """
    table = ExternalEffect.__table__
    feature_id = _owner_feature_id(ctx)
    owner_ids: list[str] = [feature_id]
    if feature_id != ctx.aggregate_id:
        owner_ids.append(ctx.aggregate_id)
    case_table = RecoveryCase.__table__
    owner_ids.extend(
        ctx.session.execute(
            select(case_table.c.recovery_case_id).where(
                case_table.c.feature_id == feature_id
            )
        ).scalars()
    )
    rows = sorted(
        ctx.session.execute(
            select(table.c.effect_id, table.c.version, table.c.state).where(
                table.c.owner_aggregate_id.in_(owner_ids)
            )
        ).all()
    )
    inventory = {
        "schema_version": "dal.external-effect-inventory-binding/1.0",
        "feature_id": feature_id,
        "effects": [
            {"effect_id": r[0], "version": r[1], "state": r[2]} for r in rows
        ],
    }
    digest = hashlib.sha256(canonical_json(inventory).encode("utf-8")).hexdigest()
    feature = Feature.__table__
    ctx.session.execute(
        update(feature)
        .where(feature.c.feature_id == feature_id)
        .values(external_effect_inventory_sha256=digest)
    )


def _w_external_effect_transition_receipt(ctx: ApplyContext) -> None:
    effect = _effect_under_command(ctx)
    companion = _companion_for(ctx, "external_effect")
    ctx.session.add(
        TransitionReceipt(
            receipt_id=new_id(),
            idempotency_key=f"{ctx.command.idempotency_key}:effect",
            aggregate_type="external_effect",
            aggregate_id=effect.effect_id,
            aggregate_version=ctx.scratch.get("effect_version", effect.version),
            # A companion carries its own id: §2.3.1 gives each companion an
            # independent identity and receipt, so naming the root spec here
            # would attribute the effect's transition to a spec that did not
            # perform it.
            spec_id=companion["companion_id"] if companion is not None else ctx.spec["spec_id"],
            command_type=ctx.command.command_type,
            # The pre-transition state, captured before `external_effect`
            # mutated the row earlier in the apply order. Reading
            # `effect.state` here would write the new state into both fields
            # and erase the transition the receipt exists to prove.
            from_state=ctx.scratch.get("effect_from_state", effect.state),
            to_state=effect.state,
            receipt_code=ReceiptCodes.APPLIED,
            receipt_schema_version=RECEIPT_SCHEMAS["external_effect"],
            request_payload_sha256=ctx.request_payload_sha256,
            event_id=ctx.event_id,
            recorded_at=ctx.now,
        )
    )


def _w_recovery_case(ctx: ApplyContext) -> None:
    """Advance the case when it is the aggregate; create it when it is not.

    Recovery specs list `recovery_case` rather than `aggregate` in their write
    set, so for those this member *is* the compare-and-swap that moves the
    case. Treating it as a no-op because "`_w_aggregate` will handle it" left
    every recovery transition silently not happening.
    """
    if ctx.spec["aggregate_type"] == "recovery_case":
        table = RecoveryCase.__table__
        result = ctx.session.execute(
            update(table)
            .where(table.c.recovery_case_id == ctx.aggregate_id)
            .where(table.c.version == ctx.command.expected_version)
            .values(
                state=ctx.to_state,
                version=ctx.command.expected_version + 1,
                reason_code=ctx.spec["result_reason_code"],
                updated_at=ctx.now,
            )
        )
        if result.rowcount != 1:
            raise TransitionRefused(
                ReceiptCodes.VERSION_CONFLICT, "recovery case moved by another writer"
            )
        ctx.scratch["recovery_case_id"] = ctx.aggregate_id
        ctx.scratch["recovery_case_state"] = ctx.to_state
        return
    case = RecoveryCase(
        recovery_case_id=new_id(),
        feature_id=_owner_feature_id(ctx),
        version=1,
        state="investigating",
        reason_code=None,
        proposal_sha256=None,
        impact_sha256=None,
        approval_id=ctx.scratch.get("approval_id"),
        execution_epoch=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    ctx.session.add(case)
    ctx.session.flush()
    ctx.scratch["recovery_case_id"] = case.recovery_case_id
    # The receipt and companion event must describe the case as it actually
    # exists -- a later applier (a decision write set) may move it off
    # `investigating` before the receipt writer runs, and the receipt is the
    # only durable record of where the case ended up.
    ctx.scratch["recovery_case_state"] = "investigating"


def _w_recovery_transition_receipt(ctx: ApplyContext) -> None:
    is_case_aggregate = ctx.spec["aggregate_type"] == "recovery_case"
    ctx.session.add(
        TransitionReceipt(
            receipt_id=new_id(),
            idempotency_key=f"{ctx.command.idempotency_key}:recovery",
            aggregate_type="recovery_case",
            aggregate_id=ctx.scratch.get("recovery_case_id", ctx.aggregate_id),
            # The receipt binds the recovery case's own version (§2.5), not
            # the root aggregate's: a case created by this command
            # (RECOVERY-OPEN) is at version 1 even though the command carried
            # the feature's expected_version; a case being advanced is CAS'd
            # against its own expected_version, so `+ 1` holds only there.
            aggregate_version=(
                (ctx.command.expected_version or 0) + 1 if is_case_aggregate else 1
            ),
            spec_id=ctx.spec["spec_id"],
            command_type=ctx.command.command_type,
            from_state=ctx.from_state if is_case_aggregate else None,
            # The case's own destination, recorded by `_w_recovery_case`. The
            # root's `to_state` would write the *feature's* state onto the
            # case's receipt whenever the two differ (RECOVERY-OPEN leaves the
            # feature in `needs_human` while the case starts `investigating`).
            to_state=(
                ctx.to_state
                if is_case_aggregate
                else ctx.scratch.get("recovery_case_state", ctx.to_state)
            ),
            receipt_code=ReceiptCodes.APPLIED,
            receipt_schema_version=RECEIPT_SCHEMAS["recovery_case"],
            request_payload_sha256=ctx.request_payload_sha256,
            event_id=ctx.event_id,
            recorded_at=ctx.now,
        )
    )


def _record_evidence(ctx: ApplyContext, kind: str, schema: str) -> None:
    ctx.session.add(
        EvidenceRecord(
            evidence_id=new_id(),
            kind=kind,
            subject_aggregate_type=ctx.spec["aggregate_type"],
            subject_aggregate_id=ctx.aggregate_id,
            evidence_schema_version=schema,
            payload_sha256=ctx.request_payload_sha256,
            protected_ref=None,
            recorded_at=ctx.now,
        )
    )


def _w_evidence(ctx: ApplyContext) -> None:
    _record_evidence(ctx, "evidence", ctx.spec["evidence_schema_version"] or "unknown")


def _w_authoritative_receipt(ctx: ApplyContext) -> None:
    _record_evidence(
        ctx, "authoritative_receipt", ctx.spec["evidence_schema_version"] or "unknown"
    )


def _w_authoritative_post_read(ctx: ApplyContext) -> None:
    _record_evidence(
        ctx, "authoritative_post_read", ctx.spec["evidence_schema_version"] or "unknown"
    )


def _record_impact(ctx: ApplyContext, kind: str) -> None:
    body = {"feature_id": _owner_feature_id(ctx), "spec_id": ctx.spec["spec_id"], "kind": kind}
    ctx.session.add(
        ImpactReport(
            impact_id=new_id(),
            feature_id=_owner_feature_id(ctx),
            kind=kind,
            impact_sha256=hashlib.sha256(
                canonical_json(body).encode("utf-8")
            ).hexdigest(),
            effect_inventory_sha256=ctx.request_payload_sha256,
            recorded_at=ctx.now,
        )
    )


def _w_impact_report(ctx: ApplyContext) -> None:
    _record_impact(ctx, "impact_report")


def _w_impact(ctx: ApplyContext) -> None:
    _record_impact(ctx, "impact")


def _w_plan_version(ctx: ApplyContext) -> None:
    """Bump the plan version, from nothing if there was no plan yet.

    `NULL + 1` is `NULL` in SQL, so a plain increment on a feature that has
    never had a plan silently does nothing -- and a revision that does not
    change the version leaves the old plan's approvals valid.
    """
    table = Feature.__table__
    ctx.session.execute(
        update(table)
        .where(table.c.feature_id == _owner_feature_id(ctx))
        .values(plan_version=func.coalesce(table.c.plan_version, 0) + 1)
    )


#: Every `atomic_write_set` member the registry can name, mapped to the change
#: it makes. A member with no applier is a hard failure: the spec asked for a
#: write the service cannot perform, and silently skipping it would report a
#: transition as complete when part of its atomic set never happened.
WRITE_SET_APPLIERS: Final[dict[str, Callable[[ApplyContext], None]]] = {
    "aggregate": _w_aggregate,
    "business_event": _w_business_event,
    "transition_receipt": _w_transition_receipt,
    "audit": _w_audit,
    "notification_outbox": _w_notification_outbox,
    "decision_create": _w_decision_create,
    "decision": _w_decision,
    "incident_decision": _w_incident_decision,
    "decision_resolve": _w_decision_resolve,
    "decision_consume": _w_decision_consume,
    "decision_supersede": _w_decision_supersede,
    "decision_projection": _w_decision_projection,
    "decision_action_receipt": _w_decision_action_receipt,
    "approval_record": _w_approval_record,
    "approval_consume": _w_approval_consume,
    "approval_action_receipt": _w_approval_action_receipt,
    "approval_action_receipt_ref": _w_approval_action_receipt_ref,
    "capability_epoch_increment": _w_capability_epoch_increment,
    "capability_issue": _w_capability_issue,
    "capability_consume": _w_capability_consume,
    "capability_revoke": _w_capability_revoke,
    "lease_issue": _w_lease_issue,
    "lease_revoke": _w_lease_revoke,
    "external_effect": _w_external_effect,
    "executor_claim": _w_executor_claim,
    "executor_claim_release": _w_executor_claim_release,
    "dispatch_marker": _w_dispatch_marker,
    "reconciler_claim": _w_reconciler_claim,
    "external_effect_intent": _w_external_effect_intent,
    "observed_external_effect": _w_observed_external_effect,
    "external_effect_outcome": _w_external_effect_outcome,
    "external_effect_inventory": _w_external_effect_inventory,
    "external_effect_transition_receipt": _w_external_effect_transition_receipt,
    "recovery_case": _w_recovery_case,
    "recovery_transition_receipt": _w_recovery_transition_receipt,
    "evidence": _w_evidence,
    "authoritative_receipt": _w_authoritative_receipt,
    "authoritative_post_read": _w_authoritative_post_read,
    "impact_report": _w_impact_report,
    "impact": _w_impact,
    "plan_version": _w_plan_version,
}

#: Members whose applier must run before others that depend on their scratch
#: output (an approval must exist before it is consumed; a decision must exist
#: before it is projected). The registry's set is unordered, so the order is
#: fixed here rather than left to dict iteration.
_APPLY_ORDER: Final[tuple[str, ...]] = (
    "aggregate",
    "plan_version",
    "capability_epoch_increment",
    "decision_create",
    "decision",
    "incident_decision",
    "decision_resolve",
    "decision_consume",
    "decision_supersede",
    "decision_action_receipt",
    "approval_record",
    "approval_consume",
    "approval_action_receipt",
    "approval_action_receipt_ref",
    "capability_issue",
    "capability_consume",
    "capability_revoke",
    "lease_issue",
    "lease_revoke",
    "external_effect",
    "executor_claim",
    "executor_claim_release",
    "dispatch_marker",
    "reconciler_claim",
    "external_effect_intent",
    "observed_external_effect",
    "external_effect_outcome",
    "external_effect_transition_receipt",
    "external_effect_inventory",
    "recovery_case",
    "recovery_transition_receipt",
    "evidence",
    "authoritative_receipt",
    "authoritative_post_read",
    "impact_report",
    "impact",
    "decision_projection",
    "business_event",
    "transition_receipt",
    "audit",
    "notification_outbox",
)


# --------------------------------------------------------------------------
# The orchestrator
# --------------------------------------------------------------------------


def _read_aggregate(
    session: Session, aggregate_type: str, aggregate_id: str
) -> tuple[str, int, str | None] | None:
    """Current state, version and checkpoint, read straight from the database.

    A Core select, not `Session.get()`: the identity map cannot see a state
    another session has already committed, and a state machine that decides
    from a cached row will happily apply a transition the database has already
    moved past (CLAUDE.md §5.2).
    """
    model = _AGGREGATE_MODELS.get(aggregate_type)
    if model is None:
        return None
    table = model.__table__
    pk = table.c[_AGGREGATE_PK[aggregate_type]]
    columns = [table.c.state, table.c.version]
    has_checkpoint = "checkpoint_state" in table.c
    if has_checkpoint:
        columns.append(table.c.checkpoint_state)
    row = session.execute(select(*columns).where(pk == aggregate_id)).first()
    if row is None:
        return None
    return (row[0], row[1], row[2] if has_checkpoint else None)


def _check_actor_and_evidence(
    spec: dict[str, Any], command: TransitionCommand
) -> None:
    """§2.3.1's three closed allowlists, in the order the contract states them."""
    if command.actor_type not in spec["allowed_actor_types"]:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED,
            f"actor {command.actor_type!r} may not submit this command",
        )

    allowed_sources = set(spec["allowed_evidence_source_types"])
    submitted_sources = set(command.evidence_source_types)
    if not submitted_sources <= allowed_sources:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED,
            "evidence source outside the spec's closed projection",
        )

    # The real unit of validation is the binding: one actor plus the complete
    # set of evidence sources that path requires. Bindings are alternatives;
    # within a chosen binding every source must be present. Taking the
    # cartesian product of the two projection arrays -- or reading a binding's
    # source set as "any of" -- would admit combinations the contract forbids.
    bindings = [
        binding
        for binding in spec["actor_evidence_bindings"]
        if binding["actor_type"] == command.actor_type
    ]
    if not bindings:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED, "no evidence binding for this actor"
        )
    if not any(
        set(binding["required_evidence_source_types"]) <= submitted_sources
        for binding in bindings
    ):
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED,
            "no binding's required evidence sources are all present",
        )

    required_schemas = set(spec["required_evidence_schema_versions"])
    if not required_schemas <= set(command.evidence_schema_versions):
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED, "required evidence schema version missing"
        )


#: Fields an evidence document must carry as non-degenerate values. The guard
#: validates their *semantic* binding (cross-field equality); the schema stage
#: validates their *presence* — a null or whitespace receipt ID is a malformed
#: document, not a semantic mismatch, and ``None == None`` would pass the
#: guard's ``equals_field`` check.
_REQUIRED_EVIDENCE_FIELDS: tuple[str, ...] = (
    "payload_sha256",
    "impact_sha256",
    "semantic_binding_sha256",
    "authoritative_readback_sha256",
)


def _validate_evidence_documents(command: TransitionCommand) -> None:
    """Schema-stage validation: each evidence document's required fields are present.

    Runs after the actor/evidence-source allowlist and before the guard's
    semantic binding checks. A document with a null, empty, or whitespace-only
    required field is rejected here — the guard's ``equals_field`` would pass
    ``None == None``, hiding the schema defect behind a semantic check that
    cannot meaningfully evaluate.

    ``authoritative_receipt_id`` is conditionally required: a
    ``confirmed_completed`` outcome must carry a non-degenerate, whitespace-free
    receipt ID (the effect was executed and has a receipt); a
    ``confirmed_not_executed`` outcome may carry ``None`` (nothing was executed,
    so there is no receipt to cite).
    """
    effect_outcome = command.command_parameters.get("effect_outcome")
    for doc in command.evidence_documents:
        for field in _REQUIRED_EVIDENCE_FIELDS:
            value = doc.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise TransitionRefused(
                    ReceiptCodes.POLICY_DENIED,
                    f"evidence schema validation failed: "
                    f"{field} is null/empty/whitespace",
                )
        if effect_outcome == "confirmed_completed":
            receipt_id = doc.get("authoritative_receipt_id")
            if receipt_id is None or not isinstance(receipt_id, str):
                raise TransitionRefused(
                    ReceiptCodes.POLICY_DENIED,
                    "evidence schema validation failed: "
                    "authoritative_receipt_id is null for confirmed_completed",
                )
            if not receipt_id.strip():
                raise TransitionRefused(
                    ReceiptCodes.POLICY_DENIED,
                    "evidence schema validation failed: "
                    "authoritative_receipt_id is empty/whitespace",
                )
            if receipt_id != receipt_id.strip():
                raise TransitionRefused(
                    ReceiptCodes.POLICY_DENIED,
                    "evidence schema validation failed: "
                    "authoritative_receipt_id has leading/trailing whitespace",
                )


def _check_guard(spec: dict[str, Any], facts: GuardFacts) -> None:
    guard_id = spec["guard_id"]
    if guard_id is None:
        return
    outcome = evaluate_guard(guard_registry().by_id(guard_id), facts)
    if not outcome.passed:
        raise TransitionRefused(
            ReceiptCodes.POLICY_DENIED, f"guard {guard_id}: {outcome.failed_clause}"
        )


def apply_transition(
    engine: Engine,
    command: TransitionCommand,
    *,
    facts: GuardFacts | None = None,
    now: datetime | None = None,
    artifact_reader: ArtifactReader | None = None,
) -> TransitionOutcome:
    """Resolve, validate and apply one transition, or refuse without writing."""
    now = now or utc_now()
    facts = facts or GuardFacts({})
    registry = transition_registry()
    request_digest = hashlib.sha256(
        canonical_json(
            {
                "aggregate_type": command.aggregate_type,
                "aggregate_id": command.aggregate_id,
                "command_type": command.command_type,
                "command_parameters": command.command_parameters,
                "reason_code": command.reason_code,
                "decision_action": command.decision_action,
            }
        ).encode("utf-8")
    ).hexdigest()

    sessions = session_factory(engine)
    spec: dict[str, Any] | None = None
    from_state = ""
    try:
        with sessions() as session, session.begin():
            current = _read_aggregate(
                session, command.aggregate_type, command.aggregate_id
            )
            if current is None:
                # No row yet. That is the `none` pseudo-state the registry
                # spells as `from_state: null`, and only a creation spec can
                # match it -- anything else falls through to
                # ILLEGAL_TRANSITION when resolution fails.
                from_state, current_version, checkpoint = None, None, None
            else:
                from_state, current_version, checkpoint = current

            # Terminal first: no spec may provide an exit, so the registry is
            # not even consulted.
            if from_state in TERMINAL_STATES.get(command.aggregate_type, frozenset()):
                raise TransitionRefused(
                    ReceiptCodes.TERMINAL_STATE,
                    f"{command.aggregate_type} is {from_state}",
                )

            replayed = session.execute(
                select(
                    TransitionReceipt.__table__.c.request_payload_sha256,
                    TransitionReceipt.__table__.c.from_state,
                    TransitionReceipt.__table__.c.to_state,
                    TransitionReceipt.__table__.c.spec_id,
                    TransitionReceipt.__table__.c.receipt_schema_version,
                ).where(
                    TransitionReceipt.__table__.c.idempotency_key
                    == command.idempotency_key
                )
            ).first()
            if replayed is not None:
                if replayed[0] != request_digest:
                    raise TransitionRefused(
                        ReceiptCodes.IDEMPOTENCY_CONFLICT,
                        "command key reused with different content",
                    )
                # §2.6: a replay returns the *original* receipt, not the
                # aggregate's current state -- a later transition may have
                # moved it, and answering with "now" would erase what this
                # command actually did.
                raise _Replay(
                    from_state=replayed[1],
                    to_state=replayed[2],
                    spec_id=replayed[3],
                    receipt_schema=replayed[4],
                )

            parameters = command.command_parameters
            spec = registry.resolve(
                ResolutionKey(
                    aggregate_type=command.aggregate_type,
                    from_state=from_state,
                    command_type=command.command_type,
                    target_state=parameters.get("target_state"),
                    effect_outcome=parameters.get("effect_outcome"),
                    owner_aggregate_type=parameters.get("owner_aggregate_type"),
                    decision_action=command.decision_action,
                    reason_code=command.reason_code,
                )
            )
            if spec is None:
                raise TransitionRefused(
                    ReceiptCodes.ILLEGAL_TRANSITION,
                    "no registry spec matches this exact command",
                )

            if command.expected_version != current_version:
                raise TransitionRefused(
                    ReceiptCodes.VERSION_CONFLICT, "stale expected version"
                )
            if (from_state is None) != (spec["from_state"] is None):
                raise TransitionRefused(
                    ReceiptCodes.ILLEGAL_TRANSITION,
                    "creation spec matched an existing aggregate, or the reverse",
                )

            _check_actor_and_evidence(spec, command)
            _validate_evidence_documents(command)
            _check_guard(spec, facts)

            ctx = ApplyContext(
                session=session,
                spec=spec,
                command=command,
                aggregate_id=command.aggregate_id,
                from_state=from_state,
                to_state=spec["to_state"],
                aggregate_version=current_version,
                now=now,
                request_payload_sha256=request_digest,
                artifact_reader=artifact_reader,
            )
            ctx.scratch["existing_checkpoint"] = checkpoint

            declared = set(spec["atomic_write_set"])
            unknown = declared - set(WRITE_SET_APPLIERS)
            if unknown:
                # Never apply a partial atomic set. If the service cannot
                # perform every member the spec names, the transition does not
                # happen at all.
                raise DalError(
                    DalErrorCode.INTERNAL_ERROR,
                    internal_detail=f"no applier for write-set members {sorted(unknown)}",
                )
            _validate_bound_authority(ctx, declared)
            for member in _APPLY_ORDER:
                if member in declared:
                    WRITE_SET_APPLIERS[member](ctx)

            # Companions are part of the same atomic unit, not a second
            # dispatchable command (§2.3.1). Their write-set members are
            # already folded into the root's set; what remains is their own
            # event, which the oracle asserts alongside the root's.
            _emit_companion_events(ctx)

    except _Replay as replay:
        return TransitionOutcome(
            receipt_code=ReceiptCodes.APPLIED,
            receipt_schema=replay.receipt_schema,
            writes=(),
            events=(),
            from_state=replay.from_state,
            to_state=replay.to_state,
            reason_code=None,
            reason_owner=None,
            spec_id=replay.spec_id,
            duplicate=True,
        )
    except TransitionRefused as refusal:
        return TransitionOutcome(
            receipt_code=refusal.code,
            receipt_schema=(
                spec["success_receipt_schema"]
                if spec is not None
                else RECEIPT_SCHEMAS.get(
                    command.aggregate_type, RECEIPT_SCHEMAS["feature"]
                )
            ),
            writes=(),
            events=(),
            from_state=from_state,
            to_state=from_state,
            reason_code=None,
            reason_owner=None,
            spec_id=spec["spec_id"] if spec else None,
        )

    assert spec is not None
    return TransitionOutcome(
        receipt_code=ReceiptCodes.APPLIED,
        receipt_schema=spec["success_receipt_schema"],
        writes=tuple(m for m in _APPLY_ORDER if m in set(spec["atomic_write_set"])),
        events=(spec["event_type"],)
        + tuple(
            companion["event_type"]
            for companion in spec["atomic_companion_transitions"]
        ),
        from_state=from_state,
        to_state=spec["to_state"],
        reason_code=spec["result_reason_code"],
        reason_owner=spec["result_reason_owner"],
        spec_id=spec["spec_id"],
    )


def _emit_companion_events(ctx: ApplyContext) -> None:
    """One event row per companion transition, in the same transaction."""
    for companion in ctx.spec["atomic_companion_transitions"]:
        # §3.6 binds an ExternalEffect companion to the root entity by
        # `owner_aggregate_id_source`, and fixes its owner type to the root's.
        # An unrecognised source would attach the effect to something else, so
        # it is refused rather than defaulted. RecoveryCase companions carry no
        # owner binding: the case is created by the root, not looked up.
        if companion["aggregate_type"] == "external_effect":
            if companion.get("owner_aggregate_id_source") != "root.aggregate_id":
                raise TransitionRefused(
                    ReceiptCodes.POLICY_DENIED,
                    f"companion {companion['companion_id']} has an unbound owner",
                )
            if companion.get("owner_aggregate_type") != ctx.spec["aggregate_type"]:
                raise TransitionRefused(
                    ReceiptCodes.POLICY_DENIED,
                    f"companion {companion['companion_id']} owner type mismatch",
                )
        # The event belongs to the companion's aggregate, so both its id and
        # its version come from that aggregate -- never from the root, and
        # never from the companion's position in the list (§3.4). An
        # ExternalEffect companion's effect was created or closed by an earlier
        # applier, which recorded both in scratch; a missing entry means the
        # write set that produces the effect was left out, which is a spec
        # defect, not a default.
        if companion["aggregate_type"] == "external_effect":
            companion_aggregate_id = ctx.scratch.get("effect_id")
            companion_aggregate_version = ctx.scratch.get("effect_version")
            if companion_aggregate_id is None or companion_aggregate_version is None:
                raise DalError(
                    DalErrorCode.INTERNAL_ERROR,
                    internal_detail=(
                        f"companion {companion['companion_id']} has no effect in "
                        "scratch: the write set is missing its effect member"
                    ),
                )
        elif companion["aggregate_type"] == "recovery_case":
            companion_aggregate_id = ctx.scratch.get("recovery_case_id")
            if companion_aggregate_id is None:
                raise DalError(
                    DalErrorCode.INTERNAL_ERROR,
                    internal_detail=(
                        f"companion {companion['companion_id']} has no recovery "
                        "case in scratch: the write set is missing recovery_case"
                    ),
                )
            companion_aggregate_version = (
                (ctx.command.expected_version or 0) + 1
                if ctx.spec["aggregate_type"] == "recovery_case"
                else 1
            )
        else:  # pragma: no cover - registry contains no other companion types
            raise DalError(
                DalErrorCode.INTERNAL_ERROR,
                internal_detail=(
                    f"unknown companion aggregate type: "
                    f"{companion['aggregate_type']}"
                ),
            )
        payload = {"companion_id": companion["companion_id"]}
        ctx.session.add(
            Event(
                event_id=f"{ctx.event_id}-companion-{companion['companion_id']}",
                schema_version=EVENT_SCHEMA_VERSION,
                event_type=companion["event_type"],
                aggregate_type=companion["aggregate_type"],
                aggregate_id=companion_aggregate_id,
                aggregate_version=companion_aggregate_version,
                command_id=ctx.command.idempotency_key,
                causation_id=ctx.event_id,
                correlation_id=ctx.aggregate_id,
                actor_type=ctx.command.actor_type,
                actor_id=SERVICE_ACTOR,
                occurred_at=ctx.now,
                encrypted_payload=None,
                payload_sha256=hashlib.sha256(
                    canonical_json(payload).encode("utf-8")
                ).hexdigest(),
            )
        )


class _Replay(Exception):
    """An exact replay of an already-applied command. Rolls back, returns the receipt."""

    def __init__(
        self,
        *,
        from_state: str | None,
        to_state: str,
        spec_id: str,
        receipt_schema: str,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.spec_id = spec_id
        self.receipt_schema = receipt_schema
        super().__init__(f"{from_state} -> {to_state}")


# The apply order and the applier table must name exactly the same members.
# A member with an applier but no place in the order would be silently skipped
# -- the transition would report an atomic set it never completed -- and a
# member in the order with no applier would raise mid-transaction. Neither is
# detectable at the call site, so it is checked once at import.
_ORDER_ONLY = set(_APPLY_ORDER) - set(WRITE_SET_APPLIERS)
_APPLIER_ONLY = set(WRITE_SET_APPLIERS) - set(_APPLY_ORDER)
if _ORDER_ONLY or _APPLIER_ONLY:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "write-set apply order and applier table disagree: "
        f"order-only={sorted(_ORDER_ONLY)} applier-only={sorted(_APPLIER_ONLY)}"
    )
