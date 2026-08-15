"""Minimal valid rows for the DAL tables.

These live in `tests/` rather than on the models: a `fixture_row` classmethod
shipped in production is a constructor that skips whatever the real write path
would have validated, and it is only ever one refactor away from being called
from real code.

Each factory fills the columns the schema requires and nothing else. The values
are placeholders — a repository id, a digest of zero bytes — never anything
resembling real repository, ledger or device data.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from personal_agent_core.timeutil import utc_now

from personal_agent_dal.machine.binding import build_state_binding
from personal_agent_dal.machine.registry import jcs_sha256
from personal_agent_dal.storage.models import Event, Feature, OperationReceiptRow


#: SHA-256 of the empty string: a syntactically valid digest that stands for
#: "nothing here yet" without pretending to be a real artifact.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
PLACEHOLDER_GIT_SHA = "0" * 40


def state_binding_sha256(feature: Feature) -> str:
    """Hash the exact server-side Feature projection used by approval tests."""

    return jcs_sha256(build_state_binding(feature))


def feature_row(
    *,
    feature_id: str,
    version: int,
    state: str = "intake",
    now: datetime | None = None,
) -> Feature:
    from personal_agent_dal.storage.models import CHECKPOINT_REQUIRED_STATES

    now = now or utc_now()
    # A stopped feature always carries the checkpoint it stopped at; the schema
    # enforces it, and seeding one without would be seeding a row the service
    # could never have written.
    stopped = state in CHECKPOINT_REQUIRED_STATES
    return Feature(
        feature_id=feature_id,
        schema_version="dal.feature-state/1.0",
        version=version,
        state=state,
        checkpoint_state="coding" if stopped else None,
        reason_code=None,
        reason_owner=None,
        plan_version=None,
        artifact_sha256=None,
        repository_id="repo-placeholder",
        base_sha=PLACEHOLDER_GIT_SHA,
        result_sha=None,
        last_verified_sha=None,
        decision_frontier_version=1,
        policy_version="dal-policy/1.0",
        capability_epoch=1,
        external_effect_inventory_sha256=EMPTY_SHA256,
        trace_id=f"trace-{feature_id}",
        created_at=now,
        updated_at=now,
    )


def event_row(
    *,
    event_id: str,
    occurred_at: datetime | None = None,
    aggregate_version: int = 1,
) -> Event:
    occurred_at = occurred_at or utc_now()
    return Event(
        event_id=event_id,
        schema_version="dal.event/1.0",
        event_type="feature.created",
        aggregate_type="feature",
        aggregate_id=f"agg-{event_id}",
        aggregate_version=aggregate_version,
        command_id=None,
        causation_id=None,
        correlation_id=None,
        actor_type="service",
        actor_id="workflow-service",
        occurred_at=occurred_at,
        encrypted_payload=None,
        payload_sha256=EMPTY_SHA256,
    )


def operation_receipt_row(
    *,
    idempotency_key: str,
    request_payload_sha256: str,
    operation_id: str | None = None,
    now: datetime | None = None,
) -> OperationReceiptRow:
    now = now or utc_now()
    return OperationReceiptRow(
        operation_id=operation_id or f"op-{idempotency_key}",
        idempotency_key=idempotency_key,
        operation_spec_id="OP-DB-CONTRACT-001",
        command_type="apply_database_contract",
        actor_type="service",
        evidence_source_type="migration-runner",
        receipt_code="APPLIED",
        receipt_schema_version="dal.operation-receipt/1.0",
        request_payload_sha256=request_payload_sha256,
        response_payload_sha256=None,
        recorded_at=now,
    )


def decision_row(*, feature_id: str, decision_id: str = "decision-seeded",
                 status: str = "open", now: datetime | None = None,
                 state_sha256: str | None = None):
    from personal_agent_dal.storage.machine_models import Decision

    now = now or utc_now()
    return Decision(
        decision_id=decision_id,
        feature_id=feature_id,
        decision_version=1,
        action=None,
        reason_code=None,
        status=status,
        priority=4,
        artifact_sha256=None,
        state_sha256=state_sha256,
        is_incident=False,
        root_id=decision_id,
        safety_or_irreversible=False,
        blocking_scope="none",
        depends_on_json="[]",
        expires_at=now + timedelta(minutes=15),
        superseded_by=None,
        notification_priority="normal",
        created_at=now,
        updated_at=now,
    )


def approval_row(
    *,
    feature_id: str,
    approval_id: str = "approval-seeded",
    action: str = "approve_plan",
    decision_id: str | None = None,
    decision_version: int | None = None,
    expected_feature_version: int = 1,
    expected_state: str = "awaiting_plan_review",
    now: datetime | None = None,
    state_sha256: str | None = None,
):
    from personal_agent_dal.storage.machine_models import Approval

    now = now or utc_now()
    return Approval(
        approval_id=approval_id,
        action=action,
        feature_id=feature_id,
        decision_id=decision_id,
        decision_version=decision_version,
        expected_feature_version=expected_feature_version,
        expected_state=expected_state,
        state_sha256=state_sha256,
        artifact_sha256=None,
        device_id="registered-device",
        subject_id="single-user",
        valid_from=now,
        expires_at=now + timedelta(minutes=15),
        idempotency_key=f"{approval_id}-key",
        policy_version="dal-policy/1.0",
        replay_policy="consume_once",
        consumed_by_command_id=None,
        consumed_at=None,
        recorded_at=now,
    )


def capability_row(*, feature_id: str, capability_id: str = "capability-seeded",
                   now: datetime | None = None):
    from datetime import timedelta

    from personal_agent_dal.storage.machine_models import Capability

    now = now or utc_now()
    return Capability(
        capability_id=capability_id,
        approval_id=None,
        feature_id=feature_id,
        action="execute_merge",
        scope="seeded",
        epoch=1,
        expires_at=now + timedelta(minutes=15),
        max_uses=1,
        uses_consumed=0,
        revoked_at=None,
        created_at=now,
    )


def lease_row(*, feature_id: str, lease_id: str = "lease-seeded",
              now: datetime | None = None):
    from datetime import timedelta

    from personal_agent_dal.storage.machine_models import Lease

    now = now or utc_now()
    return Lease(
        lease_id=lease_id,
        feature_id=feature_id,
        job_id=f"job-{lease_id}",
        worker_id="home-mac-worker",
        epoch=1,
        expires_at=now + timedelta(minutes=15),
        heartbeat_at=None,
        revoked_at=None,
        created_at=now,
    )


def recovery_case_row(*, recovery_case_id: str, feature_id: str, version: int,
                      state: str, now: datetime | None = None):
    from personal_agent_dal.storage.machine_models import RecoveryCase

    now = now or utc_now()
    return RecoveryCase(
        recovery_case_id=recovery_case_id,
        feature_id=feature_id,
        version=version,
        state=state,
        reason_code=None,
        proposal_sha256=None,
        impact_sha256=None,
        approval_id=None,
        execution_epoch=1,
        created_at=now,
        updated_at=now,
    )


def external_effect_row(*, effect_id: str, owner_id: str, version: int, state: str,
                        owner_type: str = "feature", now: datetime | None = None):
    from personal_agent_dal.storage.machine_models import ExternalEffect

    now = now or utc_now()
    return ExternalEffect(
        effect_id=effect_id,
        version=version,
        origin="dal_dispatched",
        owner_aggregate_type=("feature" if owner_type == "external_effect" else owner_type),
        owner_aggregate_id=owner_id,
        effect_scope_key=f"scope-{effect_id}",
        remote_idempotency_key=f"remote-{effect_id}",
        target_fingerprint=EMPTY_SHA256,
        state=state,
        attempt=1,
        executor_id="home-mac-worker",
        executor_epoch=1,
        claim_expires_at=None,
        capability_id=None,
        capability_epoch=None,
        receipt_refs_sha256=None,
        post_read_refs_sha256=None,
        impact_sha256=None,
        created_at=now,
        updated_at=now,
    )
