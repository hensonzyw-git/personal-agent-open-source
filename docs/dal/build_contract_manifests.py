#!/usr/bin/env python3
"""Build the Wave 0 machine-readable DAL contract manifests.

This is documentation tooling only.  It does not import or execute DAL runtime,
GitHub, Worker, provider, or Personal Agent code.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "manifests"

BASE_WRITES = ["aggregate", "business_event", "transition_receipt", "audit"]
RECOVERY_BASE_WRITES = ["recovery_case", "business_event", "recovery_transition_receipt", "audit"]
EFFECT_BASE_WRITES = ["external_effect", "business_event", "external_effect_transition_receipt", "audit"]
COMMON_EVIDENCE_FIELDS = {
    "evidence_id": "string",
    "schema_version": "string",
    "source_type": "string",
    "subject_aggregate_type": "string",
    "subject_aggregate_id": "string",
    "subject_aggregate_version": "integer",
    "observed_at": "date-time",
    "payload_sha256": "sha256",
    "protected_ref": "string",
}
OPERATION_COMMAND_TYPES = {
    "DAL-T-CMD-IDEMPOTENCY-001": "record_plan",
    "DAL-T-APP-001": "approve_plan",
    "DAL-T-CHECK-001": "write_github_check",
    "DAL-T-CONFIG-ISOLATION-001": "load_service_config",
    "DAL-T-DB-CONTRACT-001": "apply_database_contract",
    "DAL-T-DOCK-001": "project_decision_dock",
    "DAL-T-BATCH-001": "evaluate_notification_batch",
    "DAL-T-NOTIFY-001": "deliver_notification",
    "DAL-T-PROVIDER-ROUTE-001": "route_provider_attempt",
    "DAL-T-RESTART-001": "resume_persisted_run",
    "DAL-T-EFFECT-OWNERSHIP-001": "dispatch_effect_outcome_sequence",
}
OPERATION_BINDINGS = {
    "DAL-T-CMD-IDEMPOTENCY-001": ("service", "planner"),
    "DAL-T-APP-001": ("human", "registered-device"),
    "DAL-T-CHECK-001": ("service", "github-control"),
    "DAL-T-CONFIG-ISOLATION-001": ("service", "config-loader"),
    "DAL-T-DB-CONTRACT-001": ("service", "migration-runner"),
    "DAL-T-DOCK-001": ("service", "decision-store"),
    "DAL-T-BATCH-001": ("service", "decision-store"),
    "DAL-T-NOTIFY-001": ("service", "notification-delivery"),
    "DAL-T-PROVIDER-ROUTE-001": ("service", "provider-adapter"),
    "DAL-T-RESTART-001": ("service", "workflow-service"),
    "DAL-T-EFFECT-OWNERSHIP-001": ("service", "workflow-service"),
}
WRITE_SETS = {
    "A": BASE_WRITES,
    "D": [*BASE_WRITES, "decision_create", "decision_projection", "notification_outbox"],
    "H": ["decision_resolve", "decision_action_receipt", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "PLAN_REVISE": ["decision_resolve", "decision_action_receipt", "plan_version", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "P": ["decision_resolve", "decision_action_receipt", "approval_record", "approval_consume", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "P_LEASE": ["decision_resolve", "decision_action_receipt", "approval_record", "approval_consume", "lease_issue", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "J": ["decision_resolve", "approval_record", "approval_consume", "approval_action_receipt", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "O": ["observed_external_effect", "authoritative_post_read", "approval_action_receipt_ref", *BASE_WRITES, "notification_outbox"],
    "U": ["external_effect_outcome", "authoritative_post_read", "incident_decision", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "I": ["decision_resolve", "approval_consume", "capability_issue", "capability_consume", "external_effect_intent", "approval_action_receipt", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "X": ["external_effect_outcome", "authoritative_post_read", *BASE_WRITES],
    "K": ["decision_resolve", "decision_action_receipt", "capability_epoch_increment", "lease_revoke", "impact_report", "external_effect_inventory", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "E": ["external_effect_inventory", *BASE_WRITES, "decision_create", "decision_projection", "notification_outbox"],
    "Q": ["recovery_case", "decision_resolve", "decision_supersede", "decision_action_receipt", "approval_record", "approval_consume", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "R": ["external_effect_outcome", "authoritative_post_read", "decision_resolve", "approval_consume", "approval_action_receipt", *BASE_WRITES, "decision_projection", "notification_outbox"],
}


def expanded_write_set(symbol: str, spec_id: str) -> list[str]:
    values = list(WRITE_SETS[symbol])
    if symbol == "R" and spec_id == "RECONCILE-OPEN-RECOVERY":
        values.append("recovery_case")
    return values


def default_applied_write_set(entity_type: str, event_trace: list[str]) -> list[str]:
    if entity_type == "recovery_case":
        return list(RECOVERY_BASE_WRITES)
    if entity_type == "external_effect":
        return list(EFFECT_BASE_WRITES)
    events = set(event_trace)
    if "feature.blocked" in events:
        return list(WRITE_SETS["D"])
    if "reconciliation.required" in events:
        return list(WRITE_SETS["E"])
    if "approval.recorded" in events:
        return list(WRITE_SETS["J"])
    if "decision.created" in events:
        return ["decision", "decision_projection", "operation_receipt", "audit"]
    return list(BASE_WRITES)


def canonical_bytes(value: object) -> bytes:
    # All contract objects intentionally use only strings, integers, booleans,
    # null, arrays and objects, for which this is RFC 8785 JCS-compatible.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def companion(
    companion_id: str,
    aggregate_type: str,
    from_state: str | None,
    to_state: str,
    event_type: str,
    evidence_schema_versions: list[str],
    atomic_write_set: list[str],
    snapshot_key: str,
) -> dict:
    receipt_schema = {
        "recovery_case": "dal.recovery-transition-receipt/1.0",
        "external_effect": "dal.external-effect-transition-receipt/1.0",
    }[aggregate_type]
    return {
        "companion_id": companion_id,
        "aggregate_type": aggregate_type,
        "from_state": from_state,
        "to_state": to_state,
        "event_type": event_type,
        "required_evidence_schema_versions": evidence_schema_versions,
        "atomic_write_set": atomic_write_set,
        "success_receipt_schema": receipt_schema,
        "success_receipt_code": "APPLIED",
        "snapshot_key": snapshot_key,
    }


def evidence_claim_fields(schema_version: str) -> dict[str, str]:
    """Return the closed, semantic claim vocabulary for one evidence version."""
    name = schema_version.removeprefix("dal.evidence.").removesuffix("/1.0")
    exact = {
        "github-receipt": {
            "artifact_sha256": "sha256", "fact_version": "integer",
            "effect_scope_key": "string", "remote_idempotency_key": "string", "effect_state": "effect-state",
            "semantic_binding_sha256": "sha256",
            "external_effect_id": "string", "external_effect_version": "integer",
            "effect_attempt": "integer", "effect_action": "string", "target_fingerprint": "string",
            "repository_id": "string", "base_sha": "sha1", "artifact_digest": "sha256",
            "pull_request_id": "string", "head_sha": "sha1",
            "merge_sha": "sha1", "remote_receipt_id": "string",
            "authoritative_readback_sha256": "sha256", "impact_sha256": "sha256",
        },
        "deployment-receipt": {
            "artifact_sha256": "sha256", "fact_version": "integer",
            "effect_scope_key": "string", "remote_idempotency_key": "string", "effect_state": "effect-state",
            "semantic_binding_sha256": "sha256",
            "external_effect_id": "string", "external_effect_version": "integer",
            "effect_attempt": "integer", "effect_action": "string", "target_fingerprint": "string",
            "environment_id": "string", "deployment_target_id": "string", "version_digest": "sha256",
            "remote_receipt_id": "string", "authoritative_readback_sha256": "sha256",
            "impact_sha256": "sha256",
        },
        "reconciliation": {
            "artifact_sha256": "sha256", "fact_version": "integer",
            "effect_scope_key": "string", "remote_idempotency_key": "string", "effect_state": "effect-state",
            "semantic_binding_sha256": "sha256",
            "external_effect_id": "string", "external_effect_version": "integer",
            "effect_attempt": "integer", "effect_action": "string", "target_fingerprint": "string",
            "decision_action": "string", "effect_result": "reconciliation-result",
            "authoritative_receipt_id": "nullable-string",
            "authoritative_readback_sha256": "sha256", "impact_sha256": "sha256",
        },
        "github-observed-merge": {
            "repository_id": "string", "pull_request_id": "string", "head_sha": "sha1",
            "merge_sha": "sha1", "actor_id": "string", "merged_at": "date-time",
            "action_receipt_id": "nullable-string", "readback_sha256": "sha256",
        },
        "observed-deployment": {
            "target_fingerprint": "string", "version_digest": "sha256", "actor_id": "string",
            "deployed_at": "date-time", "action_receipt_id": "nullable-string",
            "readback_sha256": "sha256",
        },
        "route-resume": {
            "approved_profile_sha256": "sha256", "primary_failure_receipt_id": "string",
            "failure_class": "provider-failure-class", "fallback_eligible": "boolean",
            "fallback_preflight_receipt_id": "nullable-string", "handoff_sha256": "sha256",
            "unknown_effect_count": "integer", "budget_remaining": "boolean", "fallback_used": "boolean",
        },
        "provider-attempts": {
            "initial_attempt_count": "integer", "transient_retry_count": "integer",
            "provider_attempt_count": "integer", "max_transient_retries": "integer",
            "attempt_receipt_ids": "string-array", "last_failure_class": "provider-failure-class",
        },
        "recovery-proposal": {
            "source_effect_ids": "string-array", "source_effect_versions": "integer-array",
            "authoritative_readback_sha256": "sha256", "impact_sha256": "sha256",
            "proposal_sha256": "sha256",
        },
        "counterparty-receipt": {
            "effect_scope_key": "string", "remote_idempotency_key": "string",
            "target_fingerprint": "string", "remote_receipt_id": "string", "receipt_sha256": "sha256",
        },
        "authoritative-post-read": {
            "effect_scope_key": "string", "remote_idempotency_key": "string",
            "target_fingerprint": "string", "result": "effect-readback-result", "readback_sha256": "sha256",
        },
    }
    if name in exact:
        return exact[name]
    fields: dict[str, str] = {"artifact_sha256": "sha256", "fact_version": "integer"}
    if any(token in name for token in ("approval", "decision")):
        fields.update({"decision_id": "string", "decision_action": "string", "expires_at": "date-time"})
    if any(token in name for token in ("effect", "reconciliation", "dispatch", "claim", "receipt", "rearm")):
        fields.update({"effect_scope_key": "string", "remote_idempotency_key": "string", "effect_state": "effect-state"})
    if any(token in name for token in ("provider", "budget", "review-loop")):
        fields.update({"run_id": "string", "observed_count": "integer", "configured_limit": "integer"})
    if any(token in name for token in ("git", "merge", "pr-", "patch", "plan")):
        fields.update({"repository_id": "string", "base_sha": "sha1", "artifact_digest": "sha256"})
    if any(token in name for token in ("recovery", "post-effect")):
        fields.update({"source_effect_ids": "string-array", "authoritative_readback_sha256": "sha256"})
    return fields


def json_type(field_type: str) -> dict:
    if field_type == "integer":
        return {"type": "integer", "minimum": 0}
    if field_type == "boolean":
        return {"type": "boolean"}
    if field_type == "date-time":
        return {"type": "string", "format": "date-time"}
    if field_type == "sha1":
        return {"type": "string", "pattern": "^[0-9a-f]{40}$"}
    if field_type == "sha256":
        return {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    if field_type == "nullable-string":
        return {"type": ["string", "null"]}
    if field_type == "string-array":
        return {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}}
    if field_type == "integer-array":
        return {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 0}}
    if field_type == "provider-failure-class":
        return {"enum": ["usage_limit", "transient", "auth", "task_failure", "contract_failure", "policy_failure", "budget_limit"]}
    if field_type == "effect-state":
        return {"enum": ["intent_recorded", "claimed", "dispatch_started", "unknown", "reconciling", "confirmed_completed", "confirmed_not_executed"]}
    if field_type == "effect-readback-result":
        return {"enum": ["confirmed_completed", "confirmed_not_executed", "unknown"]}
    if field_type == "reconciliation-result":
        return {"enum": ["confirmed_completed", "confirmed_not_executed"]}
    return {"type": "string", "minLength": 1}


def guard_clauses(guard_id: str, to_state: str) -> list[dict]:
    """Compile a guard ID once into runtime-readable closed predicates."""
    checkpoint_marker = "_AND_CHECKPOINT_"
    base = guard_id
    clauses: list[dict] = []
    if checkpoint_marker in guard_id:
        base, encoded_checkpoint = guard_id.rsplit(checkpoint_marker, 1)
        if encoded_checkpoint != to_state.upper():
            raise ValueError(f"checkpoint guard drift: {guard_id}/{to_state}")
        clauses.append({"field": "checkpoint.state", "operator": "equals", "value": to_state})
    exact_clauses = {
        "G5_REQUIRED": [{"field": "policy.run_gate", "operator": "at_least", "value": "G5"}],
        "NO_OPEN_SAFETY_OR_POLICY_INCIDENT": [{"field": "incidents.open_safety_or_policy_count", "operator": "equals", "value": 0}],
        "PR_ONLY_APPROVAL_ACTION_RECEIPT": [{"field": "approval.action", "operator": "equals", "value": "approve_merge"}, {"field": "approval.receipt_valid_at_effect_time", "operator": "equals", "value": True}, {"field": "policy.managed_merge_enabled", "operator": "equals", "value": False}],
        "OBSERVED_MERGE_ACTION_RECEIPT_VALID_AT_MERGED_AT": [{"field": "approval.receipt_valid_at_effect_time", "operator": "equals", "value": True}, {"field": "external_fact.kind", "operator": "equals", "value": "merge"}],
        "OBSERVED_MERGE_WITHOUT_VALID_ACTION_RECEIPT": [{"field": "approval.receipt_valid_at_effect_time", "operator": "equals", "value": False}, {"field": "external_fact.kind", "operator": "equals", "value": "merge"}],
        "OBSERVED_DEPLOY_WITHOUT_VALID_ACTION_RECEIPT": [{"field": "approval.receipt_valid_at_effect_time", "operator": "equals", "value": False}, {"field": "external_fact.kind", "operator": "equals", "value": "deploy"}],
        "CANCEL_NO_CONFIRMED_OR_UNKNOWN_EFFECTS": [{"field": "effect_inventory.confirmed_count", "operator": "equals", "value": 0}, {"field": "effect_inventory.unknown_or_reconciling_count", "operator": "equals", "value": 0}],
        "CANCEL_CONFIRMED_EFFECTS_AND_NO_UNKNOWN_EFFECTS": [{"field": "effect_inventory.confirmed_count", "operator": "greater_than", "value": 0}, {"field": "effect_inventory.unknown_or_reconciling_count", "operator": "equals", "value": 0}],
        "NO_EXTERNAL_INTENT": [{"field": "effect_inventory.intent_count", "operator": "equals", "value": 0}],
        "UNKNOWN_EXTERNAL_EFFECT": [{"field": "effect_inventory.unknown_or_reconciling_count", "operator": "greater_than", "value": 0}],
        "EFFECT_IS_MERGE": [{"field": "external_effect.kind", "operator": "equals", "value": "merge"}, {"field": "external_effect.state", "operator": "equals", "value": "reconciling"}, {"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "feature"}, {"field": "external_effect.owner_aggregate_id_matches_root", "operator": "equals", "value": True}],
        "EFFECT_IS_DEPLOY": [{"field": "external_effect.kind", "operator": "equals", "value": "deploy"}, {"field": "external_effect.state", "operator": "equals", "value": "reconciling"}, {"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "feature"}, {"field": "external_effect.owner_aggregate_id_matches_root", "operator": "equals", "value": True}],
        "MATCHING_MANAGED_MERGE_EFFECT": [{"field": "external_effect.kind", "operator": "equals", "value": "merge"}, {"field": "external_effect.state", "operator": "equals", "value": "dispatch_started"}, {"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "feature"}, {"field": "external_effect.owner_aggregate_id_matches_root", "operator": "equals", "value": True}],
        "MATCHING_MANAGED_DEPLOY_EFFECT": [{"field": "external_effect.kind", "operator": "equals", "value": "deploy"}, {"field": "external_effect.state", "operator": "equals", "value": "dispatch_started"}, {"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "feature"}, {"field": "external_effect.owner_aggregate_id_matches_root", "operator": "equals", "value": True}],
        "RECOVERY_EFFECT_DISPATCHED_AND_RECEIPT": [{"field": "external_effect.state", "operator": "equals", "value": "dispatch_started"}, {"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "recovery_case"}, {"field": "external_effect.owner_aggregate_id_matches_root", "operator": "equals", "value": True}, {"field": "evidence.authoritative_receipt_valid", "operator": "equals", "value": True}],
        "RECOVERY_EFFECT_MATCHING_SCOPE_KEY_TARGET_AND_RECEIPT": [{"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "recovery_case"}, {"field": "recovery_case.state", "operator": "equals", "value": "blocked"}, {"field": "evidence.scope_key_matches", "operator": "equals", "value": True}, {"field": "evidence.target_matches", "operator": "equals", "value": True}, {"field": "evidence.authoritative_receipt_valid", "operator": "equals", "value": True}],
        "RECOVERY_EFFECT_MATCHING_SCOPE_KEY_TARGET_AND_NOT_EXECUTED_PROOF": [{"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "recovery_case"}, {"field": "recovery_case.state", "operator": "equals", "value": "blocked"}, {"field": "evidence.scope_key_matches", "operator": "equals", "value": True}, {"field": "evidence.target_matches", "operator": "equals", "value": True}, {"field": "evidence.not_executed_readback_valid", "operator": "equals", "value": True}],
        "MATCHING_SCOPE_KEY_TARGET_AND_RECEIPT": [{"field": "evidence.scope_key_matches", "operator": "equals", "value": True}, {"field": "evidence.target_matches", "operator": "equals", "value": True}, {"field": "evidence.authoritative_receipt_valid", "operator": "equals", "value": True}],
        "MATCHING_SCOPE_KEY_TARGET_AND_NOT_EXECUTED_PROOF": [{"field": "evidence.scope_key_matches", "operator": "equals", "value": True}, {"field": "evidence.target_matches", "operator": "equals", "value": True}, {"field": "evidence.not_executed_readback_valid", "operator": "equals", "value": True}],
        "CAPABILITY_AND_LEASE_EPOCH_CURRENT": [{"field": "capability.epoch_current", "operator": "equals", "value": True}, {"field": "lease.epoch_current", "operator": "equals", "value": True}],
        "MATCHING_EXECUTOR_CLAIM": [{"field": "executor.claim_matches", "operator": "equals", "value": True}, {"field": "executor.claim_expired", "operator": "equals", "value": False}],
        "TWO_ATTEMPTS_CONSUMED_AND_NEW_DECISION": [{"field": "provider.transient_retry_count", "operator": "equals", "value": 2}, {"field": "provider.provider_attempt_count", "operator": "equals", "value": 3}, {"field": "decision.action_valid", "operator": "equals", "value": True}],
        "FROZEN_ROUTE_RESUME_OR_FALLBACK_ELIGIBLE": [{"field": "provider.recovery_mode", "operator": "in", "value": ["frozen_route_resumed", "fallback_eligible"]}, {"field": "provider.fallback_used", "operator": "equals", "value": False}],
        "AUTHORITATIVE_POST_EFFECT_READBACK_AND_RECOVERY_PROPOSAL": [{"field": "evidence.authoritative_post_effect_readback_valid", "operator": "equals", "value": True}, {"field": "evidence.recovery_proposal_valid", "operator": "equals", "value": True}],
        "ACTION_BOUND_TEST_DECISION_VALID": [{"field": "decision.action", "operator": "equals", "value": "continue_fix"}, {"field": "decision.reason_code", "operator": "equals", "value": "TEST_BLOCKED"}, {"field": "decision.binding_valid", "operator": "equals", "value": True}, {"field": "decision.unexpired_and_unconsumed", "operator": "equals", "value": True}],
        "ALL_EFFECTS_CONFIRMED_NOT_EXECUTED_AND_NEW_DECISION": [{"field": "effect_inventory.unknown_or_reconciling_count", "operator": "equals", "value": 0}, {"field": "effect_inventory.not_confirmed_not_executed_count", "operator": "equals", "value": 0}, {"field": "decision.action_valid", "operator": "equals", "value": True}],
        "ARTIFACT_OR_PROFILE_DRIFT_AND_NEW_PLAN_APPROVAL": [{"field": "drift.kind", "operator": "in", "value": ["artifact", "execution_profile"]}, {"field": "plan.new_approval_valid", "operator": "equals", "value": True}],
        "AUTHORITATIVE_FACT_ACCEPTED": [{"field": "evidence.authoritative_readback_valid", "operator": "equals", "value": True}, {"field": "decision.action", "operator": "equals", "value": "accept_current_fact"}, {"field": "decision.binding_valid", "operator": "equals", "value": True}],
        "AUTHORITATIVE_RESULT_STILL_UNKNOWN": [{"field": "evidence.authoritative_result", "operator": "equals", "value": "unknown"}],
        "CAPABILITY_EPOCH_REVOKED_AND_NEW_PLAN": [{"field": "capability.previous_epoch_revoked", "operator": "equals", "value": True}, {"field": "plan.new_approval_valid", "operator": "equals", "value": True}],
        "CURRENT_FACT_AND_NEW_ARTIFACT_APPROVAL_VALID": [{"field": "evidence.current_fact_readback_valid", "operator": "equals", "value": True}, {"field": "artifact.new_approval_valid", "operator": "equals", "value": True}],
        "DRIFT_PROBE_VALID_AND_NEW_LEASE": [{"field": "drift_probe.matches_checkpoint", "operator": "equals", "value": True}, {"field": "lease.new_epoch_current", "operator": "equals", "value": True}],
        "EFFECT_RECONCILING_COMPLETED_NONSTATE": [{"field": "external_effect.state", "operator": "equals", "value": "reconciling"}, {"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "feature"}, {"field": "external_effect.owner_aggregate_id_matches_root", "operator": "equals", "value": True}, {"field": "external_effect.changes_feature_state", "operator": "equals", "value": False}, {"field": "evidence.effect_state", "operator": "equals", "value": "confirmed_completed"}],
        "EFFECT_RECONCILING_NOT_EXECUTED": [{"field": "external_effect.state", "operator": "equals", "value": "reconciling"}, {"field": "external_effect.owner_aggregate_type", "operator": "equals", "value": "feature"}, {"field": "external_effect.owner_aggregate_id_matches_root", "operator": "equals", "value": True}, {"field": "evidence.effect_state", "operator": "equals", "value": "confirmed_not_executed"}],
        "EXECUTOR_TERMINATED_AND_NEVER_DISPATCHED": [{"field": "executor.terminated", "operator": "equals", "value": True}, {"field": "external_effect.dispatch_marker_exists", "operator": "equals", "value": False}],
        "EXECUTOR_TERMINATION_OR_DISPATCH_NOT_PROVABLE": [{"field": "executor.failure_shape", "operator": "in", "value": ["executor_terminated", "dispatch_not_provable"]}],
        "GIT_READBACK_AND_NEW_BASE_APPROVAL_VALID": [{"field": "git.authoritative_readback_valid", "operator": "equals", "value": True}, {"field": "git.new_base_approval_valid", "operator": "equals", "value": True}],
        "MATCHING_RECOVERY_CASE_VERIFIED": [{"field": "recovery_case.state", "operator": "equals", "value": "verified"}, {"field": "recovery_case.feature_binding_matches", "operator": "equals", "value": True}],
        "NEW_BUDGET_DECISION_VALID": [{"field": "budget.new_limit_greater_than_usage", "operator": "equals", "value": True}, {"field": "decision.action", "operator": "equals", "value": "resume_with_budget"}, {"field": "decision.binding_valid", "operator": "equals", "value": True}],
        "PREREQUISITE_READBACK_VALID": [{"field": "prerequisite.authoritative_readback_satisfied", "operator": "equals", "value": True}],
        "PROVIDER_CONTRACT_STABLE_AND_NEW_DECISION": [{"field": "provider.contract_digest_unchanged", "operator": "equals", "value": True}, {"field": "decision.action", "operator": "equals", "value": "retry_from_checkpoint"}, {"field": "decision.binding_valid", "operator": "equals", "value": True}],
        "READ_ONLY_AUTH_PROBE_VALID": [{"field": "auth_probe.read_only", "operator": "equals", "value": True}, {"field": "auth_probe.authorized", "operator": "equals", "value": True}, {"field": "auth_probe.credential_rotated_or_repaired", "operator": "equals", "value": True}],
        "REQUIREMENT_ARTIFACT_VALID": [{"field": "requirement.answer_artifact_valid", "operator": "equals", "value": True}, {"field": "requirement.binding_matches_feature", "operator": "equals", "value": True}],
        "RESPONSE_LOST_OR_EXECUTOR_TERMINATED": [{"field": "executor.failure_shape", "operator": "in", "value": ["response_lost", "executor_terminated"]}],
        "REVIEW_LOOP_OVERRIDE_DECISION_VALID": [{"field": "review.fix_cycle_count", "operator": "equals", "value": 3}, {"field": "decision.action", "operator": "equals", "value": "continue_fix"}, {"field": "decision.binding_valid", "operator": "equals", "value": True}],
        "SAME_SCOPE_KEY_NEW_APPROVAL_CAPABILITY_EPOCH_AND_ATTEMPT_INCREMENT": [{"field": "external_effect.scope_and_key_match", "operator": "equals", "value": True}, {"field": "approval.new_action_bound_receipt_valid", "operator": "equals", "value": True}, {"field": "capability.new_epoch_current", "operator": "equals", "value": True}, {"field": "external_effect.attempt_increment", "operator": "equals", "value": 1}],
        "SINGLE_RECONCILER_CLAIM": [{"field": "reconciler.active_claim_count", "operator": "equals", "value": 1}, {"field": "reconciler.claim_matches_effect_version", "operator": "equals", "value": True}],
        "APPROVAL_ACTION_RECEIPT": [{"field": "approval.action_receipt_valid", "operator": "equals", "value": True}],
        "OBSERVED_DEPLOY_ACTION_RECEIPT_VALID_AT_DEPLOYED_AT": [{"field": "approval.receipt_valid_at_effect_time", "operator": "equals", "value": True}, {"field": "external_fact.kind", "operator": "equals", "value": "deploy"}],
    }
    composite_parts = {
        "G5_REQUIRED_AND_NO_OPEN_SAFETY_OR_POLICY_INCIDENT": ["G5_REQUIRED", "NO_OPEN_SAFETY_OR_POLICY_INCIDENT"],
        "G5_REQUIRED_AND_NO_OPEN_SAFETY_OR_POLICY_INCIDENT_AND_APPROVAL_ACTION_RECEIPT": ["G5_REQUIRED", "NO_OPEN_SAFETY_OR_POLICY_INCIDENT", "APPROVAL_ACTION_RECEIPT"],
        "G5_REQUIRED_AND_NO_OPEN_SAFETY_OR_POLICY_INCIDENT_AND_OBSERVED_DEPLOY_ACTION_RECEIPT_VALID_AT_DEPLOYED_AT": ["G5_REQUIRED", "NO_OPEN_SAFETY_OR_POLICY_INCIDENT", "OBSERVED_DEPLOY_ACTION_RECEIPT_VALID_AT_DEPLOYED_AT"],
        "G5_REQUIRED_AND_MATCHING_MANAGED_MERGE_EFFECT": ["G5_REQUIRED", "MATCHING_MANAGED_MERGE_EFFECT"],
        "G5_REQUIRED_AND_NO_OPEN_SAFETY_OR_POLICY_INCIDENT_AND_MATCHING_MANAGED_DEPLOY_EFFECT": ["G5_REQUIRED", "NO_OPEN_SAFETY_OR_POLICY_INCIDENT", "MATCHING_MANAGED_DEPLOY_EFFECT"],
    }
    if base in composite_parts:
        for part in composite_parts[base]:
            if part not in exact_clauses:
                raise ValueError(f"unmapped guard predicate part: {part}")
            clauses.extend(exact_clauses[part])
    else:
        if base not in exact_clauses:
            raise ValueError(f"unmapped guard predicate: {base}")
        clauses.extend(exact_clauses[base])
    return clauses


def outcome_evidence_binding_clauses(schema_versions: list[str]) -> list[dict]:
    """Bind external-outcome evidence to the exact root, effect, action and target."""
    relevant = set(schema_versions) & {
        "dal.evidence.github-receipt/1.0",
        "dal.evidence.deployment-receipt/1.0",
        "dal.evidence.reconciliation/1.0",
    }
    if not relevant:
        return []
    if len(relevant) != 1:
        raise ValueError(f"ambiguous external-outcome evidence set: {sorted(relevant)}")
    clauses = [
        {"field": "evidence.subject_aggregate_type", "operator": "equals_field", "value": "root.aggregate_type"},
        {"field": "evidence.subject_aggregate_id", "operator": "equals_field", "value": "root.aggregate_id"},
        {"field": "evidence.subject_aggregate_version", "operator": "equals_field", "value": "root.version"},
        {"field": "evidence.external_effect_id", "operator": "equals_field", "value": "external_effect.effect_id"},
        {"field": "evidence.external_effect_version", "operator": "equals_field", "value": "external_effect.version"},
        {"field": "evidence.effect_attempt", "operator": "equals_field", "value": "external_effect.attempt"},
        {"field": "evidence.effect_action", "operator": "equals_field", "value": "external_effect.action"},
        {"field": "evidence.effect_scope_key", "operator": "equals_field", "value": "external_effect.effect_scope_key"},
        {"field": "evidence.remote_idempotency_key", "operator": "equals_field", "value": "external_effect.remote_idempotency_key"},
        {"field": "evidence.effect_state", "operator": "equals_field", "value": "command.effect_outcome"},
        {"field": "evidence.target_fingerprint", "operator": "equals_field", "value": "external_effect.target_fingerprint"},
        {"field": "evidence.payload_sha256", "operator": "equals_field", "value": "protected_evidence.payload_sha256"},
        {"field": "evidence.protected_ref", "operator": "equals_field", "value": "protected_evidence.ref"},
        {"field": "evidence.authoritative_readback_sha256", "operator": "equals_field", "value": "protected_evidence.authoritative_readback_sha256"},
        {"field": "evidence.impact_sha256", "operator": "equals_field", "value": "protected_evidence.impact_sha256"},
        {"field": "evidence.semantic_binding_sha256", "operator": "equals_field", "value": "protected_evidence.semantic_binding_sha256"},
        {"field": "evidence.semantic_binding_sha256", "operator": "equals_field", "value": "runtime.recomputed_evidence_semantic_binding_sha256"},
    ]
    schema_version = next(iter(relevant))
    if schema_version == "dal.evidence.github-receipt/1.0":
        clauses.extend([
            {"field": "evidence.repository_id", "operator": "equals_field", "value": "external_effect.repository_id"},
            {"field": "evidence.pull_request_id", "operator": "equals_field", "value": "external_effect.pull_request_id"},
            {"field": "evidence.head_sha", "operator": "equals_field", "value": "external_effect.head_sha"},
            {"field": "evidence.merge_sha", "operator": "equals_field", "value": "protected_evidence.merge_sha"},
            {"field": "evidence.remote_receipt_id", "operator": "equals_field", "value": "protected_evidence.remote_receipt_id"},
        ])
    elif schema_version == "dal.evidence.deployment-receipt/1.0":
        clauses.extend([
            {"field": "evidence.environment_id", "operator": "equals_field", "value": "external_effect.environment_id"},
            {"field": "evidence.deployment_target_id", "operator": "equals_field", "value": "external_effect.deployment_target_id"},
            {"field": "evidence.version_digest", "operator": "equals_field", "value": "external_effect.version_digest"},
            {"field": "evidence.remote_receipt_id", "operator": "equals_field", "value": "protected_evidence.remote_receipt_id"},
        ])
    else:
        clauses.extend([
            {"field": "evidence.decision_action", "operator": "equals_field", "value": "command.decision_action"},
            {"field": "evidence.effect_result", "operator": "equals_field", "value": "command.effect_outcome"},
            {"field": "evidence.authoritative_receipt_id", "operator": "equals_field", "value": "protected_evidence.authoritative_receipt_id"},
            {"field": "evidence_set.registered_device.semantic_binding_sha256", "operator": "equals_field", "value": "runtime.recomputed_registered_device_semantic_binding_sha256"},
            {"field": "evidence_set.external_effect_controller.semantic_binding_sha256", "operator": "equals_field", "value": "runtime.recomputed_external_effect_controller_semantic_binding_sha256"},
            {"field": "evidence_set.registered_device.semantic_binding_sha256", "operator": "equals_field", "value": "evidence_set.external_effect_controller.semantic_binding_sha256"},
            {"field": "evidence_set.registered_device.semantic_binding_sha256", "operator": "equals_field", "value": "protected_evidence.semantic_binding_sha256"},
        ])
    return clauses


def transition_guard_clauses(item: dict) -> list[dict]:
    if item["guard_id"] is None:
        if outcome_evidence_binding_clauses(item["required_evidence_schema_versions"]):
            raise ValueError(f"external-outcome transition lacks semantic guard: {item['spec_id']}")
        return []
    return [
        *guard_clauses(item["guard_id"], item["to_state"]),
        *outcome_evidence_binding_clauses(item["required_evidence_schema_versions"]),
    ]


def build_machine_registries(specs: list[dict]) -> tuple[str, str]:
    evidence_versions = sorted({version for row in specs for version in row["required_evidence_schema_versions"]} | {version for row in specs for related in row["atomic_companion_transitions"] for version in related["required_evidence_schema_versions"]})
    evidence_rows = []
    for schema_version in evidence_versions:
        claim_fields = evidence_claim_fields(schema_version)
        all_fields = {**COMMON_EVIDENCE_FIELDS, **claim_fields}
        evidence_schema = {
            "type": "object", "additionalProperties": False,
            "required": list(all_fields),
            "properties": {name: ({"const": schema_version} if name == "schema_version" else json_type(kind)) for name, kind in all_fields.items()},
        }
        if schema_version == "dal.evidence.reconciliation/1.0":
            evidence_schema["allOf"] = [{
                "if": {"properties": {"effect_result": {"const": "confirmed_completed"}}, "required": ["effect_result"]},
                "then": {"properties": {"authoritative_receipt_id": {
                    "type": "string", "minLength": 1,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
                }}},
                "else": {"properties": {"authoritative_receipt_id": {"type": "null"}}},
            }]
        evidence_rows.append({
            "schema_version": schema_version,
            "source_type_binding": sorted({source for row in specs if schema_version in row["required_evidence_schema_versions"] for source in row["allowed_evidence_source_types"]}),
            "json_schema": evidence_schema,
        })
    evidence_catalog = {"schema_version": "dal.evidence-schema-registry/1.0", "evidence_schemas": evidence_rows, "registry_sha256": None}
    evidence_hash = write_hashed("evidence-schema-registry_v1.0.json", evidence_catalog, "registry_sha256")

    guard_rows = []
    seen: dict[str, list[dict]] = {}
    for row in specs:
        guard_id = row["guard_id"]
        if guard_id is None:
            continue
        clauses = transition_guard_clauses(row)
        if guard_id in seen:
            if seen[guard_id] != clauses:
                raise ValueError(f"guard id has divergent predicates: {guard_id}")
            continue
        seen[guard_id] = clauses
        guard_rows.append({
            "guard_id": guard_id,
            "predicate_schema_version": "dal.guard-predicate/1.0",
            "all_of": clauses,
            "missing_field_result": False,
            "evaluation_error_result": False,
        })
    guard_rows.sort(key=lambda item: item["guard_id"])
    guard_catalog = {"schema_version": "dal.guard-predicate-registry/1.0", "guards": guard_rows, "registry_sha256": None}
    guard_hash = write_hashed("guard-predicate-registry_v1.0.json", guard_catalog, "registry_sha256")
    return evidence_hash, guard_hash


def write_hashed(name: str, value: dict, hash_field: str) -> str:
    body = dict(value)
    body.pop(hash_field, None)
    value[hash_field] = digest(body)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return value[hash_field]


def spec(
    spec_id: str,
    from_state: str | None,
    to_state: str,
    command: str,
    event: str,
    actors: list[str],
    sources: list[str],
    reasons: list[str],
    evidence: str,
    action: str | None,
    write_set: str | list[str],
    guard: str | None = None,
    aggregate_type: str = "feature",
    decision_reasons: list[str] | None = None,
    evidence_schemas: list[str] | None = None,
    actor_evidence_bindings: list[dict] | None = None,
    minimum_run_gate: str = "G1",
    result_reason_owner: str | None = None,
    result_reason_code: str | None = None,
    atomic_companion_transitions: list[dict] | None = None,
) -> dict:
    stopped = to_state.startswith("blocked_") or to_state in {"needs_human", "reconciliation_required", "paused"}
    derived_reason_code = reasons[-1] if reasons and stopped else None
    if result_reason_code is None:
        result_reason_code = derived_reason_code
    if result_reason_owner is None and result_reason_code is not None:
        result_reason_owner = aggregate_type
    if actor_evidence_bindings is None:
        if len(actors) == len(sources) and len(actors) > 1:
            actor_evidence_bindings = [
                {"actor_type": actor, "required_evidence_source_types": [source]}
                for actor, source in zip(actors, sources)
            ]
        else:
            actor_evidence_bindings = [
                {"actor_type": actor, "required_evidence_source_types": list(sources)}
                for actor in actors
            ]
    receipt_schema = {
        "feature": "dal.transition-receipt/1.0",
        "recovery_case": "dal.recovery-transition-receipt/1.0",
        "external_effect": "dal.external-effect-transition-receipt/1.0",
    }[aggregate_type]
    effect_outcome = (
        "confirmed_not_executed" if "RECONCILE-NOT-EXECUTED" in spec_id
        else "confirmed_completed" if "RECONCILE-COMPLETED-NONSTATE" in spec_id
        else None
    )
    return {
        "schema_version": "dal.transition-spec/1.0",
        "spec_id": spec_id,
        "aggregate_type": aggregate_type,
        "from_state": from_state,
        "to_state": to_state,
        "command_type": command,
        "command_parameters": {"target_state": to_state, "effect_outcome": effect_outcome},
        "event_type": event,
        "allowed_actor_types": actors,
        "allowed_evidence_source_types": sources,
        "actor_evidence_bindings": actor_evidence_bindings,
        "allowed_reason_codes": reasons,
        "allowed_decision_reason_codes": decision_reasons or [],
        "result_reason_owner": result_reason_owner,
        "result_reason_code": result_reason_code,
        "evidence_schema_version": evidence,
        "required_evidence_schema_versions": evidence_schemas or [evidence],
        "requires_decision_action": action,
        "atomic_write_set": expanded_write_set(write_set, spec_id) if isinstance(write_set, str) else write_set,
        "atomic_companion_transitions": atomic_companion_transitions or [],
        "guard_id": guard,
        "minimum_run_gate": minimum_run_gate,
        "success_receipt_schema": receipt_schema,
        "success_receipt_code": "APPLIED",
    }


def build_transition_registry() -> tuple[list[dict], str]:
    rows: list[dict] = []
    add = rows.append
    normal = [
        ("SM-CREATE", None, "intake", "create_feature", "feature.created", ["service"], ["workflow-service"], "dal.evidence.feature/1.0", None, "A"),
        ("SM-PLAN-START", "intake", "planning", "start_plan", "plan.started", ["service"], ["workflow-service"], "dal.evidence.plan-start/1.0", None, "A"),
        ("SM-PLAN-READY", "planning", "awaiting_plan_review", "record_plan", "plan.ready", ["service"], ["planner"], "dal.evidence.plan/1.0", None, "D"),
        ("SM-PLAN-REVISE", "awaiting_plan_review", "planning", "request_revision", "plan.revision_requested", ["human"], ["registered-device"], "dal.evidence.revision/1.0", "request_revision", "PLAN_REVISE"),
        ("SM-PLAN-APPROVE", "awaiting_plan_review", "approved", "approve_plan", "plan.approved", ["human"], ["registered-device"], "dal.evidence.approval/1.0", "approve_plan", "P"),
        ("SM-PROVIDER-START", "approved", "coding", "start_provider", "provider.started", ["service"], ["workflow-service"], "dal.evidence.provider-start/1.0", None, "A"),
        ("SM-PROVIDER-DONE", "coding", "verifying", "record_provider_result", "provider.completed", ["service"], ["worker"], "dal.evidence.patch/1.0", None, "A"),
        ("SM-VERIFY-PASS", "verifying", "reviewing", "record_verification/pass", "verification.completed", ["service"], ["verifier"], "dal.evidence.test-receipt/1.0", None, "A"),
        ("SM-VERIFY-FIX", "verifying", "fixing", "record_verification/fixable_fail", "verification.completed", ["service"], ["verifier"], "dal.evidence.test-receipt/1.0", None, "A"),
        ("SM-VERIFY-BLOCK", "verifying", "blocked_test", "record_verification/blocked", "feature.blocked", ["service"], ["verifier"], "dal.evidence.test-receipt/1.0", None, "D"),
        ("SM-REVIEW-PASS", "reviewing", "verified", "record_review/pass", "review.completed", ["service"], ["reviewer"], "dal.evidence.review/1.0", None, "A"),
        ("SM-REVIEW-FIX", "reviewing", "fixing", "record_review/findings", "fix.requested", ["service"], ["reviewer"], "dal.evidence.review/1.0", None, "A"),
        ("SM-FIX-DONE", "fixing", "verifying", "record_fix_result", "fix.completed", ["service"], ["worker"], "dal.evidence.patch/1.0", None, "A"),
        ("SM-MERGE-WAIT", "verified", "awaiting_merge", "record_merge_candidate", "merge.awaiting_approval", ["service"], ["github-control"], "dal.evidence.pr-snapshot/1.0", None, "D"),
        ("SM-MERGE-APPROVAL", "awaiting_merge", "awaiting_merge", "record_merge_approval", "approval.recorded", ["human"], ["registered-device"], "dal.evidence.merge-approval/1.0", "approve_merge", "J"),
        ("SM-MERGED-OBSERVED", "awaiting_merge", "merged", "record_observed_merge", "merge.completed", ["service"], ["github-control"], "dal.evidence.github-observed-merge/1.0", None, "O"),
        ("SM-MERGED-OBSERVED-UNAPPROVED", "awaiting_merge", "merged", "record_unapproved_observed_merge", "merge.completed", ["service"], ["github-control"], "dal.evidence.github-observed-merge/1.0", None, "U"),
        ("SM-MERGE-INTENT", "awaiting_merge", "awaiting_merge", "start_managed_merge", "feature.managed_merge_started", ["service"], ["github-control"], "dal.evidence.github-merge-intent/1.0", "execute_merge", "I"),
        ("SM-MERGED-DISPATCHED", "awaiting_merge", "merged", "record_managed_merge", "merge.completed", ["service"], ["github-control"], "dal.evidence.github-receipt/1.0", None, "X"),
        ("SM-DEPLOY-APPROVAL", "merged", "merged", "record_deploy_approval", "approval.recorded", ["human"], ["registered-device"], "dal.evidence.deploy-approval/1.0", "approve_deploy", "J"),
        ("SM-DEPLOYED-OBSERVED", "merged", "deployed", "record_observed_deployment", "deployment.completed", ["service"], ["delivery-control"], "dal.evidence.observed-deployment/1.0", None, "O"),
        ("SM-DEPLOYED-OBSERVED-UNAPPROVED", "merged", "deployed", "record_unapproved_observed_deployment", "deployment.completed", ["service"], ["delivery-control"], "dal.evidence.observed-deployment/1.0", None, "U"),
        ("SM-DEPLOY-INTENT", "merged", "merged", "start_managed_deploy", "feature.managed_deploy_started", ["service"], ["delivery-control"], "dal.evidence.deployment-intent/1.0", "execute_deploy", "I"),
        ("SM-DEPLOYED", "merged", "deployed", "record_deployment", "deployment.completed", ["service"], ["delivery-control"], "dal.evidence.deployment-receipt/1.0", None, "X"),
        ("SM-COMPLETE-MERGED", "merged", "completed", "complete_without_deploy", "feature.completed", ["service"], ["github-control"], "dal.evidence.completion/1.0", None, "A"),
        ("SM-COMPLETE-DEPLOYED", "deployed", "completed", "complete_after_deploy", "feature.completed", ["service"], ["delivery-control"], "dal.evidence.production-verification/1.0", None, "A"),
    ]
    guard_overrides = {
        "SM-MERGE-APPROVAL": "PR_ONLY_APPROVAL_ACTION_RECEIPT",
        "SM-MERGED-OBSERVED": "OBSERVED_MERGE_ACTION_RECEIPT_VALID_AT_MERGED_AT",
        "SM-MERGED-OBSERVED-UNAPPROVED": "OBSERVED_MERGE_WITHOUT_VALID_ACTION_RECEIPT",
        "SM-DEPLOY-APPROVAL": "G5_REQUIRED_AND_NO_OPEN_SAFETY_OR_POLICY_INCIDENT_AND_APPROVAL_ACTION_RECEIPT",
        "SM-DEPLOYED-OBSERVED": "G5_REQUIRED_AND_NO_OPEN_SAFETY_OR_POLICY_INCIDENT_AND_OBSERVED_DEPLOY_ACTION_RECEIPT_VALID_AT_DEPLOYED_AT",
        "SM-DEPLOYED-OBSERVED-UNAPPROVED": "OBSERVED_DEPLOY_WITHOUT_VALID_ACTION_RECEIPT",
        "SM-DEPLOY-INTENT": "G5_REQUIRED_AND_NO_OPEN_SAFETY_OR_POLICY_INCIDENT",
        "SM-MERGED-DISPATCHED": "G5_REQUIRED_AND_MATCHING_MANAGED_MERGE_EFFECT",
        "SM-DEPLOYED": "G5_REQUIRED_AND_NO_OPEN_SAFETY_OR_POLICY_INCIDENT_AND_MATCHING_MANAGED_DEPLOY_EFFECT",
        "SM-COMPLETE-MERGED": "NO_OPEN_SAFETY_OR_POLICY_INCIDENT",
        "SM-COMPLETE-DEPLOYED": "NO_OPEN_SAFETY_OR_POLICY_INCIDENT",
    }
    for row in normal:
        reasons = ["TEST_BLOCKED"] if row[0] == "SM-VERIFY-BLOCK" else []
        guard = guard_overrides.get(row[0], "G5_REQUIRED" if row[0].startswith("SM-DEPLOY") or row[0] in {"SM-MERGE-INTENT", "SM-MERGED-DISPATCHED"} else None)
        decision_reasons = ["POLICY_FAILURE"] if row[0] in {"SM-MERGED-OBSERVED-UNAPPROVED", "SM-DEPLOYED-OBSERVED-UNAPPROVED"} else []
        minimum_gate = "G5" if row[0] in {"SM-MERGE-INTENT", "SM-MERGED-DISPATCHED", "SM-DEPLOY-APPROVAL", "SM-DEPLOYED-OBSERVED", "SM-DEPLOY-INTENT", "SM-DEPLOYED"} else "G1"
        add(spec(row[0], row[1], row[2], row[3], row[4], row[5], row[6], reasons, row[7], row[8], row[9], guard, decision_reasons=decision_reasons, minimum_run_gate=minimum_gate))

    pause_states = ["planning", "awaiting_plan_review", "approved", "coding", "verifying", "reviewing", "fixing", "verified", "awaiting_merge"]
    cancel_states = ["intake", *pause_states, "blocked_requirement", "blocked_usage", "blocked_auth", "blocked_test", "blocked_external_prerequisite", "blocked_unknown", "needs_human", "paused"]
    for state in pause_states:
        add(spec(f"SM-PAUSE--{state}", state, "paused", "pause_feature", "feature.paused", ["human", "service"], ["registered-device", "workflow-service"], ["USER_PAUSE"], "dal.evidence.checkpoint/1.0", "pause", "K"))
    for state in cancel_states:
        add(spec(f"SM-CANCEL-NO-EFFECTS--{state}", state, "cancelled", "cancel_feature", "feature.cancelled", ["human"], ["registered-device"], [], "dal.evidence.cancellation-impact/1.0", "cancel_feature", "K", "CANCEL_NO_CONFIRMED_OR_UNKNOWN_EFFECTS"))
        add(spec(f"SM-CANCEL-WITH-EFFECTS--{state}", state, "cancelled", "cancel_feature", "feature.cancelled", ["human"], ["registered-device"], [], "dal.evidence.cancellation-impact/1.0", "cancel_with_effects", "K", "CANCEL_CONFIRMED_EFFECTS_AND_NO_UNKNOWN_EFFECTS"))

    blocks = [
        ("BLK-REQUIREMENT", ["planning"], "blocked_requirement", "REQUIREMENT_MISSING", "workflow-service", "dal.evidence.requirement-gap/1.0"),
        ("BLK-USAGE", ["planning", "coding", "reviewing", "fixing"], "blocked_usage", "USAGE_LIMIT", "provider-adapter", "dal.evidence.provider-failure/1.0"),
        ("BLK-AUTH", ["planning", "coding", "reviewing", "fixing"], "blocked_auth", "AUTH_REQUIRED", "provider-adapter", "dal.evidence.provider-failure/1.0"),
        ("BLK-PREREQ", ["planning", "approved", "coding", "verifying", "reviewing", "fixing"], "blocked_external_prerequisite", "EXTERNAL_PREREQUISITE", "preflight-control", "dal.evidence.prerequisite/1.0"),
        ("BLK-TRANSIENT", ["planning", "coding", "reviewing", "fixing"], "needs_human", "TRANSIENT_RETRY_EXHAUSTED", "provider-adapter", "dal.evidence.provider-attempts/1.0"),
        ("BLK-CONTRACT", ["planning", "coding", "reviewing", "fixing"], "needs_human", "PROVIDER_CONTRACT_FAILURE", "provider-adapter", "dal.evidence.provider-failure/1.0"),
        ("BLK-TEST-FIXING", ["fixing"], "blocked_test", "TEST_BLOCKED", "verifier", "dal.evidence.test-receipt/1.0"),
        ("BLK-POLICY", pause_states, "needs_human", "POLICY_FAILURE", "policy-engine", "dal.evidence.policy-failure/1.0"),
        ("BLK-BUDGET", ["planning", "coding", "reviewing", "fixing"], "needs_human", "BUDGET_LIMIT", "budget-controller", "dal.evidence.budget/1.0"),
        ("BLK-LOOP", ["fixing"], "needs_human", "REVIEW_LOOP_LIMIT", "workflow-service", "dal.evidence.review-loop/1.0"),
        ("BLK-GIT", ["approved", "coding", "verifying", "reviewing", "fixing", "verified", "awaiting_merge"], "needs_human", "GIT_CONFLICT", "github-control", "dal.evidence.git-readback/1.0"),
        ("BLK-DRIFT", ["awaiting_plan_review", "approved", "coding", "verifying", "reviewing", "fixing", "verified", "awaiting_merge"], "needs_human", "STATE_DRIFT", "workflow-service", "dal.evidence.state-drift/1.0"),
        ("BLK-POST-EFFECT", ["merged", "deployed"], "needs_human", "POST_EFFECT_EXCEPTION", "workflow-service", "dal.evidence.post-effect-failure/1.0"),
    ]
    for family, states, target, reason, source, evidence in blocks:
        for state in states:
            add(spec(f"{family}--{state}", state, target, "block_feature", "feature.blocked", ["service"], [source], [reason], evidence, None, "D"))

    unknown_sources = ["intake", "planning", "awaiting_plan_review", "approved", "coding", "verifying", "reviewing", "fixing", "verified", "awaiting_merge", "merged", "deployed"]
    for state in unknown_sources:
        add(spec(f"BLK-UNKNOWN--{state}", state, "blocked_unknown", "block_unknown", "feature.blocked", ["service"], ["workflow-service"], ["UNKNOWN_NO_EXTERNAL_INTENT"], "dal.evidence.effect-inventory/1.0", None, "E", "NO_EXTERNAL_INTENT"))
    for state in [*unknown_sources, "blocked_unknown"]:
        add(spec(f"REC-UNKNOWN--{state}", state, "reconciliation_required", "require_reconciliation", "reconciliation.required", ["service"], ["external-effect-controller"], ["EXTERNAL_RESULT_UNKNOWN"], "dal.evidence.effect-inventory/1.0", None, "E", "UNKNOWN_EXTERNAL_EFFECT"))

    resume_map = {
        "REQUIREMENT_MISSING": ("blocked_requirement", ["planning"], "supply_requirement", "REQUIREMENT_ARTIFACT_VALID", ["dal.evidence.requirement-answer/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "USAGE_LIMIT": ("blocked_usage", ["planning", "coding", "reviewing", "fixing"], "resume_frozen_route", "FROZEN_ROUTE_RESUME_OR_FALLBACK_ELIGIBLE", ["dal.evidence.route-resume/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "AUTH_REQUIRED": ("blocked_auth", ["planning", "coding", "reviewing", "fixing"], "resume_after_auth", "READ_ONLY_AUTH_PROBE_VALID", ["dal.evidence.auth-probe/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "TEST_BLOCKED": ("blocked_test", ["fixing"], "continue_fix", "ACTION_BOUND_TEST_DECISION_VALID", ["dal.evidence.test-decision/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "EXTERNAL_PREREQUISITE": ("blocked_external_prerequisite", ["planning", "approved", "coding", "verifying", "reviewing", "fixing"], "resume_after_prerequisite", "PREREQUISITE_READBACK_VALID", ["dal.evidence.prerequisite/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "TRANSIENT_RETRY_EXHAUSTED": ("needs_human", ["planning", "coding", "reviewing", "fixing"], "retry_from_checkpoint", "TWO_ATTEMPTS_CONSUMED_AND_NEW_DECISION", ["dal.evidence.provider-attempts/1.0", "dal.evidence.decision/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "PROVIDER_CONTRACT_FAILURE": ("needs_human", ["planning", "coding", "reviewing", "fixing"], "retry_from_checkpoint", "PROVIDER_CONTRACT_STABLE_AND_NEW_DECISION", ["dal.evidence.provider-failure/1.0", "dal.evidence.decision/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "POLICY_FAILURE": ("needs_human", ["planning"], "replan", "CAPABILITY_EPOCH_REVOKED_AND_NEW_PLAN", ["dal.evidence.policy-replan/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "BUDGET_LIMIT": ("needs_human", ["planning", "coding", "reviewing", "fixing"], "resume_with_budget", "NEW_BUDGET_DECISION_VALID", ["dal.evidence.budget-approval/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "REVIEW_LOOP_LIMIT": ("needs_human", ["fixing"], "continue_fix", "REVIEW_LOOP_OVERRIDE_DECISION_VALID", ["dal.evidence.review-loop-decision/1.0", "dal.evidence.drift-probe/1.0"], "P"),
        "GIT_CONFLICT": ("needs_human", ["planning"], "replan", "GIT_READBACK_AND_NEW_BASE_APPROVAL_VALID", ["dal.evidence.git-readback/1.0", "dal.evidence.plan-approval/1.0"], "P"),
        "STATE_DRIFT": ("needs_human", ["planning"], "replan", "CURRENT_FACT_AND_NEW_ARTIFACT_APPROVAL_VALID", ["dal.evidence.state-drift/1.0", "dal.evidence.plan-approval/1.0"], "P"),
        "USER_PAUSE": ("paused", pause_states, "resume_checkpoint", "DRIFT_PROBE_VALID_AND_NEW_LEASE", ["dal.evidence.drift-probe/1.0", "dal.evidence.lease/1.0"], "P_LEASE"),
        "UNKNOWN_NO_EXTERNAL_INTENT": ("blocked_unknown", unknown_sources, "resume_checkpoint", "ALL_EFFECTS_CONFIRMED_NOT_EXECUTED_AND_NEW_DECISION", ["dal.evidence.effect-inventory/1.0", "dal.evidence.decision/1.0", "dal.evidence.drift-probe/1.0"], "P"),
    }
    for reason, (from_state, targets, action, reason_guard, evidence_schemas, write_set) in resume_map.items():
        for target in targets:
            add(spec(
                f"RESUME--{reason}--{target}", from_state, target, action, "feature.resumed",
                ["human"], ["registered-device"], [reason], evidence_schemas[0], action, write_set,
                f"{reason_guard}_AND_CHECKPOINT_{target.upper()}", evidence_schemas=evidence_schemas,
            ))

    add(spec(
        "RESUME--PROVIDER_CONTRACT_FAILURE--REPLAN", "needs_human", "planning", "replan",
        "feature.resumed", ["human"], ["registered-device"], ["PROVIDER_CONTRACT_FAILURE"],
        "dal.evidence.provider-contract-replan/1.0", "replan", "P",
        "ARTIFACT_OR_PROFILE_DRIFT_AND_NEW_PLAN_APPROVAL",
        evidence_schemas=["dal.evidence.provider-contract-replan/1.0", "dal.evidence.plan-approval/1.0"],
    ))

    for reason in ("POST_EFFECT_EXCEPTION", "RECOVERY_REQUIRED"):
        for target in ("merged", "deployed"):
            add(spec(
                f"RESUME--{reason}--ACCEPT--{target}", "needs_human", target, "accept_current_fact",
                "feature.resumed", ["human"], ["registered-device", "external-effect-controller"], [reason],
                "dal.evidence.post-effect-readback/1.0", "accept_current_fact", "P",
                f"AUTHORITATIVE_FACT_ACCEPTED_AND_CHECKPOINT_{target.upper()}",
                evidence_schemas=["dal.evidence.post-effect-readback/1.0", "dal.evidence.decision/1.0", "dal.evidence.drift-probe/1.0"],
            ))
    for target in ("merged", "deployed"):
        add(spec(
            f"RESUME--RECOVERY_REQUIRED--VERIFIED--{target}", "needs_human", target,
            "complete_verified_recovery", "feature.resumed", ["human"],
            ["registered-device", "external-effect-controller"], ["RECOVERY_REQUIRED"],
            "dal.evidence.recovery-verification/1.0", "complete_verified_recovery", "P",
            f"MATCHING_RECOVERY_CASE_VERIFIED_AND_CHECKPOINT_{target.upper()}",
            evidence_schemas=["dal.evidence.recovery-verification/1.0", "dal.evidence.decision/1.0", "dal.evidence.drift-probe/1.0"],
        ))

    for target in unknown_sources:
        for suffix, guard in (("NOT-EXECUTED", "EFFECT_RECONCILING_NOT_EXECUTED"), ("COMPLETED-NONSTATE", "EFFECT_RECONCILING_COMPLETED_NONSTATE")):
            add(spec(f"RECONCILE-{suffix}--{target}", "reconciliation_required", target, "resume_checkpoint", "feature.resumed", ["human"], ["registered-device", "external-effect-controller"], ["EXTERNAL_RESULT_UNKNOWN"], "dal.evidence.reconciliation/1.0", "resume_checkpoint", "R", f"{guard}_AND_CHECKPOINT_{target.upper()}"))
    add(spec("RECONCILE-MERGE", "reconciliation_required", "merged", "accept_merge_result", "merge.completed", ["human"], ["registered-device", "github-control"], ["EXTERNAL_RESULT_UNKNOWN"], "dal.evidence.github-observed-merge/1.0", "accept_merge_result", "R", "EFFECT_IS_MERGE"))
    add(spec("RECONCILE-DEPLOY", "reconciliation_required", "deployed", "accept_deploy_result", "deployment.completed", ["human"], ["registered-device", "delivery-control"], ["EXTERNAL_RESULT_UNKNOWN"], "dal.evidence.observed-deployment/1.0", "accept_deploy_result", "R", "EFFECT_IS_DEPLOY"))
    add(spec(
        "RECOVERY-OPEN--POST-EFFECT", "needs_human", "needs_human", "open_recovery_case",
        "feature.recovery_required", ["human"], ["registered-device", "external-effect-controller"], ["POST_EFFECT_EXCEPTION"],
        "dal.evidence.recovery-proposal/1.0", "open_recovery_case", "Q",
        "AUTHORITATIVE_POST_EFFECT_READBACK_AND_RECOVERY_PROPOSAL",
        evidence_schemas=["dal.evidence.recovery-proposal/1.0", "dal.evidence.post-effect-readback/1.0"],
        result_reason_owner="feature", result_reason_code="RECOVERY_REQUIRED",
    ))
    add(spec(
        "RECONCILE-OPEN-RECOVERY", "reconciliation_required", "needs_human", "open_recovery",
        "feature.recovery_required", ["human"], ["registered-device", "external-effect-controller"],
        ["EXTERNAL_RESULT_UNKNOWN"], "dal.evidence.recovery-proposal/1.0", "open_recovery", "R",
        result_reason_owner="feature", result_reason_code="RECOVERY_REQUIRED",
    ))

    recovery_base = ["recovery_case", "business_event", "recovery_transition_receipt", "audit"]
    recovery_specs = [
        ("RC-PROPOSAL", "investigating", "awaiting_decision", "record_recovery_proposal", "recovery_case.awaiting_decision", ["service"], ["external-effect-controller"], "dal.evidence.recovery-proposal/1.0", None, [*recovery_base, "decision", "decision_projection", "notification_outbox"]),
        ("RC-INVESTIGATION-BLOCK", "investigating", "blocked", "block_recovery_investigation", "recovery_case.blocked", ["service"], ["external-effect-controller"], "dal.evidence.recovery-investigation/1.0", None, [*recovery_base, "decision", "decision_projection", "evidence", "notification_outbox"]),
        ("RC-CANCEL-INVESTIGATING", "investigating", "cancelled", "cancel_recovery", "recovery_case.cancelled", ["human"], ["registered-device"], "dal.evidence.recovery-cancellation/1.0", "cancel_recovery", ["decision_consume", *recovery_base, "notification_outbox"]),
        ("RC-APPROVE", "awaiting_decision", "approved", "approve_recovery", "recovery_case.approved", ["human"], ["registered-device"], "dal.evidence.recovery-approval/1.0", "approve_recovery", ["approval_consume", "capability_issue", *recovery_base, "notification_outbox"]),
        ("RC-CANCEL-AWAITING", "awaiting_decision", "cancelled", "cancel_recovery", "recovery_case.cancelled", ["human"], ["registered-device"], "dal.evidence.recovery-cancellation/1.0", "cancel_recovery", ["decision_consume", *recovery_base, "notification_outbox"]),
        ("RC-START", "approved", "executing", "start_recovery", "recovery_case.executing", ["service"], ["recovery-executor"], "dal.evidence.recovery-capability/1.0", None, ["capability_consume", "external_effect_intent", *recovery_base, "notification_outbox"]),
        ("RC-START-BLOCK", "approved", "blocked", "block_recovery_start", "recovery_case.blocked", ["service"], ["policy-engine"], "dal.evidence.recovery-policy/1.0", None, ["capability_revoke", *recovery_base, "decision", "decision_projection", "evidence", "notification_outbox"]),
        ("RC-CANCEL-APPROVED", "approved", "cancelled", "cancel_recovery", "recovery_case.cancelled", ["human"], ["registered-device"], "dal.evidence.recovery-cancellation/1.0", "cancel_recovery", ["decision_consume", "capability_revoke", *recovery_base, "notification_outbox"]),
        ("RC-EXECUTED", "executing", "verifying", "record_recovery_execution", "recovery_case.verifying", ["service"], ["recovery-executor"], "dal.evidence.recovery-execution/1.0", None, [*recovery_base, "evidence", "notification_outbox"]),
        ("RC-EXECUTION-BLOCK", "executing", "blocked", "block_recovery", "recovery_case.blocked", ["service"], ["recovery-executor", "external-effect-controller"], "dal.evidence.recovery-effect-inventory/1.0", None, [*recovery_base, "decision", "decision_projection", "evidence", "notification_outbox"]),
        ("RC-VERIFICATION-BLOCK", "verifying", "blocked", "block_recovery", "recovery_case.blocked", ["service"], ["recovery-executor", "external-effect-controller"], "dal.evidence.recovery-effect-inventory/1.0", None, [*recovery_base, "decision", "decision_projection", "evidence", "notification_outbox"]),
        ("RC-VERIFIED", "verifying", "verified", "verify_recovery", "recovery_case.verified", ["service"], ["external-effect-controller"], "dal.evidence.recovery-verification/1.0", None, [*recovery_base, "evidence", "notification_outbox"]),
        ("RC-REINVESTIGATE", "blocked", "investigating", "reinvestigate_recovery", "recovery_case.reinvestigating", ["human"], ["registered-device"], "dal.evidence.recovery-reinvestigation/1.0", "reinvestigate_recovery", ["decision_consume", "capability_revoke", *recovery_base, "notification_outbox"]),
        ("RC-REARM", "blocked", "awaiting_decision", "replace_recovery_proposal", "recovery_case.awaiting_decision", ["human"], ["registered-device"], "dal.evidence.recovery-proposal/1.0", "replace_recovery_proposal", ["decision_consume", *recovery_base, "decision", "decision_projection", "notification_outbox"]),
        ("RC-CANCEL-BLOCKED", "blocked", "cancelled", "cancel_recovery", "recovery_case.cancelled", ["human"], ["registered-device"], "dal.evidence.recovery-cancellation/1.0", "cancel_recovery", ["decision_consume", *recovery_base, "impact", "notification_outbox"]),
    ]
    recovery_block_reasons = {
        "RC-INVESTIGATION-BLOCK": "RECOVERY_READBACK_UNKNOWN",
        "RC-START-BLOCK": "RECOVERY_POLICY_STALE",
        "RC-EXECUTION-BLOCK": "RECOVERY_EFFECT_UNKNOWN",
        "RC-VERIFICATION-BLOCK": "RECOVERY_EFFECT_UNKNOWN",
    }
    recovery_outgoing_reasons = {"RECOVERY_READBACK_UNKNOWN", "RECOVERY_POLICY_STALE", "RECOVERY_EFFECT_UNKNOWN"}
    for row in recovery_specs:
        reason = recovery_block_reasons.get(row[0])
        if row[0] in {"RC-REINVESTIGATE", "RC-REARM", "RC-CANCEL-BLOCKED"}:
            for outgoing_reason in sorted(recovery_outgoing_reasons):
                add(spec(
                    f"{row[0]}--{outgoing_reason}", row[1], row[2], row[3], row[4], row[5], row[6],
                    [outgoing_reason], row[7], row[8], row[9], aggregate_type="recovery_case",
                ))
            continue
        reasons = [reason] if reason else []
        bindings = None
        if row[0] in {"RC-EXECUTION-BLOCK", "RC-VERIFICATION-BLOCK"}:
            bindings = [
                {"actor_type": "service", "required_evidence_source_types": ["recovery-executor"]},
                {"actor_type": "service", "required_evidence_source_types": ["external-effect-controller"]},
            ]
        add(spec(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], reasons,
            row[7], row[8], row[9], aggregate_type="recovery_case",
            actor_evidence_bindings=bindings,
            result_reason_owner="recovery_case" if reason else None,
            result_reason_code=reason,
        ))

    effect_specs = [
        ("EE-CLAIM", "intent_recorded", "claimed", "claim_external_effect", "external_effect.claimed", ["service"], ["external-effect-controller"], "dal.evidence.effect-claim/1.0", None, [*EFFECT_BASE_WRITES, "executor_claim"], "CAPABILITY_AND_LEASE_EPOCH_CURRENT"),
        ("EE-DISPATCH", "claimed", "dispatch_started", "record_effect_dispatch", "external_effect.dispatch_started", ["service"], ["effect-executor"], "dal.evidence.effect-dispatch/1.0", None, [*EFFECT_BASE_WRITES, "dispatch_marker"], "MATCHING_EXECUTOR_CLAIM"),
        ("EE-CONFIRM-NOT-EXECUTED", "dispatch_started", "confirmed_not_executed", "record_effect_not_executed", "external_effect.outcome_recorded", ["service"], ["counterparty-adapter"], "dal.evidence.authoritative-post-read/1.0", None, [*EFFECT_BASE_WRITES, "authoritative_post_read", "notification_outbox"], "MATCHING_SCOPE_KEY_TARGET_AND_NOT_EXECUTED_PROOF"),
        ("EE-CLAIM-RELEASE", "claimed", "intent_recorded", "release_expired_effect_claim", "external_effect.claim_released", ["service"], ["external-effect-controller"], "dal.evidence.executor-termination/1.0", None, [*EFFECT_BASE_WRITES, "executor_claim_release"], "EXECUTOR_TERMINATED_AND_NEVER_DISPATCHED"),
        ("EE-CLAIM-UNKNOWN", "claimed", "unknown", "record_effect_unknown", "external_effect.unknown", ["service"], ["external-effect-controller"], "dal.evidence.effect-unknown/1.0", None, [*EFFECT_BASE_WRITES, "decision_create", "decision_projection", "notification_outbox"], "EXECUTOR_TERMINATION_OR_DISPATCH_NOT_PROVABLE"),
        ("EE-DISPATCH-UNKNOWN", "dispatch_started", "unknown", "record_effect_unknown", "external_effect.unknown", ["service"], ["external-effect-controller"], "dal.evidence.effect-unknown/1.0", None, [*EFFECT_BASE_WRITES, "decision_create", "decision_projection", "notification_outbox"], "RESPONSE_LOST_OR_EXECUTOR_TERMINATED"),
        ("EE-RECONCILE-START", "unknown", "reconciling", "start_effect_reconciliation", "external_effect.reconciling", ["service"], ["external-effect-controller"], "dal.evidence.reconciliation-claim/1.0", None, [*EFFECT_BASE_WRITES, "reconciler_claim"], "SINGLE_RECONCILER_CLAIM"),
        ("EE-RECONCILE-COMPLETED", "reconciling", "confirmed_completed", "record_reconciled_completed", "external_effect.reconciled", ["service"], ["counterparty-adapter"], "dal.evidence.counterparty-receipt/1.0", None, [*EFFECT_BASE_WRITES, "authoritative_receipt", "notification_outbox"], "RECOVERY_EFFECT_MATCHING_SCOPE_KEY_TARGET_AND_RECEIPT"),
        ("EE-RECONCILE-NOT-EXECUTED", "reconciling", "confirmed_not_executed", "record_reconciled_not_executed", "external_effect.reconciled", ["service"], ["counterparty-adapter"], "dal.evidence.authoritative-post-read/1.0", None, [*EFFECT_BASE_WRITES, "authoritative_post_read", "notification_outbox"], "RECOVERY_EFFECT_MATCHING_SCOPE_KEY_TARGET_AND_NOT_EXECUTED_PROOF"),
        ("EE-RECONCILE-STILL-UNKNOWN", "reconciling", "unknown", "record_reconciliation_unknown", "external_effect.unknown", ["service"], ["counterparty-adapter"], "dal.evidence.effect-unknown/1.0", None, [*EFFECT_BASE_WRITES, "decision_create", "decision_projection", "notification_outbox"], "AUTHORITATIVE_RESULT_STILL_UNKNOWN"),
        ("EE-REARM", "confirmed_not_executed", "intent_recorded", "rearm_external_effect", "external_effect.rearmed", ["human"], ["registered-device", "external-effect-controller"], "dal.evidence.effect-rearm/1.0", "rearm_external_effect", ["decision_resolve", "approval_consume", "capability_issue", *EFFECT_BASE_WRITES, "notification_outbox"], "SAME_SCOPE_KEY_NEW_APPROVAL_CAPABILITY_EPOCH_AND_ATTEMPT_INCREMENT"),
    ]
    for row in effect_specs:
        minimum_gate = "G5" if row[0] == "EE-OBSERVED-DEPLOY-APPROVED" else "G1"
        add(spec(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], [], row[7], row[8], row[9], row[10],
            aggregate_type="external_effect", minimum_run_gate=minimum_gate,
        ))

    by_id = {row["spec_id"]: row for row in rows}
    by_id["EE-RECONCILE-COMPLETED"]["command_parameters"]["effect_outcome"] = "confirmed_completed"
    by_id["EE-RECONCILE-COMPLETED"]["command_parameters"]["owner_aggregate_type"] = "recovery_case"
    by_id["EE-RECONCILE-NOT-EXECUTED"]["command_parameters"]["owner_aggregate_type"] = "recovery_case"
    by_id["RC-EXECUTED"]["guard_id"] = "RECOVERY_EFFECT_DISPATCHED_AND_RECEIPT"
    observed_effect_writes = [*EFFECT_BASE_WRITES, "authoritative_post_read", "approval_action_receipt_ref", "notification_outbox"]
    unapproved_effect_writes = [*EFFECT_BASE_WRITES, "authoritative_post_read", "incident_decision", "decision_projection", "notification_outbox"]
    companion_map = {
        "SM-MERGE-INTENT": [companion("EE-CREATE-MERGE-INTENT", "external_effect", None, "intent_recorded", "external_effect.intent_recorded", ["dal.evidence.github-merge-intent/1.0"], EFFECT_BASE_WRITES, "external_effect")],
        "SM-DEPLOY-INTENT": [companion("EE-CREATE-DEPLOY-INTENT", "external_effect", None, "intent_recorded", "external_effect.intent_recorded", ["dal.evidence.deployment-intent/1.0"], EFFECT_BASE_WRITES, "external_effect")],
        "SM-MERGED-DISPATCHED": [companion("EE-CLOSE-MANAGED-MERGE", "external_effect", "dispatch_started", "confirmed_completed", "external_effect.outcome_recorded", ["dal.evidence.github-receipt/1.0"], [*EFFECT_BASE_WRITES, "authoritative_receipt"], "external_effect")],
        "SM-DEPLOYED": [companion("EE-CLOSE-MANAGED-DEPLOY", "external_effect", "dispatch_started", "confirmed_completed", "external_effect.outcome_recorded", ["dal.evidence.deployment-receipt/1.0"], [*EFFECT_BASE_WRITES, "authoritative_receipt"], "external_effect")],
        "SM-MERGED-OBSERVED": [companion("EE-CREATE-OBSERVED-MERGE-APPROVED", "external_effect", None, "confirmed_completed", "external_effect.observed_completed", ["dal.evidence.github-observed-merge/1.0"], observed_effect_writes, "external_effect")],
        "SM-MERGED-OBSERVED-UNAPPROVED": [companion("EE-CREATE-OBSERVED-MERGE-UNAPPROVED", "external_effect", None, "confirmed_completed", "external_effect.observed_completed", ["dal.evidence.github-observed-merge/1.0"], unapproved_effect_writes, "external_effect")],
        "SM-DEPLOYED-OBSERVED": [companion("EE-CREATE-OBSERVED-DEPLOY-APPROVED", "external_effect", None, "confirmed_completed", "external_effect.observed_completed", ["dal.evidence.observed-deployment/1.0"], observed_effect_writes, "external_effect")],
        "SM-DEPLOYED-OBSERVED-UNAPPROVED": [companion("EE-CREATE-OBSERVED-DEPLOY-UNAPPROVED", "external_effect", None, "confirmed_completed", "external_effect.observed_completed", ["dal.evidence.observed-deployment/1.0"], unapproved_effect_writes, "external_effect")],
        "RECOVERY-OPEN--POST-EFFECT": [companion("RC-CREATE-POST-EFFECT", "recovery_case", None, "investigating", "recovery_case.created", ["dal.evidence.recovery-proposal/1.0", "dal.evidence.post-effect-readback/1.0"], RECOVERY_BASE_WRITES, "recovery_case")],
        "RECONCILE-OPEN-RECOVERY": [companion("RC-CREATE-RECONCILIATION", "recovery_case", None, "investigating", "recovery_case.created", ["dal.evidence.recovery-proposal/1.0", "dal.evidence.effect-inventory/1.0"], RECOVERY_BASE_WRITES, "recovery_case")],
        "RC-START": [companion("EE-CREATE-RECOVERY-INTENT", "external_effect", None, "intent_recorded", "external_effect.intent_recorded", ["dal.evidence.recovery-capability/1.0"], EFFECT_BASE_WRITES, "external_effect")],
        "RC-EXECUTED": [companion("EE-CLOSE-RECOVERY-EXECUTED", "external_effect", "dispatch_started", "confirmed_completed", "external_effect.outcome_recorded", ["dal.evidence.recovery-execution/1.0"], [*EFFECT_BASE_WRITES, "authoritative_receipt"], "external_effect")],
    }
    for spec_id, companions in companion_map.items():
        for related in companions:
            if related["aggregate_type"] == "external_effect":
                related["owner_aggregate_type"] = by_id[spec_id]["aggregate_type"]
                related["owner_aggregate_id_source"] = "root.aggregate_id"
                if related["companion_id"].startswith("EE-CLOSE-"):
                    by_id[spec_id]["command_parameters"]["effect_outcome"] = related["to_state"]
        by_id[spec_id]["atomic_companion_transitions"] = companions
        by_id[spec_id]["atomic_write_set"] = list(dict.fromkeys(
            by_id[spec_id]["atomic_write_set"] + [write for item in companions for write in item["atomic_write_set"]]
        ))
    for row in rows:
        if row["spec_id"].startswith("RECONCILE-NOT-EXECUTED--"):
            row["atomic_companion_transitions"] = [companion(f"EE-CLOSE-{row['spec_id']}", "external_effect", "reconciling", "confirmed_not_executed", "external_effect.reconciled", ["dal.evidence.reconciliation/1.0"], [*EFFECT_BASE_WRITES, "authoritative_post_read"], "external_effect")]
        elif row["spec_id"].startswith("RECONCILE-COMPLETED-NONSTATE--") or row["spec_id"] in {"RECONCILE-MERGE", "RECONCILE-DEPLOY"}:
            row["atomic_companion_transitions"] = [companion(f"EE-CLOSE-{row['spec_id']}", "external_effect", "reconciling", "confirmed_completed", "external_effect.reconciled", [row["evidence_schema_version"]], [*EFFECT_BASE_WRITES, "authoritative_receipt"], "external_effect")]
        if row["atomic_companion_transitions"]:
            for related in row["atomic_companion_transitions"]:
                if related["aggregate_type"] == "external_effect":
                    related["owner_aggregate_type"] = row["aggregate_type"]
                    related["owner_aggregate_id_source"] = "root.aggregate_id"
                    if related["companion_id"].startswith("EE-CLOSE-"):
                        row["command_parameters"]["effect_outcome"] = related["to_state"]
            row["atomic_write_set"] = list(dict.fromkeys(
                row["atomic_write_set"] + [write for item in row["atomic_companion_transitions"] for write in item["atomic_write_set"]]
            ))

    rows.sort(key=lambda row: row["spec_id"])
    spec_ids = [row["spec_id"] for row in rows]
    if len(spec_ids) != len(set(spec_ids)):
        raise ValueError("duplicate TransitionSpec spec_id")
    required = {
        "schema_version", "spec_id", "aggregate_type", "from_state", "to_state", "command_type", "command_parameters", "event_type",
        "allowed_actor_types", "allowed_evidence_source_types",
        "actor_evidence_bindings",
        "allowed_reason_codes", "allowed_decision_reason_codes", "result_reason_owner",
        "result_reason_code", "evidence_schema_version", "required_evidence_schema_versions",
        "requires_decision_action", "guard_id", "atomic_write_set", "success_receipt_schema", "success_receipt_code",
        "minimum_run_gate", "atomic_companion_transitions",
    }
    for row in rows:
        if not required.issubset(row) or not row["allowed_actor_types"] or not row["allowed_evidence_source_types"]:
            raise ValueError(f"incomplete TransitionSpec: {row['spec_id']}")
        if len(row["allowed_reason_codes"]) > 1 or len(row["allowed_decision_reason_codes"]) > 1:
            raise ValueError(f"reason branch must be expanded to one exact spec: {row['spec_id']}")
        if row["requires_decision_action"] is not None and re.fullmatch(r"[a-z][a-z0-9_]*", row["requires_decision_action"]) is None:
            raise ValueError(f"non-canonical decision action: {row['spec_id']}")
        if len(row["atomic_write_set"]) != len(set(row["atomic_write_set"])):
            raise ValueError(f"duplicate member in atomic write set: {row['spec_id']}")
        for related in row["atomic_companion_transitions"]:
            required_related = {"companion_id", "aggregate_type", "from_state", "to_state", "event_type", "required_evidence_schema_versions", "atomic_write_set", "success_receipt_schema", "success_receipt_code", "snapshot_key"}
            if related["aggregate_type"] == "external_effect":
                required_related |= {"owner_aggregate_type", "owner_aggregate_id_source"}
            if set(related) != required_related or related["aggregate_type"] == row["aggregate_type"]:
                raise ValueError(f"invalid atomic companion transition: {row['spec_id']}")
            if related["aggregate_type"] == "external_effect" and (
                related["owner_aggregate_type"] != row["aggregate_type"]
                or related["owner_aggregate_id_source"] != "root.aggregate_id"
            ):
                raise ValueError(f"external-effect companion owner drift: {row['spec_id']}")
        if row["evidence_schema_version"] not in row["required_evidence_schema_versions"]:
            raise ValueError(f"primary evidence schema absent from required set: {row['spec_id']}")
        if row["minimum_run_gate"] not in {"G1", "G2", "G3", "G4", "G5"}:
            raise ValueError(f"invalid minimum run gate: {row['spec_id']}")
        if row["from_state"] in {"completed", "cancelled"}:
            raise ValueError(f"terminal state has outgoing transition: {row['spec_id']}")
        if row["from_state"] in {"reconciliation_required", "unknown", "reconciling"} and any(
            verb in row["command_type"] for verb in ("cancel", "retry")
        ):
            raise ValueError(f"unknown outcome has direct cancel/retry transition: {row['spec_id']}")
        binding_actors = {binding["actor_type"] for binding in row["actor_evidence_bindings"]}
        binding_sources = {
            source
            for binding in row["actor_evidence_bindings"]
            for source in binding["required_evidence_source_types"]
        }
        if binding_actors != set(row["allowed_actor_types"]) or binding_sources != set(row["allowed_evidence_source_types"]):
            raise ValueError(f"actor/evidence binding projection drift: {row['spec_id']}")
        if any(not binding["required_evidence_source_types"] for binding in row["actor_evidence_bindings"]):
            raise ValueError(f"empty actor/evidence binding: {row['spec_id']}")
        expected_receipt = {
            "feature": "dal.transition-receipt/1.0",
            "recovery_case": "dal.recovery-transition-receipt/1.0",
            "external_effect": "dal.external-effect-transition-receipt/1.0",
        }[row["aggregate_type"]]
        if row["success_receipt_schema"] != expected_receipt:
            raise ValueError(f"aggregate receipt mismatch: {row['spec_id']}")

    if "EE-CONFIRM-COMPLETED" in by_id:
        raise ValueError("completed external effect must be closed by its owner root command")
    expected_owner_roots = {
        "SM-MERGED-DISPATCHED": "EE-CLOSE-MANAGED-MERGE",
        "SM-DEPLOYED": "EE-CLOSE-MANAGED-DEPLOY",
        "RC-EXECUTED": "EE-CLOSE-RECOVERY-EXECUTED",
    }
    for root_spec_id, companion_id in expected_owner_roots.items():
        companions = by_id[root_spec_id]["atomic_companion_transitions"]
        if [item["companion_id"] for item in companions] != [companion_id] or by_id[root_spec_id]["command_parameters"]["effect_outcome"] != "confirmed_completed":
            raise ValueError(f"effect outcome owner drift: {root_spec_id}")
    for effect_spec_id in ("EE-RECONCILE-COMPLETED", "EE-RECONCILE-NOT-EXECUTED"):
        row = by_id[effect_spec_id]
        if row["command_parameters"].get("owner_aggregate_type") != "recovery_case" or not row["guard_id"].startswith("RECOVERY_EFFECT_"):
            raise ValueError(f"top-level reconciliation owner drift: {effect_spec_id}")
    dispatch_keys = [
        (
            row["aggregate_type"], row["from_state"], row["command_type"],
            tuple(row["allowed_reason_codes"]), row["requires_decision_action"],
            tuple(sorted(row["command_parameters"].items())),
        )
        for row in rows
    ]
    if len(dispatch_keys) != len(set(dispatch_keys)):
        duplicates = sorted({key for key in dispatch_keys if dispatch_keys.count(key) > 1})
        duplicate_ids = [row["spec_id"] for row, key in zip(rows, dispatch_keys) if key in duplicates]
        raise ValueError(f"duplicate TransitionSpec dispatch identity: {duplicate_ids}")
    by_id = {row["spec_id"]: row for row in rows}
    if by_id["SM-DEPLOYED-OBSERVED-UNAPPROVED"]["minimum_run_gate"] != "G1":
        raise ValueError("unapproved observed deploy fact ingestion must not depend on G5")
    if any(by_id[spec_id]["guard_id"] != "NO_OPEN_SAFETY_OR_POLICY_INCIDENT" for spec_id in ("SM-COMPLETE-MERGED", "SM-COMPLETE-DEPLOYED")):
        raise ValueError("completion transitions must reject open safety/policy incidents")
    registry = {"schema_version": "dal.transition-spec-registry/1.0", "specs": rows, "registry_sha256": None}
    registry_hash = write_hashed("transition-spec-registry_v1.0.json", registry, "registry_sha256")
    return rows, registry_hash


def build_test_contracts(specs: list[dict], registry_hash: str, evidence_hash: str, guard_hash: str) -> None:
    fixtures: dict[str, dict] = {}
    oracles: dict[str, dict] = {}
    rows: list[dict] = []
    operation_specs: dict[str, dict] = {}
    spec_by_id = {row["spec_id"]: row for row in specs}
    guard_rows = {
        row["guard_id"]: row
        for row in specs
        if row["guard_id"] is not None
    }

    def denied_value(value: object) -> object:
        if isinstance(value, bool):
            return not value
        if isinstance(value, int):
            return value + 1
        if value is None:
            return "invalid"
        return f"invalid:{value}"

    def denied_clause_value(clause: dict) -> object:
        if clause["operator"] == "in":
            return "not-in-closed-allowlist"
        if clause["operator"] == "greater_than":
            return clause["value"]
        if clause["operator"] == "at_least" and clause["value"] == "G5":
            return "G4"
        return denied_value(clause["value"])

    def satisfied_clause_value(clause: dict) -> object:
        if clause["operator"] == "in":
            return clause["value"][0]
        if clause["operator"] == "greater_than":
            return clause["value"] + 1
        return clause["value"]

    def outcome_fixture_values(item: dict) -> dict[str, object]:
        values: dict[str, object] = {
            "evidence.subject_aggregate_type": item["aggregate_type"],
            "root.aggregate_type": item["aggregate_type"],
            "evidence.subject_aggregate_id": "fixture-entity",
            "root.aggregate_id": "fixture-entity",
            "evidence.subject_aggregate_version": 7,
            "root.version": 7,
            "evidence.external_effect_id": "fixture-effect",
            "external_effect.effect_id": "fixture-effect",
            "evidence.external_effect_version": 11,
            "external_effect.version": 11,
            "evidence.effect_attempt": 2,
            "external_effect.attempt": 2,
            "evidence.effect_scope_key": "fixture-effect-scope",
            "external_effect.effect_scope_key": "fixture-effect-scope",
            "evidence.remote_idempotency_key": "fixture-remote-idempotency",
            "external_effect.remote_idempotency_key": "fixture-remote-idempotency",
            "evidence.effect_state": item["command_parameters"]["effect_outcome"],
            "command.effect_outcome": item["command_parameters"]["effect_outcome"],
            "evidence.target_fingerprint": "fixture-target",
            "external_effect.target_fingerprint": "fixture-target",
            "evidence.payload_sha256": "1" * 64,
            "protected_evidence.payload_sha256": "1" * 64,
            "evidence.protected_ref": "fixture-protected-ref",
            "protected_evidence.ref": "fixture-protected-ref",
            "evidence.authoritative_readback_sha256": "2" * 64,
            "protected_evidence.authoritative_readback_sha256": "2" * 64,
            "evidence.impact_sha256": "3" * 64,
            "protected_evidence.impact_sha256": "3" * 64,
            "evidence.semantic_binding_sha256": "6" * 64,
            "protected_evidence.semantic_binding_sha256": "6" * 64,
            "runtime.recomputed_evidence_semantic_binding_sha256": "6" * 64,
        }
        versions = set(item["required_evidence_schema_versions"])
        if "dal.evidence.github-receipt/1.0" in versions:
            values.update({
                "evidence.effect_action": "merge_pull_request",
                "external_effect.action": "merge_pull_request",
                "evidence.repository_id": "fixture-repository",
                "external_effect.repository_id": "fixture-repository",
                "evidence.pull_request_id": "fixture-pr-42",
                "external_effect.pull_request_id": "fixture-pr-42",
                "evidence.head_sha": "a" * 40,
                "external_effect.head_sha": "a" * 40,
                "evidence.merge_sha": "c" * 40,
                "protected_evidence.merge_sha": "c" * 40,
                "evidence.remote_receipt_id": "fixture-github-receipt",
                "protected_evidence.remote_receipt_id": "fixture-github-receipt",
            })
        elif "dal.evidence.deployment-receipt/1.0" in versions:
            values.update({
                "evidence.effect_action": "deploy_release",
                "external_effect.action": "deploy_release",
                "evidence.environment_id": "fixture-production",
                "external_effect.environment_id": "fixture-production",
                "evidence.deployment_target_id": "fixture-service",
                "external_effect.deployment_target_id": "fixture-service",
                "evidence.version_digest": "b" * 64,
                "external_effect.version_digest": "b" * 64,
                "evidence.remote_receipt_id": "fixture-deployment-receipt",
                "protected_evidence.remote_receipt_id": "fixture-deployment-receipt",
            })
        elif "dal.evidence.reconciliation/1.0" in versions:
            authoritative_receipt = (
                "fixture-reconciliation-receipt"
                if item["command_parameters"]["effect_outcome"] == "confirmed_completed"
                else None
            )
            values.update({
                "evidence.effect_action": "reconcile_external_effect",
                "external_effect.action": "reconcile_external_effect",
                "evidence.decision_action": item["command_type"],
                "command.decision_action": item["command_type"],
                "evidence.effect_result": item["command_parameters"]["effect_outcome"],
                "command.effect_outcome": item["command_parameters"]["effect_outcome"],
                "evidence.authoritative_receipt_id": authoritative_receipt,
                "protected_evidence.authoritative_receipt_id": authoritative_receipt,
                "evidence_set.registered_device.semantic_binding_sha256": "6" * 64,
                "evidence_set.external_effect_controller.semantic_binding_sha256": "6" * 64,
                "runtime.recomputed_registered_device_semantic_binding_sha256": "6" * 64,
                "runtime.recomputed_external_effect_controller_semantic_binding_sha256": "6" * 64,
            })
        return values

    def guard_fixture(item: dict, satisfied: bool) -> dict:
        guard_id = item["guard_id"]
        material: dict = {
            "schema_version": "dal.guard-fixture/1.0",
            "guard_id": guard_id,
            "guard_registry_sha256": guard_hash,
            "facts": [],
        }
        if guard_id is None:
            return material
        clauses = transition_guard_clauses(item)
        binding_values = outcome_fixture_values(item)
        facts: list[dict] = []
        for index, clause in enumerate(clauses):
            if clause["operator"] == "equals_field":
                if clause["field"] not in binding_values or clause["value"] not in binding_values:
                    raise ValueError(f"missing fixture binding value: {item['spec_id']}/{clause}")
                expected = binding_values[clause["value"]]
                actual = expected if satisfied or index > 0 else denied_value(expected)
                facts.extend([
                    {"field": clause["field"], "value": actual},
                    {"field": clause["value"], "value": expected},
                ])
            else:
                facts.append({
                    "field": clause["field"],
                    "value": satisfied_clause_value(clause) if satisfied or index > 0 else denied_clause_value(clause),
                })
        material["facts"] = facts
        return material

    def add(
        test_id: str,
        variant: str,
        gate: str,
        owners: list[str],
        pre_state: str | None,
        final_state: str | None,
        reason_owner: str | None,
        reason: str | None,
        code: str | None,
        effect: str | None,
        event_trace: list[str],
        entity_type: str = "feature",
        coverage_ref: str | None = None,
        allowed_writes: list[str] | None = None,
        receipt_schema_override: str | None = None,
        related_snapshots: list[dict] | None = None,
        transition_spec: dict | None = None,
        transition_case: str | None = None,
        transition_binding_index: int = 0,
        external_effect_trace: list[str] | None = None,
        operation_sequence: list[dict] | None = None,
        expected_receipts_override: list[dict] | None = None,
        expected_state_trace_override: list[str | None] | None = None,
        scenario_assertions: list[dict] | None = None,
        evidence_documents: list[dict] | None = None,
        evidence_validation_expected: str | None = None,
        guard_fact_overrides: dict[str, object] | None = None,
    ) -> None:
        key = f"{test_id}/{variant}/{gate}"
        injection = f"dal.inject/{key}/1.0"
        fixture_ref = f"dal.fixture/{key}/1.0"
        oracle_id = f"dal.oracle/{key}/1.0"
        fixture = {
            "schema_version": "dal.test-fixture/1.0",
            "test_id": test_id,
            "variant_id": variant,
            "run_gate": gate,
            "pre_state": {"entity_type": entity_type, "entity_id": "fixture-entity", "version": None if pre_state is None else 7, "state": pre_state},
            "injection_operation": {
                "schema_version": "dal.test-injection/1.0",
                "injection_point": injection,
                "operation_id": variant,
                "occurrence": 1,
            },
            "transition_registry_sha256": registry_hash if coverage_ref else None,
            "evidence_registry_sha256": evidence_hash,
            "guard_registry_sha256": guard_hash,
            "coverage_ref": coverage_ref,
        }
        if transition_spec is not None:
            binding = transition_spec["actor_evidence_bindings"][transition_binding_index]
            guard_preconditions = guard_fixture(transition_spec, transition_case != "guard_deny")
            if guard_fact_overrides:
                for fact in guard_preconditions["facts"]:
                    if fact["field"] in guard_fact_overrides:
                        fact["value"] = guard_fact_overrides[fact["field"]]
            fixture["transition_command"] = {
                "schema_version": "dal.test-transition-command/1.0",
                "aggregate_type": transition_spec["aggregate_type"],
                "aggregate_id": "fixture-entity",
                "expected_version": None if pre_state is None else 7,
                "command_type": transition_spec["command_type"],
                "command_parameters": transition_spec["command_parameters"],
                "actor_type": "unauthorized-actor" if transition_case == "actor_deny" else binding["actor_type"],
                "reason_code": transition_spec["allowed_reason_codes"][0] if transition_spec["allowed_reason_codes"] else None,
                "decision_action": transition_spec["requires_decision_action"],
                "evidence_source_types": ["unauthorized-source"] if transition_case == "evidence_source_deny" else binding["required_evidence_source_types"],
                "evidence_schema_versions": transition_spec["required_evidence_schema_versions"],
                "guard_preconditions": guard_preconditions,
            }
            if evidence_documents is not None:
                fixture["transition_command"]["evidence_documents"] = evidence_documents
                fixture["transition_command"]["evidence_validation_expected"] = evidence_validation_expected or "valid"
        else:
            operation_spec_id = f"OP-{test_id.removeprefix('DAL-T-')}"
            operation_actor, operation_source = OPERATION_BINDINGS.get(test_id, ("service", "immutable-fixture-catalog"))
            operation_command = {
                "schema_version": "dal.test-operation-command/1.0",
                "operation_spec_id": operation_spec_id,
                "operation_id": f"fixture-operation:{key}",
                "idempotency_key": f"fixture-idempotency:{key}",
                "actor_type": operation_actor,
                "evidence_source_type": operation_source,
                "input": {
                    "variant_id": variant,
                    "entity_id": "fixture-entity",
                    "expected_version": None if pre_state is None else 7,
                    "injection_point": injection,
                    "injection_occurrence": 1,
                },
            }
            fixture["operation_sequence"] = operation_sequence or [operation_command]
            operation_spec = operation_specs.setdefault(operation_spec_id, {
                "schema_version": "dal.operation-spec/1.0",
                "operation_spec_id": operation_spec_id,
                "command_type": OPERATION_COMMAND_TYPES.get(test_id, f"execute_{test_id.lower().replace('dal-t-', '').replace('-', '_')}_fixture"),
                "allowed_actor_types": [operation_actor],
                "allowed_evidence_source_types": [operation_source],
                "allowed_receipt_codes": [],
                "atomic_write_sets_by_variant": {},
                "variant_input_contracts": {},
                "success_receipt_schema": receipt_schema_override or "dal.operation-receipt/1.0",
            })
        receipt_schema = receipt_schema_override or (
            "dal.transition-receipt/1.0" if code is not None and entity_type == "feature"
            else "dal.recovery-transition-receipt/1.0" if code is not None and entity_type == "recovery_case"
            else "dal.external-effect-transition-receipt/1.0" if code is not None and entity_type == "external_effect"
            else None
        )
        denied_codes = {"POLICY_DENIED", "APPROVAL_INVALID", "DECISION_STALE", "CAPABILITY_STALE", "IDEMPOTENCY_CONFLICT", "VERSION_CONFLICT", "ILLEGAL_TRANSITION", "TERMINAL_STATE"}
        allowed = [] if code in denied_codes else (allowed_writes if allowed_writes is not None else (default_applied_write_set(entity_type, event_trace) if code == "APPLIED" else []))
        effective_effect_trace = (
            external_effect_trace
            if external_effect_trace is not None
            else [pre_state, final_state] if entity_type == "external_effect"
            else [] if effect is None
            else [effect]
        )
        receipts = expected_receipts_override if expected_receipts_override is not None else ([] if code is None else [{"schema_version": receipt_schema, "code": code, "count": 1}])
        state_trace = expected_state_trace_override or [pre_state, final_state]
        companion_transitions = transition_spec["atomic_companion_transitions"] if transition_spec is not None and transition_case == "allow" else []
        if companion_transitions:
            receipts = [*receipts, *[
                {"schema_version": related["success_receipt_schema"], "code": related["success_receipt_code"], "count": 1}
                for related in companion_transitions
            ]]
            related_snapshots = [*(related_snapshots or []), *[
                {"entity_type": related["aggregate_type"], "snapshot_key": related["snapshot_key"], "state": related["to_state"]}
                for related in companion_transitions
            ]]
            effective_effect_trace = [related["to_state"] for related in companion_transitions if related["aggregate_type"] == "external_effect"] or effective_effect_trace
            event_trace = [*event_trace, *[related["event_type"] for related in companion_transitions]]
        effect_summary = effective_effect_trace[-1] if effective_effect_trace else None
        oracle = {
            "schema_version": "dal.test-oracle/1.0",
            "test_id": test_id,
            "variant_id": variant,
            "run_gate": gate,
            "pre_state": fixture["pre_state"],
            "injection_operation": fixture["injection_operation"],
            "expected_state_trace": state_trace,
            "expected_event_trace": event_trace,
            "expected_receipts": receipts,
            "expected_external_effect_trace": effective_effect_trace,
            "expected_final_snapshot": {"entity_type": entity_type, "state": final_state, "reason_owner": reason_owner, "reason_code": reason},
            "expected_related_snapshots": related_snapshots or [],
            "allowed_write_set": allowed,
            "forbidden_side_effects": ["provider_call", "github_write", "worker_start", "production_access"] if gate == "G1" else ["unapproved_external_effect", "production_access"],
            "expected_atomic_companion_transitions": companion_transitions,
            "scenario_assertions": scenario_assertions or [],
            "coverage_ref": coverage_ref,
        }
        if transition_spec is None:
            operation_spec["allowed_receipt_codes"] = sorted(set(operation_spec["allowed_receipt_codes"] + [receipt["code"] for receipt in receipts]))
            operation_spec["atomic_write_sets_by_variant"][variant] = allowed
            operation_spec["variant_input_contracts"][variant] = fixture["operation_sequence"]
        fixtures[fixture_ref] = fixture
        oracles[oracle_id] = oracle
        rows.append({
            "test_id": test_id,
            "variant_id": variant,
            "injection_schema_version": "dal.test-injection/1.0",
            "injection_point": injection,
            "fixture_schema_version": "dal.test-fixture/1.0",
            "fixture_ref": fixture_ref,
            "fixture_sha256": digest(fixture),
            "oracle_schema_version": "dal.test-oracle/1.0",
            "oracle_id": oracle_id,
            "oracle_sha256": digest(oracle),
            "expected_entity_type": entity_type,
            "expected_entity_state": final_state,
            "expected_reason_owner": reason_owner,
            "expected_reason_code": reason,
            "expected_receipt_schema": receipts[0]["schema_version"] if receipts else None,
            "expected_receipt_code": receipts[0]["code"] if receipts else None,
            "expected_effect_state": effect_summary,
            "owner_tasks": owners,
            "run_gate": gate,
        })

    # Every expanded TransitionSpec gets one legal and illegal-source row for
    # every actor/evidence binding, plus one illegal-actor row. Guarded specs
    # additionally get an exact guard-deny row. Legal rows run no earlier than
    # the spec's frozen minimum gate.
    for item in specs:
        suffix = item["spec_id"].lower().replace("_", "-")
        test_id = (
            "DAL-T-RECOVERY-001" if item["aggregate_type"] == "recovery_case"
            else "DAL-T-EXTERNAL-EFFECT-001" if item["aggregate_type"] == "external_effect"
            else "DAL-T-SM-001"
        )
        related = [{"entity_type": "decision", "state": "open", "reason_owner": "decision", "reason_code": code} for code in item["allowed_decision_reason_codes"]]
        gate = item["minimum_run_gate"]
        owns_related_lifecycle = item["aggregate_type"] in {"recovery_case", "external_effect"} or bool(item["atomic_companion_transitions"])
        owners = ["DAL-009", "DAL-010", "DAL-011"] if owns_related_lifecycle else ["DAL-009", "DAL-010"]
        for binding_index, _binding in enumerate(item["actor_evidence_bindings"]):
            binding_suffix = "" if len(item["actor_evidence_bindings"]) == 1 else f"--binding-{binding_index + 1:02d}"
            add(test_id, f"expanded_spec_allow--{suffix}{binding_suffix}", gate, owners, item["from_state"], item["to_state"], item["result_reason_owner"], item["result_reason_code"], "APPLIED", None, [item["event_type"]], entity_type=item["aggregate_type"], coverage_ref=item["spec_id"], allowed_writes=item["atomic_write_set"], related_snapshots=related, transition_spec=item, transition_case="allow", transition_binding_index=binding_index)
            add(test_id, f"evidence_source_deny--{suffix}{binding_suffix}", gate, owners, item["from_state"], item["from_state"], None, None, "POLICY_DENIED", None, [], entity_type=item["aggregate_type"], coverage_ref=item["spec_id"], transition_spec=item, transition_case="evidence_source_deny", transition_binding_index=binding_index)
        add(test_id, f"actor_deny--{suffix}", gate, owners, item["from_state"], item["from_state"], None, None, "POLICY_DENIED", None, [], entity_type=item["aggregate_type"], coverage_ref=item["spec_id"], transition_spec=item, transition_case="actor_deny")
        if item["guard_id"] is not None:
            add(test_id, f"guard_deny--{suffix}", gate, owners, item["from_state"], item["from_state"], None, None, "POLICY_DENIED", None, [], entity_type=item["aggregate_type"], coverage_ref=item["spec_id"], transition_spec=item, transition_case="guard_deny")

    def external_outcome_evidence(
        item: dict,
        schema_version: str,
        *,
        source_type: str | None = None,
        overrides: dict[str, object] | None = None,
        omit: set[str] | None = None,
    ) -> dict:
        values = outcome_fixture_values(item)
        document: dict[str, object] = {
            "evidence_id": f"fixture-evidence:{schema_version}:{source_type or item['allowed_evidence_source_types'][0]}",
            "schema_version": schema_version,
            "source_type": source_type or item["allowed_evidence_source_types"][0],
            "subject_aggregate_type": values["evidence.subject_aggregate_type"],
            "subject_aggregate_id": values["evidence.subject_aggregate_id"],
            "subject_aggregate_version": values["evidence.subject_aggregate_version"],
            "observed_at": "2026-08-09T12:00:00Z",
            "payload_sha256": "1" * 64,
            "protected_ref": "fixture-protected-ref",
            "artifact_sha256": "4" * 64,
            "fact_version": 1,
            "effect_scope_key": "fixture-effect-scope",
            "remote_idempotency_key": "fixture-remote-idempotency",
            "effect_state": item["command_parameters"]["effect_outcome"],
            "semantic_binding_sha256": "6" * 64,
            "external_effect_id": values["evidence.external_effect_id"],
            "external_effect_version": values["evidence.external_effect_version"],
            "effect_attempt": values["evidence.effect_attempt"],
            "effect_action": values["evidence.effect_action"],
            "target_fingerprint": values["evidence.target_fingerprint"],
            "authoritative_readback_sha256": "2" * 64,
            "impact_sha256": "3" * 64,
        }
        if schema_version == "dal.evidence.github-receipt/1.0":
            document.update({
                "repository_id": values["evidence.repository_id"],
                "base_sha": "d" * 40,
                "artifact_digest": "5" * 64,
                "pull_request_id": values["evidence.pull_request_id"],
                "head_sha": values["evidence.head_sha"],
                "merge_sha": "c" * 40,
                "remote_receipt_id": "fixture-github-receipt",
            })
        elif schema_version == "dal.evidence.deployment-receipt/1.0":
            document.update({
                "environment_id": values["evidence.environment_id"],
                "deployment_target_id": values["evidence.deployment_target_id"],
                "version_digest": values["evidence.version_digest"],
                "remote_receipt_id": "fixture-deployment-receipt",
            })
        elif schema_version == "dal.evidence.reconciliation/1.0":
            document.update({
                "decision_action": values["evidence.decision_action"],
                "effect_result": values["evidence.effect_result"],
                "authoritative_receipt_id": values["evidence.authoritative_receipt_id"],
            })
        else:
            raise ValueError(f"unsupported external outcome evidence fixture: {schema_version}")
        document.update(overrides or {})
        for field in omit or set():
            document.pop(field, None)
        expected_fields = set(COMMON_EVIDENCE_FIELDS) | set(evidence_claim_fields(schema_version))
        if not omit and set(document) != expected_fields:
            raise ValueError(f"external outcome fixture field drift: {schema_version}")
        return document

    def add_evidence_binding_case(
        variant: str,
        item: dict,
        evidence_documents: list[dict],
        *,
        allowed: bool,
        failed_field: str | None = None,
        failed_value: object | None = None,
        validation_stage: str = "semantic_guard",
    ) -> None:
        pre_effect = "dispatch_started" if item["spec_id"] in {"SM-MERGED-DISPATCHED", "SM-DEPLOYED"} else "reconciling"
        post_effect = item["command_parameters"]["effect_outcome"] if allowed else pre_effect
        add(
            "DAL-T-EVIDENCE-BINDING-001", variant, item["minimum_run_gate"],
            ["DAL-009", "DAL-010", "DAL-011"], item["from_state"], item["to_state"] if allowed else item["from_state"],
            item["result_reason_owner"] if allowed else None, item["result_reason_code"] if allowed else None,
            "APPLIED" if allowed else "POLICY_DENIED", post_effect,
            [item["event_type"]] if allowed else [], coverage_ref=item["spec_id"],
            allowed_writes=item["atomic_write_set"] if allowed else [], transition_spec=item,
            transition_case="allow" if allowed else "evidence_semantic_deny",
            external_effect_trace=[pre_effect, post_effect], evidence_documents=evidence_documents,
            evidence_validation_expected="valid" if validation_stage != "schema" else "invalid",
            guard_fact_overrides={} if failed_field is None else {failed_field: failed_value},
            scenario_assertions=[
                {"field": "evidence_validation_stage", "operator": "equals", "value": validation_stage},
                {"field": "evidence_binding_valid", "operator": "equals", "value": allowed},
                {"field": "root_version_increment", "operator": "equals", "value": 1 if allowed else 0},
                {"field": "effect_version_increment", "operator": "equals", "value": 1 if allowed else 0},
            ],
        )

    merge_evidence_spec = spec_by_id["SM-MERGED-DISPATCHED"]
    merge_schema = "dal.evidence.github-receipt/1.0"
    merge_evidence = external_outcome_evidence(merge_evidence_spec, merge_schema)
    add_evidence_binding_case("valid_github_outcome", merge_evidence_spec, [merge_evidence], allowed=True, validation_stage="applied")
    for variant, evidence_field, wrong_value in (
        ("correct_shape_wrong_effect", "external_effect_id", "other-effect"),
        ("wrong_attempt", "effect_attempt", 3),
        ("wrong_repo", "repository_id", "other-repository"),
    ):
        document = external_outcome_evidence(merge_evidence_spec, merge_schema, overrides={evidence_field: wrong_value})
        add_evidence_binding_case(
            variant, merge_evidence_spec, [document], allowed=False,
            failed_field=f"evidence.{evidence_field}", failed_value=wrong_value,
        )

    deploy_evidence_spec = spec_by_id["SM-DEPLOYED"]
    deploy_schema = "dal.evidence.deployment-receipt/1.0"
    deploy_evidence = external_outcome_evidence(deploy_evidence_spec, deploy_schema)
    add_evidence_binding_case("valid_deployment_outcome", deploy_evidence_spec, [deploy_evidence], allowed=True, validation_stage="applied")
    wrong_env = external_outcome_evidence(deploy_evidence_spec, deploy_schema, overrides={"environment_id": "fixture-staging"})
    add_evidence_binding_case(
        "wrong_env", deploy_evidence_spec, [wrong_env], allowed=False,
        failed_field="evidence.environment_id", failed_value="fixture-staging",
    )
    missing_readback = external_outcome_evidence(deploy_evidence_spec, deploy_schema, omit={"authoritative_readback_sha256"})
    add_evidence_binding_case(
        "missing_authoritative_readback", deploy_evidence_spec, [missing_readback], allowed=False,
        validation_stage="schema",
    )

    reconciliation_spec = spec_by_id["RECONCILE-COMPLETED-NONSTATE--coding"]
    reconciliation_schema = "dal.evidence.reconciliation/1.0"
    reconciliation_evidence = [
        external_outcome_evidence(reconciliation_spec, reconciliation_schema, source_type=source)
        for source in reconciliation_spec["actor_evidence_bindings"][0]["required_evidence_source_types"]
    ]
    add_evidence_binding_case("valid_reconciliation_outcome", reconciliation_spec, reconciliation_evidence, allowed=True, validation_stage="applied")
    reconciliation_not_executed_spec = spec_by_id["RECONCILE-NOT-EXECUTED--coding"]
    reconciliation_not_executed_evidence = [
        external_outcome_evidence(reconciliation_not_executed_spec, reconciliation_schema, source_type=source)
        for source in reconciliation_not_executed_spec["actor_evidence_bindings"][0]["required_evidence_source_types"]
    ]
    add_evidence_binding_case(
        "valid_reconciliation_not_executed", reconciliation_not_executed_spec,
        reconciliation_not_executed_evidence, allowed=True, validation_stage="applied",
    )
    wrong_action_documents = [dict(document, decision_action="accept_deploy_result") for document in reconciliation_evidence]
    add_evidence_binding_case(
        "wrong_action", reconciliation_spec, wrong_action_documents, allowed=False,
        failed_field="evidence.decision_action", failed_value="accept_deploy_result",
    )

    def add_protected_result_negative(
        variant: str,
        item: dict,
        documents: list[dict],
        evidence_field: str,
        wrong_value: object,
    ) -> None:
        mutated = [dict(document, **{evidence_field: wrong_value, "semantic_binding_sha256": "7" * 64}) for document in documents]
        add_evidence_binding_case(
            variant, item, mutated, allowed=False,
            failed_field=f"evidence.{evidence_field}", failed_value=wrong_value,
            validation_stage="semantic_guard",
        )
        key = f"dal.fixture/DAL-T-EVIDENCE-BINDING-001/{variant}/{item['minimum_run_gate']}/1.0"
        facts = fixtures[key]["transition_command"]["guard_preconditions"]["facts"]
        for fact in facts:
            if fact["field"] in {"evidence.semantic_binding_sha256", "runtime.recomputed_evidence_semantic_binding_sha256"}:
                fact["value"] = "7" * 64
        # add() already recorded hashes; replace the just-added row hashes after
        # making this fixture model an attacker who recomputed its own digest.
        fixture_ref = next(row["fixture_ref"] for row in reversed(rows) if row["test_id"] == "DAL-T-EVIDENCE-BINDING-001" and row["variant_id"] == variant)
        manifest_row = next(row for row in reversed(rows) if row["fixture_ref"] == fixture_ref)
        manifest_row["fixture_sha256"] = digest(fixtures[fixture_ref])

    for variant, field, value in (
        ("wrong_github_merge_sha", "merge_sha", "e" * 40),
        ("wrong_github_remote_receipt", "remote_receipt_id", "other-github-receipt"),
        ("wrong_github_authoritative_readback", "authoritative_readback_sha256", "9" * 64),
        ("wrong_github_impact", "impact_sha256", "8" * 64),
    ):
        add_protected_result_negative(variant, merge_evidence_spec, [merge_evidence], field, value)

    for variant, field, value in (
        ("wrong_deployment_remote_receipt", "remote_receipt_id", "other-deployment-receipt"),
        ("wrong_deployment_authoritative_readback", "authoritative_readback_sha256", "9" * 64),
        ("wrong_deployment_impact", "impact_sha256", "8" * 64),
    ):
        add_protected_result_negative(variant, deploy_evidence_spec, [deploy_evidence], field, value)

    for variant, field, value in (
        ("wrong_reconciliation_authoritative_receipt", "authoritative_receipt_id", "other-reconciliation-receipt"),
        ("wrong_reconciliation_authoritative_readback", "authoritative_readback_sha256", "9" * 64),
        ("wrong_reconciliation_impact", "impact_sha256", "8" * 64),
    ):
        add_protected_result_negative(variant, reconciliation_spec, reconciliation_evidence, field, value)

    inconsistent_reconciliation = [dict(document) for document in reconciliation_evidence]
    inconsistent_reconciliation[1]["authoritative_readback_sha256"] = "9" * 64
    inconsistent_reconciliation[1]["semantic_binding_sha256"] = "7" * 64
    add_evidence_binding_case(
        "inconsistent_dual_source_reconciliation", reconciliation_spec, inconsistent_reconciliation,
        allowed=False,
        failed_field="evidence_set.external_effect_controller.semantic_binding_sha256",
        failed_value="7" * 64,
        validation_stage="cross_source_consistency",
    )
    inconsistent_key = "dal.fixture/DAL-T-EVIDENCE-BINDING-001/inconsistent_dual_source_reconciliation/G1/1.0"
    for fact in fixtures[inconsistent_key]["transition_command"]["guard_preconditions"]["facts"]:
        if fact["field"] == "runtime.recomputed_external_effect_controller_semantic_binding_sha256":
            fact["value"] = "7" * 64
    inconsistent_row = next(row for row in rows if row["fixture_ref"] == inconsistent_key)
    inconsistent_row["fixture_sha256"] = digest(fixtures[inconsistent_key])

    def add_completed_receipt_schema_negative(variant: str, invalid_receipt: object) -> None:
        documents = [
            dict(
                document,
                authoritative_receipt_id=invalid_receipt,
                payload_sha256="9" * 64,
                semantic_binding_sha256="7" * 64,
            )
            for document in reconciliation_evidence
        ]
        add_evidence_binding_case(
            variant, reconciliation_spec, documents, allowed=False,
            failed_field="evidence.authoritative_receipt_id", failed_value=invalid_receipt,
            validation_stage="schema",
        )
        key = f"dal.fixture/DAL-T-EVIDENCE-BINDING-001/{variant}/G1/1.0"
        fact_overrides = {
            "evidence.authoritative_receipt_id": invalid_receipt,
            "protected_evidence.authoritative_receipt_id": invalid_receipt,
            "evidence.payload_sha256": "9" * 64,
            "protected_evidence.payload_sha256": "9" * 64,
            "evidence.semantic_binding_sha256": "7" * 64,
            "protected_evidence.semantic_binding_sha256": "7" * 64,
            "runtime.recomputed_evidence_semantic_binding_sha256": "7" * 64,
            "evidence_set.registered_device.semantic_binding_sha256": "7" * 64,
            "evidence_set.external_effect_controller.semantic_binding_sha256": "7" * 64,
            "runtime.recomputed_registered_device_semantic_binding_sha256": "7" * 64,
            "runtime.recomputed_external_effect_controller_semantic_binding_sha256": "7" * 64,
        }
        for fact in fixtures[key]["transition_command"]["guard_preconditions"]["facts"]:
            if fact["field"] in fact_overrides:
                fact["value"] = fact_overrides[fact["field"]]
        row = next(row for row in rows if row["fixture_ref"] == key)
        row["fixture_sha256"] = digest(fixtures[key])

    add_completed_receipt_schema_negative("completed_null_authoritative_receipt", None)
    add_completed_receipt_schema_negative("completed_empty_authoritative_receipt", "")
    add_completed_receipt_schema_negative("completed_ascii_whitespace_authoritative_receipt", " \t ")
    add_completed_receipt_schema_negative("completed_unicode_whitespace_authoritative_receipt", "\u2003")

    def outcome_call(
        aggregate_type: str,
        aggregate_id: str,
        expected_version: int,
        command_type: str,
        command_parameters: dict,
        evidence_schema_versions: list[str],
        idempotency_suffix: str,
        actor_type: str,
        evidence_source_types: list[str],
        concurrency_group: str | None = None,
    ) -> dict:
        value = {
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "expected_version": expected_version,
            "command_type": command_type,
            "command_parameters": command_parameters,
            "actor_type": actor_type,
            "evidence_source_types": evidence_source_types,
            "evidence_schema_versions": evidence_schema_versions,
            "idempotency_key": f"fixture-idempotency:effect-ownership:{idempotency_suffix}",
        }
        if concurrency_group is not None:
            value["concurrency_group"] = concurrency_group
        return value

    def outcome_sequence(
        root_type: str,
        root_state: str,
        effect_state: str,
        effect_owner_type: str,
        commands: list[dict],
        sequence_key: str,
        fault_member: str | None = None,
    ) -> list[dict]:
        return [{
            "schema_version": "dal.test-operation-command/1.0",
            "operation_spec_id": "OP-EFFECT-OWNERSHIP-001",
            "operation_id": "fixture-operation:dispatch-effect-outcome-sequence",
            "idempotency_key": f"fixture-idempotency:dispatch-effect-outcome-sequence:{sequence_key}",
            "actor_type": "service",
            "evidence_source_type": "workflow-service",
            "input": {
                "root_snapshot": {"aggregate_type": root_type, "aggregate_id": "fixture-root", "version": 7, "state": root_state},
                "external_effect_snapshot": {
                    "aggregate_type": "external_effect", "aggregate_id": "fixture-effect", "version": 11,
                    "state": effect_state, "owner_aggregate_type": effect_owner_type,
                    "owner_aggregate_id": "fixture-root",
                },
                "commands": commands,
                "fault_injection": None if fault_member is None else {"atomic_write_member": fault_member, "occurrence": 1},
            },
        }]

    direct_completed = outcome_call(
        "external_effect", "fixture-effect", 11, "record_effect_completed",
        {"target_state": "confirmed_completed", "effect_outcome": None},
        ["dal.evidence.counterparty-receipt/1.0"], "direct-completed", "service", ["counterparty-adapter"],
    )
    add(
        "DAL-T-EFFECT-OWNERSHIP-001", "feature_direct_completed_command_removed", "G1", ["DAL-009", "DAL-010", "DAL-011"],
        "awaiting_merge", "awaiting_merge", None, None, "ILLEGAL_TRANSITION", "dispatch_started", [],
        operation_sequence=outcome_sequence("feature", "awaiting_merge", "dispatch_started", "feature", [direct_completed], "direct-completed"),
        expected_receipts_override=[{"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "ILLEGAL_TRANSITION", "count": 1}],
        external_effect_trace=["dispatch_started", "dispatch_started"], allowed_writes=[],
        scenario_assertions=[{"field": "root_version_increment", "operator": "equals", "value": 0}, {"field": "effect_version_increment", "operator": "equals", "value": 0}],
        receipt_schema_override="dal.operation-receipt/1.0",
    )
    direct_reconcile = outcome_call(
        "external_effect", "fixture-effect", 11, "record_reconciled_completed",
        spec_by_id["EE-RECONCILE-COMPLETED"]["command_parameters"],
        ["dal.evidence.counterparty-receipt/1.0"], "direct-reconcile", "service", ["counterparty-adapter"],
    )
    add(
        "DAL-T-EFFECT-OWNERSHIP-001", "feature_direct_reconciliation_owner_denied", "G1", ["DAL-009", "DAL-010", "DAL-011"],
        "reconciliation_required", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "POLICY_DENIED", "reconciling", [],
        operation_sequence=outcome_sequence("feature", "reconciliation_required", "reconciling", "feature", [direct_reconcile], "direct-reconcile"),
        expected_receipts_override=[{"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "POLICY_DENIED", "count": 1}],
        external_effect_trace=["reconciling", "reconciling"], allowed_writes=[],
        scenario_assertions=[{"field": "root_version_increment", "operator": "equals", "value": 0}, {"field": "effect_version_increment", "operator": "equals", "value": 0}],
        receipt_schema_override="dal.operation-receipt/1.0",
    )
    merge_root = outcome_call(
        "feature", "fixture-root", 7, "record_managed_merge",
        spec_by_id["SM-MERGED-DISPATCHED"]["command_parameters"],
        spec_by_id["SM-MERGED-DISPATCHED"]["required_evidence_schema_versions"], "merge-root", "service", ["github-control"],
    )
    add(
        "DAL-T-EFFECT-OWNERSHIP-001", "rejected_effect_first_then_owner_root", "G5", ["DAL-009", "DAL-010", "DAL-011"],
        "awaiting_merge", "merged", None, None, "APPLIED", "confirmed_completed", ["merge.completed", "external_effect.outcome_recorded"],
        operation_sequence=outcome_sequence("feature", "awaiting_merge", "dispatch_started", "feature", [direct_completed, merge_root], "effect-first-then-root"),
        expected_receipts_override=[
            {"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "ILLEGAL_TRANSITION", "count": 1},
            {"schema_version": "dal.transition-receipt/1.0", "code": "APPLIED", "count": 1},
            {"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "APPLIED", "count": 1},
        ],
        expected_state_trace_override=["awaiting_merge", "awaiting_merge", "merged"],
        external_effect_trace=["dispatch_started", "dispatch_started", "confirmed_completed"],
        related_snapshots=[{"entity_type": "external_effect", "snapshot_key": "external_effect", "state": "confirmed_completed"}],
        allowed_writes=spec_by_id["SM-MERGED-DISPATCHED"]["atomic_write_set"],
        scenario_assertions=[{"field": "root_version_increment", "operator": "equals", "value": 1}, {"field": "effect_version_increment", "operator": "equals", "value": 1}],
        receipt_schema_override="dal.operation-receipt/1.0",
    )
    add(
        "DAL-T-EFFECT-OWNERSHIP-001", "owner_root_then_removed_effect_command", "G5", ["DAL-009", "DAL-010", "DAL-011"],
        "awaiting_merge", "merged", None, None, "APPLIED", "confirmed_completed", ["merge.completed", "external_effect.outcome_recorded"],
        operation_sequence=outcome_sequence("feature", "awaiting_merge", "dispatch_started", "feature", [merge_root, direct_completed], "root-then-removed-effect"),
        expected_receipts_override=[
            {"schema_version": "dal.transition-receipt/1.0", "code": "APPLIED", "count": 1},
            {"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "APPLIED", "count": 1},
            {"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "ILLEGAL_TRANSITION", "count": 1},
        ],
        expected_state_trace_override=["awaiting_merge", "merged", "merged"],
        external_effect_trace=["dispatch_started", "confirmed_completed", "confirmed_completed"],
        related_snapshots=[{"entity_type": "external_effect", "snapshot_key": "external_effect", "state": "confirmed_completed"}],
        allowed_writes=spec_by_id["SM-MERGED-DISPATCHED"]["atomic_write_set"],
        scenario_assertions=[{"field": "root_version_increment", "operator": "equals", "value": 1}, {"field": "effect_version_increment", "operator": "equals", "value": 1}, {"field": "successful_root_command_count", "operator": "equals", "value": 1}],
        receipt_schema_override="dal.operation-receipt/1.0",
    )
    merge_root_a = outcome_call(
        "feature", "fixture-root", 7, "record_managed_merge",
        spec_by_id["SM-MERGED-DISPATCHED"]["command_parameters"],
        spec_by_id["SM-MERGED-DISPATCHED"]["required_evidence_schema_versions"], "merge-concurrent-a", "service", ["github-control"], "effect-owner-cas",
    )
    merge_root_b = outcome_call(
        "feature", "fixture-root", 7, "record_managed_merge",
        spec_by_id["SM-MERGED-DISPATCHED"]["command_parameters"],
        spec_by_id["SM-MERGED-DISPATCHED"]["required_evidence_schema_versions"], "merge-concurrent-b", "service", ["github-control"], "effect-owner-cas",
    )
    add(
        "DAL-T-EFFECT-OWNERSHIP-001", "concurrent_owner_roots_single_winner", "G5", ["DAL-009", "DAL-010", "DAL-011"],
        "awaiting_merge", "merged", None, None, "APPLIED", "confirmed_completed", ["merge.completed", "external_effect.outcome_recorded"],
        operation_sequence=outcome_sequence("feature", "awaiting_merge", "dispatch_started", "feature", [merge_root_a, merge_root_b], "concurrent-roots"),
        expected_receipts_override=[
            {"schema_version": "dal.transition-receipt/1.0", "code": "APPLIED", "count": 1},
            {"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "APPLIED", "count": 1},
            {"schema_version": "dal.transition-receipt/1.0", "code": "VERSION_CONFLICT", "count": 1},
        ],
        external_effect_trace=["dispatch_started", "confirmed_completed"],
        related_snapshots=[{"entity_type": "external_effect", "snapshot_key": "external_effect", "state": "confirmed_completed"}],
        allowed_writes=spec_by_id["SM-MERGED-DISPATCHED"]["atomic_write_set"],
        scenario_assertions=[{"field": "root_version_increment", "operator": "equals", "value": 1}, {"field": "effect_version_increment", "operator": "equals", "value": 1}, {"field": "successful_root_command_count", "operator": "equals", "value": 1}],
        receipt_schema_override="dal.operation-receipt/1.0",
    )
    merge_companion_writes = spec_by_id["SM-MERGED-DISPATCHED"]["atomic_companion_transitions"][0]["atomic_write_set"]
    for failed_member in merge_companion_writes:
        add(
            "DAL-T-EFFECT-OWNERSHIP-001", f"companion_write_failure_rolls_back_root--{failed_member.replace('_', '-')}", "G5", ["DAL-009", "DAL-010", "DAL-011"],
            "awaiting_merge", "awaiting_merge", None, None, None, "dispatch_started", [],
            operation_sequence=outcome_sequence("feature", "awaiting_merge", "dispatch_started", "feature", [merge_root], f"companion-failure-{failed_member}", failed_member),
            expected_receipts_override=[], external_effect_trace=["dispatch_started", "dispatch_started"], allowed_writes=[],
            scenario_assertions=[{"field": "root_version_increment", "operator": "equals", "value": 0}, {"field": "effect_version_increment", "operator": "equals", "value": 0}, {"field": "business_event_count", "operator": "equals", "value": 0}, {"field": "failed_atomic_write_member", "operator": "equals", "value": failed_member}],
            receipt_schema_override="dal.operation-receipt/1.0",
        )
    recovery_root = outcome_call(
        "recovery_case", "fixture-root", 7, "record_recovery_execution",
        spec_by_id["RC-EXECUTED"]["command_parameters"],
        spec_by_id["RC-EXECUTED"]["required_evidence_schema_versions"], "recovery-root", "service", ["recovery-executor"],
    )
    add(
        "DAL-T-EFFECT-OWNERSHIP-001", "recovery_owner_root_closes_effect", "G1", ["DAL-009", "DAL-010", "DAL-011"],
        "executing", "verifying", None, None, "APPLIED", "confirmed_completed", ["recovery_case.verifying", "external_effect.outcome_recorded"],
        entity_type="recovery_case",
        operation_sequence=outcome_sequence("recovery_case", "executing", "dispatch_started", "recovery_case", [recovery_root], "recovery-root"),
        expected_receipts_override=[
            {"schema_version": "dal.recovery-transition-receipt/1.0", "code": "APPLIED", "count": 1},
            {"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "APPLIED", "count": 1},
        ],
        external_effect_trace=["dispatch_started", "confirmed_completed"],
        related_snapshots=[{"entity_type": "external_effect", "snapshot_key": "external_effect", "state": "confirmed_completed"}],
        allowed_writes=spec_by_id["RC-EXECUTED"]["atomic_write_set"],
        scenario_assertions=[{"field": "root_version_increment", "operator": "equals", "value": 1}, {"field": "effect_version_increment", "operator": "equals", "value": 1}],
        receipt_schema_override="dal.operation-receipt/1.0",
    )
    reconcile_root_spec = spec_by_id["RECONCILE-NOT-EXECUTED--coding"]
    reconcile_root = outcome_call(
        "feature", "fixture-root", 7, "resume_checkpoint",
        reconcile_root_spec["command_parameters"], reconcile_root_spec["required_evidence_schema_versions"], "feature-reconcile-root",
        reconcile_root_spec["actor_evidence_bindings"][0]["actor_type"], reconcile_root_spec["actor_evidence_bindings"][0]["required_evidence_source_types"],
    )
    add(
        "DAL-T-EFFECT-OWNERSHIP-001", "feature_reconciliation_root_closes_effect", "G1", ["DAL-009", "DAL-010", "DAL-011"],
        "reconciliation_required", "coding", None, None, "APPLIED", "confirmed_not_executed", ["feature.resumed", "external_effect.reconciled"],
        operation_sequence=outcome_sequence("feature", "reconciliation_required", "reconciling", "feature", [reconcile_root], "feature-reconcile-root"),
        expected_receipts_override=[
            {"schema_version": "dal.transition-receipt/1.0", "code": "APPLIED", "count": 1},
            {"schema_version": "dal.external-effect-transition-receipt/1.0", "code": "APPLIED", "count": 1},
        ],
        external_effect_trace=["reconciling", "confirmed_not_executed"],
        related_snapshots=[{"entity_type": "external_effect", "snapshot_key": "external_effect", "state": "confirmed_not_executed"}],
        allowed_writes=reconcile_root_spec["atomic_write_set"],
        scenario_assertions=[{"field": "root_version_increment", "operator": "equals", "value": 1}, {"field": "effect_version_increment", "operator": "equals", "value": 1}],
        receipt_schema_override="dal.operation-receipt/1.0",
    )

    def many(test: str, variants: list[str], gate: str, owners: list[str], pre: str | None, final: str | None, reason_owner: str | None, reason: str | None, code: str | None, effect: str | None, events: list[str], entity: str = "feature", allowed_writes: list[str] | None = None, receipt_schema_override: str | None = None) -> None:
        for variant in variants:
            add(test, variant, gate, owners, pre, final, reason_owner, reason, code, effect, events, entity, allowed_writes=allowed_writes, receipt_schema_override=receipt_schema_override)

    add("DAL-T-SM-001", "illegal_edge", "G1", ["DAL-009", "DAL-010"], "intake", "intake", None, None, "ILLEGAL_TRANSITION", None, [])
    add("DAL-T-SM-001", "event_mismatch", "G1", ["DAL-009", "DAL-010"], "planning", "planning", None, None, "ILLEGAL_TRANSITION", None, [])
    replay_command = {
        "schema_version": "dal.test-operation-command/1.0", "operation_spec_id": "OP-CMD-IDEMPOTENCY-001",
        "operation_id": "fixture-operation:record-plan", "idempotency_key": "fixture-idempotency:record-plan",
        "actor_type": "service", "evidence_source_type": "planner",
        "input": {"variant_id": "idempotent_replay", "entity_id": "fixture-entity", "expected_version": 7, "canonical_payload_sha256": "1" * 64},
    }
    add(
        "DAL-T-CMD-IDEMPOTENCY-001", "idempotent_replay", "G1", ["DAL-009", "DAL-010"],
        "planning", "awaiting_plan_review", None, None, "APPLIED", None, ["plan.ready"],
        allowed_writes=list(WRITE_SETS["D"]), operation_sequence=[replay_command, dict(replay_command)],
        expected_receipts_override=[{"schema_version": "dal.transition-receipt/1.0", "code": "APPLIED", "count": 2, "unique_receipt_ids": 1, "duplicate_flags": [False, True]}],
        expected_state_trace_override=["planning", "awaiting_plan_review", "awaiting_plan_review"],
        scenario_assertions=[{"field": "business_event_count", "operator": "equals", "value": 1}, {"field": "aggregate_version_increment", "operator": "equals", "value": 1}],
    )
    add("DAL-T-CMD-IDEMPOTENCY-001", "idempotency_conflict", "G1", ["DAL-009", "DAL-010"], "awaiting_plan_review", "awaiting_plan_review", None, None, "IDEMPOTENCY_CONFLICT", None, [])
    add("DAL-T-EVENT-ORDER-001", "out_of_order_event", "G1", ["DAL-009", "DAL-010"], "approved", "approved", None, None, "VERSION_CONFLICT", None, [])
    add("DAL-T-SM-001", "terminal_completed", "G1", ["DAL-009"], "completed", "completed", None, None, "TERMINAL_STATE", None, [])
    add("DAL-T-SM-001", "terminal_cancelled", "G1", ["DAL-009"], "cancelled", "cancelled", None, None, "TERMINAL_STATE", None, [])
    many("DAL-T-CONFIG-ISOLATION-001", ["finance_import", "production_credential", "unknown_config", "insecure_secret_file"], "G1", ["DAL-007"], "not_loaded", "not_loaded", None, None, "POLICY_DENIED", None, [], "service_config", allowed_writes=[], receipt_schema_override="dal.operation-receipt/1.0")
    add("DAL-T-DB-CONTRACT-001", "migration_up", "G1", ["DAL-008"], "empty", "migrated", None, None, "APPLIED", None, ["database.migrated"], "database", allowed_writes=["schema", "migration_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    add("DAL-T-DB-CONTRACT-001", "idempotency_unique", "G1", ["DAL-008"], "migrated", "migrated", None, None, "IDEMPOTENCY_CONFLICT", None, [], "database", allowed_writes=[], receipt_schema_override="dal.operation-receipt/1.0")
    add("DAL-T-DB-CONTRACT-001", "cas_conflict", "G1", ["DAL-008"], "migrated", "migrated", None, None, "VERSION_CONFLICT", None, [], "database", allowed_writes=[], receipt_schema_override="dal.operation-receipt/1.0")
    add("DAL-T-DB-CONTRACT-001", "sensitive_field_encryption", "G1", ["DAL-008"], "migrated", "migrated", None, None, "APPLIED", None, ["database.encrypted_roundtrip"], "database", allowed_writes=["encrypted_record", "operation_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    add("DAL-T-DB-CONTRACT-001", "retention_delete", "G1", ["DAL-008"], "migrated", "migrated", None, None, "APPLIED", None, ["database.retention_applied"], "database", allowed_writes=["retention_tombstone", "operation_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    add("DAL-T-SM-001", "paused_resume_base_drift", "G1", ["DAL-009", "DAL-011"], "paused", "needs_human", "feature", "STATE_DRIFT", "APPLIED", None, ["feature.blocked"])
    many("DAL-T-TX-001", ["event_fail", "audit_fail", "outbox_fail", "receipt_fail"], "G1", ["DAL-010"], "planning", "planning", None, None, None, None, [])
    add("DAL-T-APP-001", "stale_decision", "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "DECISION_STALE", None, [])
    many("DAL-T-APP-001", ["double_tap", "revoke_race"], "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "APPROVAL_INVALID", None, [])
    concurrent_command = {
        "schema_version": "dal.test-operation-command/1.0", "operation_spec_id": "OP-APP-001",
        "operation_id": "fixture-operation:concurrent-approval", "idempotency_key": "fixture-idempotency:approval-a",
        "actor_type": "human", "evidence_source_type": "registered-device",
        "input": {"variant_id": "concurrent_consume", "entity_id": "fixture-entity", "expected_version": 7, "approval_id": "fixture-approval"},
    }
    concurrent_command_b = json.loads(json.dumps(concurrent_command))
    concurrent_command_b["idempotency_key"] = "fixture-idempotency:approval-b"
    add(
        "DAL-T-APP-001", "concurrent_consume", "G1", ["DAL-011", "DAL-013"],
        "awaiting_plan_review", "approved", None, None, "APPLIED", None, ["plan.approved"],
        allowed_writes=list(WRITE_SETS["P"]), operation_sequence=[
            {"concurrency_group": "approval-cas-1", **concurrent_command},
            {"concurrency_group": "approval-cas-1", **concurrent_command_b},
        ],
        expected_receipts_override=[
            {"schema_version": "dal.transition-receipt/1.0", "code": "APPLIED", "count": 1},
            {"schema_version": "dal.transition-receipt/1.0", "code": "APPROVAL_INVALID", "count": 1},
        ],
        scenario_assertions=[{"field": "aggregate_version_increment", "operator": "equals", "value": 1}, {"field": "approval_consume_count", "operator": "equals", "value": 1}],
    )
    add("DAL-T-APP-EXP-001", "approval_expired", "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "APPROVAL_INVALID", None, [])
    add("DAL-T-APP-EXP-001", "decision_expired", "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "DECISION_STALE", None, [])
    many("DAL-T-STATEHASH-001", ["field", "order", "null", "version", "forged_digest", "effect_inventory_order", "effect_inventory_membership"], "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "DECISION_STALE", None, [])
    many("DAL-T-ARTIFACTHASH-001", ["body", "metadata", "canonicalizer", "field_boundary", "forged_digest"], "G1", ["DAL-011"], "awaiting_plan_review", "awaiting_plan_review", None, None, "APPROVAL_INVALID", None, [])
    for variant in ("synthetic_kill", "synthetic_disconnect", "synthetic_ack_loss"):
        add("DAL-T-REC-001", variant, "G1", ["DAL-009", "DAL-010"], "coding", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "APPLIED", "unknown", ["external_effect.intent_recorded", "external_effect.claimed", "external_effect.dispatch_started", "external_effect.unknown", "reconciliation.required"], external_effect_trace=["intent_recorded", "claimed", "dispatch_started", "unknown"])
    many("DAL-T-REC-001", ["unknown_merge_cancel", "unknown_deploy_cancel"], "G1", ["DAL-009", "DAL-010"], "reconciliation_required", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "POLICY_DENIED", "unknown", [])
    for variant in ("worker_kill", "worker_disconnect"):
        add("DAL-T-REC-001", variant, "G2", ["DAL-016", "DAL-017"], "coding", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "APPLIED", "unknown", ["external_effect.intent_recorded", "external_effect.claimed", "external_effect.dispatch_started", "external_effect.unknown", "reconciliation.required"], external_effect_trace=["intent_recorded", "claimed", "dispatch_started", "unknown"])
    recovery = {
        "investigation_fail": ("investigating", "blocked", "recovery_case.blocked", "APPLIED"),
        "cancel_investigating": ("investigating", "cancelled", "recovery_case.cancelled", "APPLIED"),
        "cancel_approved": ("approved", "cancelled", "recovery_case.cancelled", "APPLIED"),
        "start_revoked": ("approved", "blocked", "recovery_case.blocked", "APPLIED"),
        "execution_blocked": ("executing", "blocked", "recovery_case.blocked", "APPLIED"),
        "verification_blocked": ("verifying", "blocked", "recovery_case.blocked", "APPLIED"),
        "reinvestigate": ("blocked", "investigating", "recovery_case.reinvestigating", "APPLIED"),
        "replace_proposal": ("blocked", "awaiting_decision", "recovery_case.awaiting_decision", "APPLIED"),
    }
    for variant, (pre, final, event, code) in recovery.items():
        add("DAL-T-RECOVERY-001", variant, "G1", ["DAL-009", "DAL-010", "DAL-011"], pre, final, None, None, code, None, [event], "recovery_case")
    retry_limit_command = {
        "schema_version": "dal.test-operation-command/1.0", "operation_spec_id": "OP-RESTART-001",
        "operation_id": "fixture-operation:restart-retry-limit", "idempotency_key": "fixture-idempotency:restart-retry-limit",
        "actor_type": "service", "evidence_source_type": "workflow-service",
        "input": {"variant_id": "service_retry_limit", "entity_id": "fixture-entity", "expected_version": 7, "initial_attempt_count": 1, "transient_retry_count": 2, "provider_attempt_count": 3, "max_transient_retries": 2, "restart_count": 1},
    }
    add(
        "DAL-T-RESTART-001", "service_retry_limit", "G1", ["DAL-009"], "coding", "needs_human",
        "feature", "TRANSIENT_RETRY_EXHAUSTED", "APPLIED", None, ["feature.blocked"],
        operation_sequence=[retry_limit_command],
        scenario_assertions=[{"field": "provider_call_count_after_restart", "operator": "equals", "value": 0}, {"field": "provider_attempt_count", "operator": "equals", "value": 3}, {"field": "transient_retry_count", "operator": "equals", "value": 2}],
    )
    add("DAL-T-RESTART-001", "service_review_limit", "G1", ["DAL-009"], "fixing", "needs_human", "feature", "REVIEW_LOOP_LIMIT", "APPLIED", None, ["feature.blocked"])
    add("DAL-T-RESTART-001", "mac_retry_limit", "G2", ["DAL-016"], "coding", "needs_human", "feature", "TRANSIENT_RETRY_EXHAUSTED", "APPLIED", None, ["feature.blocked"])
    add("DAL-T-RESTART-001", "mac_review_limit", "G2", ["DAL-016"], "fixing", "needs_human", "feature", "REVIEW_LOOP_LIMIT", "APPLIED", None, ["feature.blocked"])
    add("DAL-T-INJECTION-001", "api_intake", "G1", ["DAL-012"], "intake", "intake", None, None, "POLICY_DENIED", None, [])
    many("DAL-T-INJECTION-001", ["issue", "readme", "diff", "test_failure"], "G2", ["DAL-015", "DAL-018", "DAL-019"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    add("DAL-T-INJECTION-001", "provider_output", "G4", ["DAL-022", "DAL-025"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    many("DAL-T-PATH-001", ["dotdot", "absolute", "symlink_swap", "nested_repo"], "G2", ["DAL-017", "DAL-018"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    many("DAL-T-ENDPOINT-001", ["scheme", "host", "path", "redirect", "proxy"], "G4", ["DAL-025", "DAL-026"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    many("DAL-T-SECRET-OUTPUT-001", ["env", "stdout", "stderr", "patch", "artifact", "synthetic_exception"], "G2", ["DAL-017", "DAL-028"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    add("DAL-T-SECRET-OUTPUT-001", "provider_exception", "G4", ["DAL-025"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    many("DAL-T-GH-EVENT-001", ["fork", "unknown_repo", "unknown_sender", "edited_event", "replay_delivery"], "G2", ["DAL-014", "DAL-015"], "intake", "intake", None, None, "POLICY_DENIED", None, [])
    many("DAL-T-EPOCH-001", ["old_lease", "old_capability", "lease_capability", "all_old"], "G2", ["DAL-011", "DAL-016", "DAL-017"], "coding", "coding", None, None, "CAPABILITY_STALE", None, [])
    many("DAL-T-EPOCH-001", ["old_approval", "capability_approval"], "G2", ["DAL-011", "DAL-016", "DAL-017"], "approved", "approved", None, None, "APPROVAL_INVALID", None, [])
    add("DAL-T-LEASE-001", "pause_expire", "G2", ["DAL-016", "DAL-017"], "paused", "paused", "feature", "USER_PAUSE", "CAPABILITY_STALE", None, [])
    add("DAL-T-LEASE-001", "old_worker_result", "G2", ["DAL-016", "DAL-017"], "paused", "paused", "feature", "USER_PAUSE", "CAPABILITY_STALE", None, [])
    add("DAL-T-LEASE-001", "new_lease_after_drift", "G2", ["DAL-016", "DAL-017"], "paused", "needs_human", "feature", "STATE_DRIFT", "APPLIED", None, ["feature.blocked"])
    many("DAL-T-GIT-BASE-001", ["base_drift", "pr_head_drift", "content_conflict", "index_conflict"], "G2", ["DAL-015", "DAL-018"], "coding", "needs_human", "feature", "GIT_CONFLICT", "APPLIED", None, ["feature.blocked"])
    for variant in ("push_ack_loss", "pr_ack_loss"):
        add("DAL-T-GIT-ACK-001", variant, "G3", ["DAL-015", "DAL-034"], "awaiting_merge", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "APPLIED", "unknown", ["external_effect.intent_recorded", "external_effect.claimed", "external_effect.dispatch_started", "external_effect.unknown", "reconciliation.required"], external_effect_trace=["intent_recorded", "claimed", "dispatch_started", "unknown"])
    for variant in ("push_ack_reconciled", "pr_ack_reconciled"):
        add("DAL-T-GIT-ACK-001", variant, "G3", ["DAL-015", "DAL-034"], "reconciliation_required", "awaiting_merge", None, None, "APPLIED", "confirmed_completed", ["external_effect.reconciling", "external_effect.reconciled", "feature.resumed"], external_effect_trace=["unknown", "reconciling", "confirmed_completed"])
    many("DAL-T-CHECK-001", ["repo", "sha", "name", "external_id", "receipt"], "G3", ["DAL-015", "DAL-033"], "awaiting_merge", "awaiting_merge", None, None, "POLICY_DENIED", None, [])
    add("DAL-T-CHECK-001", "duplicate", "G3", ["DAL-015", "DAL-033"], "awaiting_merge", "awaiting_merge", None, None, "IDEMPOTENT_REPLAY", "confirmed_completed", [], allowed_writes=[], receipt_schema_override="dal.operation-receipt/1.0")
    many("DAL-T-KILL-001", ["commit_race", "push_race", "check_race"], "G3", ["DAL-016", "DAL-031", "DAL-032", "DAL-033"], "paused", "paused", "feature", "POLICY_FAILURE", "CAPABILITY_STALE", None, [])
    many("DAL-T-REVIEW-INDEP-001", ["same_session", "same_context", "same_independence_key"], "G3", ["DAL-023", "DAL-024", "DAL-030"], "reviewing", "reviewing", None, None, "POLICY_DENIED", None, [])
    many("DAL-T-REVIEW-INDEP-001", ["synthetic_fresh"], "G3", ["DAL-023", "DAL-024", "DAL-030"], "reviewing", "verified", None, None, "APPLIED", None, ["review.completed"])
    many("DAL-T-REVIEW-INDEP-001", ["live_fresh"], "G4", ["DAL-023", "DAL-024", "DAL-030"], "reviewing", "verified", None, None, "APPLIED", None, ["review.completed"])
    pronly = {
        "text_request": ("awaiting_merge", "POLICY_DENIED", None, []),
        "approve_record": ("awaiting_merge", "APPLIED", None, ["approval.recorded"]),
        "github_call_attempt": ("awaiting_merge", "POLICY_DENIED", None, []),
        "manual_observation": ("merged", "APPLIED", "confirmed_completed", ["merge.completed"]),
        "unapproved_observation": ("merged", "APPLIED", "confirmed_completed", ["merge.completed", "decision.created"]),
        "unknown_observation": ("reconciliation_required", "APPLIED", "unknown", ["reconciliation.required"]),
    }
    for variant, (final, code, effect, events) in pronly.items():
        observed = variant in {"manual_observation", "unapproved_observation"}
        add(
            "DAL-T-PRONLY-MERGE-001", variant, "G3", ["DAL-015", "DAL-032", "DAL-033"], "awaiting_merge", final,
            "decision" if variant == "unapproved_observation" else ("feature" if variant == "unknown_observation" else None),
            "POLICY_FAILURE" if variant == "unapproved_observation" else ("EXTERNAL_RESULT_UNKNOWN" if variant == "unknown_observation" else None),
            code, effect, (["external_effect.observed_completed", *events] if observed else events),
            allowed_writes=(list(dict.fromkeys(WRITE_SETS["U"] + EFFECT_BASE_WRITES)) if variant == "unapproved_observation" else list(dict.fromkeys(WRITE_SETS["O"] + EFFECT_BASE_WRITES)) if variant == "manual_observation" else None),
            external_effect_trace=(["confirmed_completed"] if observed else None),
            related_snapshots=([{"entity_type": "external_effect", "snapshot_key": "external_effect", "state": "confirmed_completed"}] if observed else None),
        )
    add(
        "DAL-T-DELIVERY-OBS-001", "deploy_approve_record", "G5", ["DAL-045", "DAL-048", "DAL-050"],
        "merged", "merged", None, None, "APPLIED", None, ["approval.recorded"],
        allowed_writes=list(WRITE_SETS["J"]),
    )
    add(
        "DAL-T-DELIVERY-OBS-001", "manual_deploy_observation", "G5", ["DAL-045", "DAL-048", "DAL-050"],
        "merged", "deployed", None, None, "APPLIED", "confirmed_completed",
        ["external_effect.observed_completed", "deployment.completed"],
        allowed_writes=list(WRITE_SETS["O"]), external_effect_trace=["confirmed_completed"],
    )
    add(
        "DAL-T-DELIVERY-OBS-001", "unapproved_deploy_observation", "G1", ["DAL-045", "DAL-048", "DAL-050"],
        "merged", "deployed", "decision", "POLICY_FAILURE", "APPLIED", "confirmed_completed",
        ["external_effect.observed_completed", "deployment.completed", "decision.created"],
        allowed_writes=list(WRITE_SETS["U"]), external_effect_trace=["confirmed_completed"],
    )
    add(
        "DAL-T-DELIVERY-OBS-001", "unknown_deploy_observation", "G5", ["DAL-045", "DAL-048", "DAL-050"],
        "merged", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "APPLIED", "unknown",
        ["external_effect.unknown", "reconciliation.required"],
        allowed_writes=list(WRITE_SETS["E"]), external_effect_trace=["unknown"],
    )
    add(
        "DAL-T-DELIVERY-OBS-001", "managed_merge", "G5", ["DAL-045", "DAL-048", "DAL-050"],
        "awaiting_merge", "merged", None, None, "APPLIED", "confirmed_completed",
        ["external_effect.intent_recorded", "external_effect.claimed", "external_effect.dispatch_started", "merge.completed"],
        allowed_writes=list(dict.fromkeys(WRITE_SETS["I"] + WRITE_SETS["X"])),
        external_effect_trace=["intent_recorded", "claimed", "dispatch_started", "confirmed_completed"],
    )
    add(
        "DAL-T-DELIVERY-OBS-001", "managed_deploy", "G5", ["DAL-045", "DAL-048", "DAL-050"],
        "merged", "deployed", None, None, "APPLIED", "confirmed_completed",
        ["external_effect.intent_recorded", "external_effect.claimed", "external_effect.dispatch_started", "deployment.completed"],
        allowed_writes=list(dict.fromkeys(WRITE_SETS["I"] + WRITE_SETS["X"])),
        external_effect_trace=["intent_recorded", "claimed", "dispatch_started", "confirmed_completed"],
    )
    route = ["usage", "account_429", "transient_429", "timeout", "5xx", "auth", "policy", "budget", "profile_drift", "unconfigured_model"]
    for variant in route:
        reason = "USAGE_LIMIT" if variant in {"usage", "account_429"} else "AUTH_REQUIRED" if variant == "auth" else "POLICY_FAILURE" if variant in {"policy", "profile_drift", "unconfigured_model"} else "BUDGET_LIMIT" if variant == "budget" else "TRANSIENT_RETRY_EXHAUSTED"
        final = "blocked_usage" if reason == "USAGE_LIMIT" else "blocked_auth" if reason == "AUTH_REQUIRED" else "needs_human"
        add("DAL-T-PROVIDER-ROUTE-001", variant, "G4", ["DAL-025", "DAL-026", "DAL-027"], "coding", final, "feature", reason, "APPLIED", None, ["feature.blocked"])
    fallback_command = {
        "schema_version": "dal.test-operation-command/1.0", "operation_spec_id": "OP-PROVIDER-ROUTE-001",
        "operation_id": "fixture-operation:verified-usage-fallback", "idempotency_key": "fixture-idempotency:verified-usage-fallback",
        "actor_type": "service", "evidence_source_type": "provider-adapter",
        "input": {
            "variant_id": "usage_fallback_eligible", "entity_id": "fixture-entity", "expected_version": 7,
            "failure_class": "usage_limit", "same_approved_profile": True, "preflight_receipt_fresh": True,
            "handoff_persisted": True, "unknown_effect_count": 0, "budget_remaining": True,
            "fallback_used": False, "provider_attempt_count": 1,
        },
    }
    add(
        "DAL-T-PROVIDER-ROUTE-001", "usage_fallback_eligible", "G4", ["DAL-025", "DAL-026", "DAL-027"],
        "coding", "coding", None, None, "FALLBACK_STARTED", None, ["provider.fallback_started"],
        allowed_writes=["run_counter", "provider_attempt", "fallback_handoff", "operation_receipt", "audit"],
        receipt_schema_override="dal.operation-receipt/1.0", operation_sequence=[fallback_command],
        scenario_assertions=[{"field": "fallback_attempt_count", "operator": "equals", "value": 1}, {"field": "fallback_used", "operator": "equals", "value": True}, {"field": "provider_attempt_count", "operator": "equals", "value": 2}],
    )
    for variant, failed_field in (
        ("usage_fallback_stale_preflight", "preflight_receipt_fresh"),
        ("usage_fallback_unknown_effect", "unknown_effect_count"),
        ("usage_fallback_already_used", "fallback_used"),
        ("usage_fallback_budget_exhausted", "budget_remaining"),
        ("usage_fallback_profile_mismatch", "same_approved_profile"),
        ("usage_fallback_missing_handoff", "handoff_persisted"),
    ):
        denied_fallback = json.loads(json.dumps(fallback_command))
        denied_fallback["operation_id"] = f"fixture-operation:{variant}"
        denied_fallback["idempotency_key"] = f"fixture-idempotency:{variant}"
        denied_fallback["input"]["variant_id"] = variant
        denied_fallback["input"][failed_field] = 1 if failed_field == "unknown_effect_count" else (True if failed_field == "fallback_used" else False)
        add(
            "DAL-T-PROVIDER-ROUTE-001", variant, "G4", ["DAL-025", "DAL-026", "DAL-027"],
            "coding", "blocked_usage", "feature", "USAGE_LIMIT", "APPLIED", None, ["feature.blocked"],
            operation_sequence=[denied_fallback],
            scenario_assertions=[{"field": "fallback_attempt_count", "operator": "equals", "value": 0}, {"field": "failed_eligibility_field", "operator": "equals", "value": failed_field}],
        )
    many("DAL-T-PROVIDER-CONTRACT-001", ["empty", "multi_tool", "prose_tool", "malformed_args", "half_stream", "multi_final", "multi_turn", "context_drift"], "G4", ["DAL-022", "DAL-023", "DAL-025", "DAL-026", "DAL-027"], "coding", "needs_human", "feature", "PROVIDER_CONTRACT_FAILURE", "APPLIED", None, ["feature.blocked"])
    add("DAL-T-CRED-001", "dependency_hook", "G2", ["DAL-017"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    for gate in ("G2", "G4"):
        many("DAL-T-CRED-001", [f"{v}--{gate.lower()}" for v in ["env", "fd", "keychain", "proxy", "parent_process", "log"]], gate, ["DAL-017", "DAL-025"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    many("DAL-T-CARD-001", ["resolved", "expired", "superseded", "stale", "apns_loss", "old_click"], "G1", ["DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "DECISION_STALE", None, [])
    many("DAL-T-DOCK-001", ["mixed_rank", "tie", "dependency", "same_root"], "G1", ["DAL-013"], "needs_human", "needs_human", None, None, "APPLIED", None, ["decision.created"], allowed_writes=["decision_projection", "operation_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    add("DAL-T-DOCK-001", "bulk_high_risk", "G1", ["DAL-013"], "needs_human", "needs_human", None, None, "POLICY_DENIED", None, [])
    many("DAL-T-BATCH-001", ["continuous", "service_restart", "fifth_item", "high_risk_interrupt"], "G1", ["DAL-013"], "needs_human", "needs_human", None, None, "APPLIED", None, ["notification.batch_flushed"], allowed_writes=["notification_batch", "notification_outbox", "operation_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    add(
        "DAL-T-BATCH-001", "all_invalid", "G1", ["DAL-013"], "needs_human", "needs_human",
        None, None, "NOOP", None, [], allowed_writes=[], receipt_schema_override="dal.operation-receipt/1.0",
        scenario_assertions=[{"field": "notification_outbox_create_count", "operator": "equals", "value": 0}, {"field": "batch_window_state", "operator": "equals", "value": "cancelled"}],
    )
    many("DAL-T-NOTIFY-001", ["ack_loss", "concurrent_claim", "restart"], "G1", ["DAL-013"], "needs_human", "needs_human", None, None, "APPLIED", None, ["notification.delivery_claimed", "notification.delivery_started", "notification.delivery_succeeded"], allowed_writes=["notification_delivery", "notification_outbox", "operation_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    permanent_events = ["notification.delivery_created"]
    for attempt in range(1, 6):
        permanent_events.extend(["notification.delivery_claimed", "notification.delivery_started", "notification.delivery_failed"])
        if attempt < 5:
            permanent_events.append("notification.delivery_retry_scheduled")
    permanent_events.append("notification.delivery_dead_lettered")
    add(
        "DAL-T-NOTIFY-001", "permanent_failure", "G1", ["DAL-013"], "needs_human", "needs_human",
        None, None, "APPLIED", None, permanent_events,
        allowed_writes=["notification_delivery", "notification_outbox", "operation_receipt", "audit"],
        receipt_schema_override="dal.operation-receipt/1.0",
        related_snapshots=[{"entity_type": "notification_delivery", "state": "dead_letter", "attempt_count": 5, "retry_schedule_seconds": [30, 120, 600, 1800]}],
    )
    many("DAL-T-EVAL-001", ["reference_object", "reference_ref", "alternate", "remote", "trusted_test_write"], "G4", ["DAL-037"], "not_started", "failed_safe", "eval_run", "POLICY_FAILURE", None, None, [], "eval_run")
    many("DAL-T-NET-001", ["finance", "personal_agent_prod", "lan", "inbound_listener"], "G2", ["DAL-017"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])

    rows.sort(key=lambda row: (row["test_id"], row["variant_id"], row["run_gate"]))
    row_keys = [(row["test_id"], row["variant_id"], row["run_gate"]) for row in rows]
    if len(row_keys) != len(set(row_keys)):
        raise ValueError("duplicate test_id/variant_id/run_gate tuple")
    for item in specs:
        covered_fixtures = [
            fixture for fixture in fixtures.values()
            if fixture["coverage_ref"] == item["spec_id"]
        ]
        allow_bindings = [
            (
                fixture["transition_command"]["actor_type"],
                tuple(fixture["transition_command"]["evidence_source_types"]),
            )
            for fixture in covered_fixtures
            if fixture["variant_id"].startswith("expanded_spec_allow--")
        ]
        expected_bindings = [
            (binding["actor_type"], tuple(binding["required_evidence_source_types"]))
            for binding in item["actor_evidence_bindings"]
        ]
        source_deny_count = sum(fixture["variant_id"].startswith("evidence_source_deny--") for fixture in covered_fixtures)
        actor_deny_count = sum(fixture["variant_id"].startswith("actor_deny--") for fixture in covered_fixtures)
        guard_deny_count = sum(fixture["variant_id"].startswith("guard_deny--") for fixture in covered_fixtures)
        if allow_bindings != expected_bindings or source_deny_count != len(expected_bindings):
            raise ValueError(f"actor/evidence binding test coverage drift: {item['spec_id']}")
        if actor_deny_count != 1 or guard_deny_count != (1 if item["guard_id"] is not None else 0):
            raise ValueError(f"actor/guard deny test coverage drift: {item['spec_id']}")
    for row in rows:
        fixture = fixtures[row["fixture_ref"]]
        oracle = oracles[row["oracle_id"]]
        if ("transition_command" in fixture) == ("operation_sequence" in fixture):
            raise ValueError(f"fixture must select exactly one executable command form: {row['test_id']}/{row['variant_id']}")
        if "operation_sequence" in fixture and not fixture["operation_sequence"]:
            raise ValueError(f"empty operation sequence: {row['test_id']}/{row['variant_id']}")
        if row["fixture_sha256"] != digest(fixture) or row["oracle_sha256"] != digest(oracle):
            raise ValueError(f"fixture/oracle hash drift: {row['test_id']}/{row['variant_id']}")
        if row["expected_entity_state"] != oracle["expected_final_snapshot"]["state"]:
            raise ValueError(f"final-state summary drift: {row['test_id']}/{row['variant_id']}")
        if row["expected_effect_state"] != (oracle["expected_external_effect_trace"][-1] if oracle["expected_external_effect_trace"] else None):
            raise ValueError(f"external-effect summary drift: {row['test_id']}/{row['variant_id']}")
        receipt = oracle["expected_receipts"]
        if row["expected_receipt_schema"] != (receipt[0]["schema_version"] if receipt else None) or row["expected_receipt_code"] != (receipt[0]["code"] if receipt else None):
            raise ValueError(f"receipt summary drift: {row['test_id']}/{row['variant_id']}")
    operation_rows = sorted(operation_specs.values(), key=lambda item: item["operation_spec_id"])
    operation_catalog = {"schema_version": "dal.operation-spec-registry/1.0", "operation_specs": operation_rows, "registry_sha256": None}
    operation_hash = write_hashed("operation-spec-registry_v1.0.json", operation_catalog, "registry_sha256")
    fixture_catalog = {"schema_version": "dal.test-fixture-catalog/1.0", "transition_registry_sha256": registry_hash, "evidence_registry_sha256": evidence_hash, "guard_registry_sha256": guard_hash, "operation_registry_sha256": operation_hash, "fixtures": fixtures, "catalog_sha256": None}
    oracle_catalog = {"schema_version": "dal.test-oracle-catalog/1.0", "transition_registry_sha256": registry_hash, "evidence_registry_sha256": evidence_hash, "guard_registry_sha256": guard_hash, "operation_registry_sha256": operation_hash, "oracles": oracles, "catalog_sha256": None}
    write_hashed("test-fixtures_v1.0.json", fixture_catalog, "catalog_sha256")
    write_hashed("test-oracles_v1.0.json", oracle_catalog, "catalog_sha256")
    manifest = {"schema_version": "dal.test-manifest/1.2", "manifest_version": "1.2", "transition_registry_sha256": registry_hash, "evidence_registry_sha256": evidence_hash, "guard_registry_sha256": guard_hash, "operation_registry_sha256": operation_hash, "test_variants": rows, "manifest_sha256": None}
    write_hashed("test-manifest_v1.2.json", manifest, "manifest_sha256")


def build_eval_schema() -> None:
    digest_fields = ["base_commit_sha", "reference_commit_sha", "base_tree_sha256", "trusted_test_bundle_sha256", "task_brief_sha256", "case_contract_sha256", "behavior_oracle_sha256", "prompt_sha256", "output_schema_sha256", "tool_policy_sha256"]
    run_input = {
        "type": "object",
        "additionalProperties": False,
        "required": ["case_id", *digest_fields],
        "properties": {"case_id": {"type": "string", "pattern": "^DAL-EVAL-[0-9]{3}$"}, **{name: {"type": "string", "pattern": "^[0-9a-f]{40}$" if name in {"base_commit_sha", "reference_commit_sha"} else "^[0-9a-f]{64}$"} for name in digest_fields}},
    }
    route = {
        "type": "object", "additionalProperties": False,
        "required": ["provider_id", "model_id", "harness_id", "harness_version", "billing_mode", "binary_sha256", "config_sha256", "classifier_sha256"],
        "properties": {
            **{name: ({"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"} if name.endswith("sha256") else {"type": "string", "minLength": 1}) for name in ["provider_id", "model_id", "harness_id", "harness_version", "binary_sha256", "config_sha256", "classifier_sha256"]},
            "billing_mode": {"enum": ["api", "subscription"]},
        },
    }
    limits = {
        "type": "object", "additionalProperties": False,
        "required": ["max_turns_per_run", "max_wall_seconds_per_run", "max_provider_attempts_per_run", "max_fix_review_cycles", "max_usd_per_run", "max_usd_per_day", "max_subscription_runs_per_day", "max_patch_bytes", "max_changed_files"],
        "properties": {
            name: {"type": (["number", "null"] if name.startswith("max_usd") else ["integer", "null"]), "minimum": 0}
            for name in ["max_turns_per_run", "max_wall_seconds_per_run", "max_provider_attempts_per_run", "max_fix_review_cycles", "max_usd_per_run", "max_usd_per_day", "max_subscription_runs_per_day", "max_patch_bytes", "max_changed_files"]
        },
    }
    p4_cases = ["DAL-EVAL-001", "DAL-EVAL-003", "DAL-EVAL-010"]
    p5_cases = [f"DAL-EVAL-{i:03d}" for i in range(1, 13)]

    def profile_branch(profile: str, cases: list[str]) -> dict:
        return {
            "properties": {
                "series_profile": {"const": profile},
                "case_ids_and_order": {"const": cases},
                "case_inputs": {
                    "minItems": len(cases),
                    "maxItems": len(cases),
                    "prefixItems": [
                        {"properties": {"case_id": {"const": case_id}}, "required": ["case_id"]}
                        for case_id in cases
                    ],
                },
            },
            "required": ["series_profile", "case_ids_and_order", "case_inputs"],
        }

    ready_route = {
        "properties": {
            name: {"type": "string", "pattern": "^[0-9a-f]{64}$"}
            for name in ("binary_sha256", "config_sha256", "classifier_sha256")
        }
    }
    ready_limits = {
        "properties": {
            name: ({"type": "number", "exclusiveMinimum": 0} if name.startswith("max_usd") else {"type": "integer", "minimum": 1})
            for name in ("max_turns_per_run", "max_wall_seconds_per_run", "max_provider_attempts_per_run", "max_usd_per_run", "max_usd_per_day", "max_subscription_runs_per_day", "max_patch_bytes", "max_changed_files")
        }
    }
    ready_limits["properties"]["max_fix_review_cycles"] = {"const": 3}
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "dal.eval-run-manifest/1.0",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "series_id", "series_profile", "series_status", "case_ids_and_order", "repeat_n", "seed", "case_inputs", "route", "limits", "stop_rule_ids", "created_at", "manifest_sha256"],
        "properties": {
            "schema_version": {"const": "dal.eval-run-manifest/1.0"},
            "series_id": {"type": "string", "minLength": 1},
            "series_profile": {"enum": ["P4", "P5"]},
            "series_status": {"enum": ["not_ready", "ready", "running", "closed"]},
            "case_ids_and_order": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "pattern": "^DAL-EVAL-[0-9]{3}$"}},
            "repeat_n": {"const": 3},
            "seed": {"type": ["integer", "null"]},
            "case_inputs": {"type": "array", "minItems": 1, "items": run_input},
            "route": route,
            "limits": limits,
            "stop_rule_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "created_at": {"type": "string", "format": "date-time"},
            "manifest_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "allOf": [
            {"oneOf": [profile_branch("P4", p4_cases), profile_branch("P5", p5_cases)]},
            {
                "if": {"properties": {"series_status": {"enum": ["ready", "running", "closed"]}}, "required": ["series_status"]},
                "then": {"properties": {"route": ready_route, "limits": ready_limits}},
            },
        ],
        "x-canonicalization": {"algorithm": "RFC8785-JCS", "hash": "SHA-256", "excluded_hash_fields": ["manifest_sha256"], "array_order": "preserved"},
        "$comment": "P4/P5 denominator order and ready-state non-null route/limit constraints are enforced by allOf/oneOf/if/then; manifest_sha256 canonical-byte verification remains a controller operation.",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "eval-run-manifest_schema_v1.0.json").write_text(json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    specs, registry_hash = build_transition_registry()
    evidence_hash, guard_hash = build_machine_registries(specs)
    build_test_contracts(specs, registry_hash, evidence_hash, guard_hash)
    build_eval_schema()
    print(json.dumps({"transition_specs": len(specs), "test_variants": len(json.loads((OUT / 'test-manifest_v1.2.json').read_text())["test_variants"]), "registry_sha256": registry_hash, "evidence_registry_sha256": evidence_hash, "guard_registry_sha256": guard_hash}, sort_keys=True))


if __name__ == "__main__":
    main()
