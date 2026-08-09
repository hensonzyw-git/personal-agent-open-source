#!/usr/bin/env python3
"""Build the Wave 0 machine-readable DAL contract manifests.

This is documentation tooling only.  It does not import or execute DAL runtime,
GitHub, Worker, provider, or Personal Agent code.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "manifests"

BASE_WRITES = ["aggregate", "business_event", "transition_receipt", "audit"]
RECOVERY_BASE_WRITES = ["recovery_case", "business_event", "recovery_transition_receipt", "audit"]
WRITE_SETS = {
    "A": BASE_WRITES,
    "D": [*BASE_WRITES, "decision", "decision_projection", "notification_outbox"],
    "H": ["approval_consume", "plan_version", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "P": ["decision_consume", "approval_consume", "lease_or_capability", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "J": ["decision_consume", "approval_consume", "approval_action_receipt", *BASE_WRITES, "decision_projection"],
    "O": ["external_effect_outcome", "authoritative_post_read", "decision_resolve", "approval_action_receipt_ref", *BASE_WRITES, "decision_projection"],
    "U": ["external_effect_outcome", "authoritative_post_read", "incident_decision", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "I": ["approval_consume", "capability_issue", "capability_consume", "external_effect_intent", *BASE_WRITES, "notification_outbox"],
    "X": ["capability_consume", "external_effect_outcome", "authoritative_post_read", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "K": ["lease_revoke", "capability_revoke", "decision_resolve", "approval_resolve", *BASE_WRITES, "decision_projection", "notification_outbox"],
    "E": ["external_effect_inventory", *BASE_WRITES, "decision", "decision_projection", "notification_outbox"],
    "Q": ["recovery_case", "decision_consume", "approval_consume", *BASE_WRITES, "decision_projection", "notification_outbox"],
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
) -> dict:
    stopped = to_state.startswith("blocked_") or to_state in {"needs_human", "reconciliation_required", "paused"}
    result_reason_code = reasons[-1] if reasons and stopped else None
    return {
        "schema_version": "dal.transition-spec/1.0",
        "spec_id": spec_id,
        "aggregate_type": aggregate_type,
        "from_state": from_state,
        "to_state": to_state,
        "command_type": command,
        "event_type": event,
        "allowed_actor_types": actors,
        "allowed_evidence_source_types": sources,
        "allowed_reason_codes": reasons,
        "allowed_decision_reason_codes": decision_reasons or [],
        "result_reason_owner": "feature" if result_reason_code is not None and aggregate_type == "feature" else None,
        "result_reason_code": result_reason_code,
        "evidence_schema_version": evidence,
        "requires_decision_action": action,
        "atomic_write_set": expanded_write_set(write_set, spec_id) if isinstance(write_set, str) else write_set,
        "guard_id": guard,
        "success_receipt_schema": "dal.transition-receipt/1.0",
        "success_receipt_code": "APPLIED",
    }


def build_transition_registry() -> tuple[list[dict], str]:
    rows: list[dict] = []
    add = rows.append
    normal = [
        ("SM-CREATE", None, "intake", "create_feature", "feature.created", ["service"], ["workflow-service"], "dal.evidence.feature/1.0", None, "A"),
        ("SM-PLAN-START", "intake", "planning", "start_plan", "plan.started", ["service"], ["workflow-service"], "dal.evidence.plan-start/1.0", None, "A"),
        ("SM-PLAN-READY", "planning", "awaiting_plan_review", "record_plan", "plan.ready", ["service"], ["planner"], "dal.evidence.plan/1.0", None, "D"),
        ("SM-PLAN-REVISE", "awaiting_plan_review", "planning", "request_revision", "plan.revision_requested", ["human"], ["registered-device"], "dal.evidence.revision/1.0", "request_revision", "H"),
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
        ("SM-MERGE-INTENT", "awaiting_merge", "awaiting_merge", "start_managed_merge", "external_effect.intent_recorded", ["service"], ["github-control"], "dal.evidence.github-merge-intent/1.0", "execute_merge", "I"),
        ("SM-MERGED-DISPATCHED", "awaiting_merge", "merged", "record_managed_merge", "merge.completed", ["service"], ["github-control"], "dal.evidence.github-receipt/1.0", None, "X"),
        ("SM-DEPLOY-APPROVAL", "merged", "merged", "record_deploy_approval", "approval.recorded", ["human"], ["registered-device"], "dal.evidence.deploy-approval/1.0", "approve_deploy", "J"),
        ("SM-DEPLOYED-OBSERVED", "merged", "deployed", "record_observed_deployment", "deployment.completed", ["service"], ["delivery-control"], "dal.evidence.observed-deployment/1.0", None, "O"),
        ("SM-DEPLOYED-OBSERVED-UNAPPROVED", "merged", "deployed", "record_unapproved_observed_deployment", "deployment.completed", ["service"], ["delivery-control"], "dal.evidence.observed-deployment/1.0", None, "U"),
        ("SM-DEPLOY-INTENT", "merged", "merged", "start_managed_deploy", "external_effect.intent_recorded", ["service"], ["delivery-control"], "dal.evidence.deployment-intent/1.0", "execute_deploy", "I"),
        ("SM-DEPLOYED", "merged", "deployed", "record_deployment", "deployment.completed", ["service"], ["delivery-control"], "dal.evidence.deployment-receipt/1.0", None, "X"),
        ("SM-COMPLETE-MERGED", "merged", "completed", "complete_without_deploy", "feature.completed", ["service"], ["github-control"], "dal.evidence.completion/1.0", None, "A"),
        ("SM-COMPLETE-DEPLOYED", "deployed", "completed", "complete_after_deploy", "feature.completed", ["service"], ["delivery-control"], "dal.evidence.production-verification/1.0", None, "A"),
    ]
    guard_overrides = {
        "SM-MERGE-APPROVAL": "PR_ONLY_APPROVAL_ACTION_RECEIPT",
        "SM-MERGED-OBSERVED": "OBSERVED_MERGE_ACTION_RECEIPT_VALID_AT_MERGED_AT",
        "SM-MERGED-OBSERVED-UNAPPROVED": "OBSERVED_MERGE_WITHOUT_VALID_ACTION_RECEIPT",
        "SM-DEPLOY-APPROVAL": "G5_REQUIRED_AND_APPROVAL_ACTION_RECEIPT",
        "SM-DEPLOYED-OBSERVED": "G5_REQUIRED_AND_OBSERVED_DEPLOY_ACTION_RECEIPT_VALID_AT_DEPLOYED_AT",
        "SM-DEPLOYED-OBSERVED-UNAPPROVED": "G5_REQUIRED_AND_OBSERVED_DEPLOY_WITHOUT_VALID_ACTION_RECEIPT",
    }
    for row in normal:
        reasons = ["TEST_BLOCKED"] if row[0] == "SM-VERIFY-BLOCK" else []
        guard = guard_overrides.get(row[0], "G5_REQUIRED" if row[0].startswith("SM-DEPLOY") or row[0] in {"SM-MERGE-INTENT", "SM-MERGED-DISPATCHED"} else None)
        decision_reasons = ["POLICY_FAILURE"] if row[0] in {"SM-MERGED-OBSERVED-UNAPPROVED", "SM-DEPLOYED-OBSERVED-UNAPPROVED"} else []
        add(spec(row[0], row[1], row[2], row[3], row[4], row[5], row[6], reasons, row[7], row[8], row[9], guard, decision_reasons=decision_reasons))

    pause_states = ["planning", "awaiting_plan_review", "approved", "coding", "verifying", "reviewing", "fixing", "verified", "awaiting_merge"]
    cancel_states = ["intake", *pause_states, "blocked_requirement", "blocked_usage", "blocked_auth", "blocked_test", "blocked_external_prerequisite", "blocked_unknown", "needs_human", "paused", "reconciliation_required"]
    for state in pause_states:
        add(spec(f"SM-PAUSE--{state}", state, "paused", "pause_feature", "feature.paused", ["human", "service"], ["registered-device", "workflow-service"], ["USER_PAUSE"], "dal.evidence.checkpoint/1.0", "pause", "K"))
    for state in cancel_states:
        add(spec(f"SM-CANCEL--{state}", state, "cancelled", "cancel_feature", "feature.cancelled", ["human"], ["registered-device"], [], "dal.evidence.cancellation-impact/1.0", "cancel_feature|cancel_with_effects", "K", "CANCEL_EFFECT_INVARIANT"))

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
        "REQUIREMENT_MISSING": ("blocked_requirement", ["planning"], "supply_requirement"),
        "USAGE_LIMIT": ("blocked_usage", ["planning", "coding", "reviewing", "fixing"], "resume_frozen_route"),
        "AUTH_REQUIRED": ("blocked_auth", ["planning", "coding", "reviewing", "fixing"], "resume_after_auth"),
        "TEST_BLOCKED": ("blocked_test", ["fixing"], "continue_fix"),
        "EXTERNAL_PREREQUISITE": ("blocked_external_prerequisite", ["planning", "approved", "coding", "verifying", "reviewing", "fixing"], "resume_after_prerequisite"),
        "TRANSIENT_RETRY_EXHAUSTED": ("needs_human", ["planning", "coding", "reviewing", "fixing"], "retry_from_checkpoint"),
        "PROVIDER_CONTRACT_FAILURE": ("needs_human", ["planning", "coding", "reviewing", "fixing"], "retry_from_checkpoint"),
        "POLICY_FAILURE": ("needs_human", ["planning"], "replan"),
        "BUDGET_LIMIT": ("needs_human", ["planning", "coding", "reviewing", "fixing"], "resume_with_budget"),
        "REVIEW_LOOP_LIMIT": ("needs_human", ["fixing"], "continue_fix"),
        "GIT_CONFLICT": ("needs_human", ["planning"], "replan"),
        "STATE_DRIFT": ("needs_human", ["planning"], "replan"),
        "USER_PAUSE": ("paused", pause_states, "resume_checkpoint"),
        "UNKNOWN_NO_EXTERNAL_INTENT": ("blocked_unknown", unknown_sources, "resume_checkpoint"),
    }
    for reason, (from_state, targets, action) in resume_map.items():
        for target in targets:
            add(spec(f"RESUME--{reason}--{target}", from_state, target, action, "feature.resumed", ["human"], ["registered-device"], [reason], "dal.evidence.drift-probe/1.0", action, "P", f"CHECKPOINT_EQUALS_{target.upper()}"))

    for target in unknown_sources:
        for suffix, guard in (("NOT-EXECUTED", "EFFECT_CONFIRMED_NOT_EXECUTED"), ("COMPLETED-NONSTATE", "EFFECT_CONFIRMED_COMPLETED_NONSTATE")):
            add(spec(f"RECONCILE-{suffix}--{target}", "reconciliation_required", target, "resume_checkpoint", "feature.resumed", ["human"], ["registered-device", "external-effect-controller"], ["EXTERNAL_RESULT_UNKNOWN"], "dal.evidence.reconciliation/1.0", "resume_checkpoint", "R", f"{guard}_AND_CHECKPOINT_{target.upper()}"))
    add(spec("RECONCILE-MERGE", "reconciliation_required", "merged", "accept_merge_result", "merge.completed", ["human"], ["registered-device", "github-control"], ["EXTERNAL_RESULT_UNKNOWN"], "dal.evidence.github-observed-merge/1.0", "accept_merge_result", "R", "EFFECT_IS_MERGE"))
    add(spec("RECONCILE-DEPLOY", "reconciliation_required", "deployed", "accept_deploy_result", "deployment.completed", ["human"], ["registered-device", "delivery-control"], ["EXTERNAL_RESULT_UNKNOWN"], "dal.evidence.observed-deployment/1.0", "accept_deploy_result", "R", "EFFECT_IS_DEPLOY"))
    add(spec("RECOVERY-OPEN--POST-EFFECT", "needs_human", "needs_human", "open_recovery_case", "recovery_case.created", ["human"], ["registered-device"], ["POST_EFFECT_EXCEPTION", "RECOVERY_REQUIRED"], "dal.evidence.recovery-proposal/1.0", "open_recovery_case", "Q"))
    add(spec("RECONCILE-OPEN-RECOVERY", "reconciliation_required", "needs_human", "open_recovery", "recovery_case.created", ["human"], ["registered-device", "external-effect-controller"], ["EXTERNAL_RESULT_UNKNOWN", "RECOVERY_REQUIRED"], "dal.evidence.recovery-proposal/1.0", "open_recovery", "R"))

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
    for row in recovery_specs:
        add(spec(row[0], row[1], row[2], row[3], row[4], row[5], row[6], [], row[7], row[8], row[9], aggregate_type="recovery_case"))

    rows.sort(key=lambda row: row["spec_id"])
    spec_ids = [row["spec_id"] for row in rows]
    if len(spec_ids) != len(set(spec_ids)):
        raise ValueError("duplicate TransitionSpec spec_id")
    required = {
        "aggregate_type", "from_state", "to_state", "command_type", "event_type",
        "allowed_actor_types", "allowed_evidence_source_types",
        "allowed_reason_codes", "allowed_decision_reason_codes", "result_reason_owner",
        "result_reason_code", "evidence_schema_version",
        "atomic_write_set", "success_receipt_schema", "success_receipt_code",
    }
    for row in rows:
        if not required.issubset(row) or not row["allowed_actor_types"] or not row["allowed_evidence_source_types"]:
            raise ValueError(f"incomplete TransitionSpec: {row['spec_id']}")
    registry = {"schema_version": "dal.transition-spec-registry/1.0", "specs": rows, "registry_sha256": None}
    registry_hash = write_hashed("transition-spec-registry_v1.0.json", registry, "registry_sha256")
    return rows, registry_hash


def build_test_contracts(specs: list[dict], registry_hash: str) -> None:
    fixtures: dict[str, dict] = {}
    oracles: dict[str, dict] = {}
    rows: list[dict] = []

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
            "coverage_ref": coverage_ref,
        }
        receipt_schema = receipt_schema_override or (
            "dal.transition-receipt/1.0" if code is not None and entity_type == "feature"
            else "dal.recovery-transition-receipt/1.0" if code is not None and entity_type == "recovery_case"
            else None
        )
        denied_codes = {"POLICY_DENIED", "APPROVAL_INVALID", "DECISION_STALE", "CAPABILITY_STALE", "IDEMPOTENCY_CONFLICT", "VERSION_CONFLICT", "ILLEGAL_TRANSITION", "TERMINAL_STATE"}
        allowed = [] if code in denied_codes else (allowed_writes if allowed_writes is not None else (default_applied_write_set(entity_type, event_trace) if code == "APPLIED" else []))
        oracle = {
            "schema_version": "dal.test-oracle/1.0",
            "test_id": test_id,
            "variant_id": variant,
            "run_gate": gate,
            "pre_state": fixture["pre_state"],
            "injection_operation": fixture["injection_operation"],
            "expected_state_trace": [pre_state, final_state],
            "expected_event_trace": event_trace,
            "expected_receipts": [] if code is None else [{"schema_version": receipt_schema, "code": code, "count": 1}],
            "expected_external_effect_trace": [] if effect is None else [effect],
            "expected_final_snapshot": {"entity_type": entity_type, "state": final_state, "reason_owner": reason_owner, "reason_code": reason},
            "expected_related_snapshots": related_snapshots or [],
            "allowed_write_set": allowed,
            "forbidden_side_effects": ["provider_call", "github_write", "worker_start", "production_access"] if gate == "G1" else ["unapproved_external_effect", "production_access"],
            "coverage_ref": coverage_ref,
        }
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
            "expected_receipt_schema": receipt_schema,
            "expected_receipt_code": code,
            "expected_effect_state": effect,
            "owner_tasks": owners,
            "run_gate": gate,
        })

    # Every expanded TransitionSpec gets one legal, illegal-actor and illegal-source row.
    for item in specs:
        suffix = item["spec_id"].lower().replace("_", "-")
        test_id = "DAL-T-RECOVERY-001" if item["aggregate_type"] == "recovery_case" else "DAL-T-SM-001"
        related = [{"entity_type": "decision", "state": "open", "reason_owner": "decision", "reason_code": code} for code in item["allowed_decision_reason_codes"]]
        add(test_id, f"expanded_spec_allow--{suffix}", "G1", ["DAL-009", "DAL-010"], item["from_state"], item["to_state"], item["result_reason_owner"], item["result_reason_code"], "APPLIED", None, [item["event_type"]], entity_type=item["aggregate_type"], coverage_ref=item["spec_id"], allowed_writes=item["atomic_write_set"], related_snapshots=related)
        add(test_id, f"actor_deny--{suffix}", "G1", ["DAL-009", "DAL-010"], item["from_state"], item["from_state"], None, None, "POLICY_DENIED", None, [], entity_type=item["aggregate_type"], coverage_ref=item["spec_id"])
        add(test_id, f"evidence_source_deny--{suffix}", "G1", ["DAL-009", "DAL-010"], item["from_state"], item["from_state"], None, None, "POLICY_DENIED", None, [], entity_type=item["aggregate_type"], coverage_ref=item["spec_id"])

    def many(test: str, variants: list[str], gate: str, owners: list[str], pre: str | None, final: str | None, reason_owner: str | None, reason: str | None, code: str | None, effect: str | None, events: list[str], entity: str = "feature", allowed_writes: list[str] | None = None, receipt_schema_override: str | None = None) -> None:
        for variant in variants:
            add(test, variant, gate, owners, pre, final, reason_owner, reason, code, effect, events, entity, allowed_writes=allowed_writes, receipt_schema_override=receipt_schema_override)

    add("DAL-T-SM-001", "illegal_edge", "G1", ["DAL-009", "DAL-010"], "intake", "intake", None, None, "ILLEGAL_TRANSITION", None, [])
    add("DAL-T-SM-001", "event_mismatch", "G1", ["DAL-009", "DAL-010"], "planning", "planning", None, None, "ILLEGAL_TRANSITION", None, [])
    add("DAL-T-CMD-IDEMPOTENCY-001", "idempotent_replay", "G1", ["DAL-009", "DAL-010"], "planning", "awaiting_plan_review", None, None, "IDEMPOTENT_REPLAY", None, ["plan.ready"])
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
    many("DAL-T-APP-001", ["double_tap", "concurrent_consume", "revoke_race"], "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "APPROVAL_INVALID", None, [])
    add("DAL-T-APP-EXP-001", "approval_expired", "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "APPROVAL_INVALID", None, [])
    add("DAL-T-APP-EXP-001", "decision_expired", "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "DECISION_STALE", None, [])
    many("DAL-T-STATEHASH-001", ["field", "order", "null", "version", "forged_digest", "effect_inventory_order", "effect_inventory_membership"], "G1", ["DAL-011", "DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "DECISION_STALE", None, [])
    many("DAL-T-ARTIFACTHASH-001", ["body", "metadata", "canonicalizer", "field_boundary", "forged_digest"], "G1", ["DAL-011"], "awaiting_plan_review", "awaiting_plan_review", None, None, "APPROVAL_INVALID", None, [])
    many("DAL-T-REC-001", ["synthetic_kill", "synthetic_disconnect", "synthetic_ack_loss"], "G1", ["DAL-009", "DAL-010"], "coding", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "APPLIED", "unknown", ["reconciliation.required"])
    many("DAL-T-REC-001", ["unknown_merge_cancel", "unknown_deploy_cancel"], "G1", ["DAL-009", "DAL-010"], "reconciliation_required", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "POLICY_DENIED", "unknown", [])
    many("DAL-T-REC-001", ["worker_kill", "worker_disconnect"], "G2", ["DAL-016", "DAL-017"], "coding", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "APPLIED", "unknown", ["reconciliation.required"])
    recovery = {
        "investigation_fail": ("investigating", "blocked", "recovery_case.blocked", "APPLIED"),
        "cancel_investigating": ("investigating", "cancelled", "recovery_case.cancelled", "APPLIED"),
        "cancel_approved": ("approved", "cancelled", "recovery_case.cancelled", "APPLIED"),
        "start_revoked": ("approved", "blocked", "recovery_case.blocked", "APPLIED"),
        "execution_blocked": ("executing", "blocked", "recovery_case.blocked", "APPLIED"),
        "verification_blocked": ("verifying", "blocked", "recovery_case.blocked", "APPLIED"),
        "reinvestigate": ("blocked", "investigating", "recovery_case.reinvestigating", "APPLIED"),
        "rearm": ("blocked", "awaiting_decision", "recovery_case.awaiting_decision", "APPLIED"),
    }
    for variant, (pre, final, event, code) in recovery.items():
        add("DAL-T-RECOVERY-001", variant, "G1", ["DAL-009", "DAL-010", "DAL-011"], pre, final, None, None, code, None, [event], "recovery_case")
    add("DAL-T-RESTART-001", "service_retry_limit", "G1", ["DAL-009"], "coding", "needs_human", "feature", "TRANSIENT_RETRY_EXHAUSTED", "APPLIED", None, ["feature.blocked"])
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
    many("DAL-T-GIT-ACK-001", ["push_ack_loss", "pr_ack_loss"], "G3", ["DAL-015", "DAL-034"], "awaiting_merge", "reconciliation_required", "feature", "EXTERNAL_RESULT_UNKNOWN", "APPLIED", "unknown", ["reconciliation.required"])
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
        add("DAL-T-PRONLY-MERGE-001", variant, "G3", ["DAL-015", "DAL-032", "DAL-033"], "awaiting_merge", final, "decision" if variant == "unapproved_observation" else ("feature" if variant == "unknown_observation" else None), "POLICY_FAILURE" if variant == "unapproved_observation" else ("EXTERNAL_RESULT_UNKNOWN" if variant == "unknown_observation" else None), code, effect, events)
    many("DAL-T-DELIVERY-OBS-001", ["deploy_approve_record", "manual_deploy_observation", "unapproved_deploy_observation", "unknown_deploy_observation", "managed_merge", "managed_deploy"], "G5", ["DAL-045", "DAL-048", "DAL-050"], "merged", "merged", None, None, "POLICY_DENIED", None, [])
    route = ["usage", "account_429", "transient_429", "timeout", "5xx", "auth", "policy", "budget", "profile_drift", "unconfigured_model"]
    for variant in route:
        reason = "USAGE_LIMIT" if variant in {"usage", "account_429"} else "AUTH_REQUIRED" if variant == "auth" else "POLICY_FAILURE" if variant in {"policy", "profile_drift", "unconfigured_model"} else "BUDGET_LIMIT" if variant == "budget" else "TRANSIENT_RETRY_EXHAUSTED"
        final = "blocked_usage" if reason == "USAGE_LIMIT" else "blocked_auth" if reason == "AUTH_REQUIRED" else "needs_human"
        add("DAL-T-PROVIDER-ROUTE-001", variant, "G4", ["DAL-025", "DAL-026", "DAL-027"], "coding", final, "feature", reason, "APPLIED", None, ["feature.blocked"])
    many("DAL-T-PROVIDER-CONTRACT-001", ["empty", "multi_tool", "prose_tool", "malformed_args", "half_stream", "multi_final", "multi_turn", "context_drift"], "G4", ["DAL-022", "DAL-023", "DAL-025", "DAL-026", "DAL-027"], "coding", "needs_human", "feature", "PROVIDER_CONTRACT_FAILURE", "APPLIED", None, ["feature.blocked"])
    add("DAL-T-CRED-001", "dependency_hook", "G2", ["DAL-017"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    for gate in ("G2", "G4"):
        many("DAL-T-CRED-001", [f"{v}--{gate.lower()}" for v in ["env", "fd", "keychain", "proxy", "parent_process", "log"]], gate, ["DAL-017", "DAL-025"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])
    many("DAL-T-CARD-001", ["resolved", "expired", "superseded", "stale", "apns_loss", "old_click"], "G1", ["DAL-013"], "awaiting_plan_review", "awaiting_plan_review", None, None, "DECISION_STALE", None, [])
    many("DAL-T-DOCK-001", ["mixed_rank", "tie", "dependency", "same_root"], "G1", ["DAL-013"], "needs_human", "needs_human", None, None, "APPLIED", None, ["decision.created"], allowed_writes=["decision_projection", "operation_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    add("DAL-T-DOCK-001", "bulk_high_risk", "G1", ["DAL-013"], "needs_human", "needs_human", None, None, "POLICY_DENIED", None, [])
    many("DAL-T-BATCH-001", ["continuous", "service_restart", "fifth_item", "high_risk_interrupt", "all_invalid"], "G1", ["DAL-013"], "needs_human", "needs_human", None, None, "APPLIED", None, ["notification.batch_flushed"], allowed_writes=["notification_batch", "notification_outbox", "operation_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    many("DAL-T-NOTIFY-001", ["ack_loss", "concurrent_claim", "restart", "permanent_failure"], "G1", ["DAL-013"], "needs_human", "needs_human", None, None, "APPLIED", None, ["notification.delivery_failed"], allowed_writes=["notification_delivery", "notification_outbox", "operation_receipt", "audit"], receipt_schema_override="dal.operation-receipt/1.0")
    many("DAL-T-EVAL-001", ["reference_object", "reference_ref", "alternate", "remote", "trusted_test_write"], "G4", ["DAL-037"], "not_started", "failed_safe", "eval_run", "POLICY_FAILURE", None, None, [], "eval_run")
    many("DAL-T-NET-001", ["finance", "personal_agent_prod", "lan", "inbound_listener"], "G2", ["DAL-017"], "coding", "needs_human", "feature", "POLICY_FAILURE", "APPLIED", None, ["feature.blocked"])

    rows.sort(key=lambda row: (row["test_id"], row["variant_id"], row["run_gate"]))
    row_keys = [(row["test_id"], row["variant_id"], row["run_gate"]) for row in rows]
    if len(row_keys) != len(set(row_keys)):
        raise ValueError("duplicate test_id/variant_id/run_gate tuple")
    for row in rows:
        fixture = fixtures[row["fixture_ref"]]
        oracle = oracles[row["oracle_id"]]
        if row["fixture_sha256"] != digest(fixture) or row["oracle_sha256"] != digest(oracle):
            raise ValueError(f"fixture/oracle hash drift: {row['test_id']}/{row['variant_id']}")
        if row["expected_entity_state"] != oracle["expected_final_snapshot"]["state"]:
            raise ValueError(f"final-state summary drift: {row['test_id']}/{row['variant_id']}")
        if row["expected_effect_state"] != (oracle["expected_external_effect_trace"][0] if oracle["expected_external_effect_trace"] else None):
            raise ValueError(f"external-effect summary drift: {row['test_id']}/{row['variant_id']}")
        receipt = oracle["expected_receipts"]
        if row["expected_receipt_schema"] != (receipt[0]["schema_version"] if receipt else None) or row["expected_receipt_code"] != (receipt[0]["code"] if receipt else None):
            raise ValueError(f"receipt summary drift: {row['test_id']}/{row['variant_id']}")
    fixture_catalog = {"schema_version": "dal.test-fixture-catalog/1.0", "fixtures": fixtures, "catalog_sha256": None}
    oracle_catalog = {"schema_version": "dal.test-oracle-catalog/1.0", "oracles": oracles, "catalog_sha256": None}
    write_hashed("test-fixtures_v1.0.json", fixture_catalog, "catalog_sha256")
    write_hashed("test-oracles_v1.0.json", oracle_catalog, "catalog_sha256")
    manifest = {"schema_version": "dal.test-manifest/1.2", "manifest_version": "1.2", "test_variants": rows, "manifest_sha256": None}
    write_hashed("test-manifest_v1.2.json", manifest, "manifest_sha256")


def build_eval_schema() -> None:
    digest_fields = ["base_commit_sha", "base_tree_sha256", "trusted_test_bundle_sha256", "task_brief_sha256", "prompt_sha256", "output_schema_sha256", "tool_policy_sha256"]
    run_input = {
        "type": "object",
        "additionalProperties": False,
        "required": ["case_id", *digest_fields],
        "properties": {"case_id": {"type": "string", "pattern": "^DAL-EVAL-[0-9]{3}$"}, **{name: {"type": "string", "pattern": "^[0-9a-f]{40}$" if name == "base_commit_sha" else "^[0-9a-f]{64}$"} for name in digest_fields}},
    }
    route = {
        "type": "object", "additionalProperties": False,
        "required": ["provider_id", "model_id", "harness_id", "harness_version", "binary_sha256", "config_sha256", "classifier_sha256"],
        "properties": {name: ({"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"} if name.endswith("sha256") else {"type": "string", "minLength": 1}) for name in ["provider_id", "model_id", "harness_id", "harness_version", "binary_sha256", "config_sha256", "classifier_sha256"]},
    }
    limits = {
        "type": "object", "additionalProperties": False,
        "required": ["max_turns_per_run", "max_wall_seconds_per_run", "max_provider_attempts_per_run", "max_fix_review_cycles", "max_usd_per_run", "max_usd_per_day", "max_subscription_runs_per_day", "max_patch_bytes", "max_changed_files"],
        "properties": {
            name: {"type": (["number", "null"] if name.startswith("max_usd") else ["integer", "null"]), "minimum": 0}
            for name in ["max_turns_per_run", "max_wall_seconds_per_run", "max_provider_attempts_per_run", "max_fix_review_cycles", "max_usd_per_run", "max_usd_per_day", "max_subscription_runs_per_day", "max_patch_bytes", "max_changed_files"]
        },
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "dal.eval-run-manifest/1.0",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "series_id", "series_status", "case_ids_and_order", "repeat_n", "seed", "case_inputs", "route", "limits", "stop_rule_ids", "created_at", "manifest_sha256"],
        "properties": {
            "schema_version": {"const": "dal.eval-run-manifest/1.0"},
            "series_id": {"type": "string", "minLength": 1},
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
        "x-canonicalization": {"algorithm": "RFC8785-JCS", "hash": "SHA-256", "excluded_hash_fields": ["manifest_sha256"], "array_order": "preserved"},
        "x-cross-field-constraints": ["case_ids_and_order equals case_inputs.case_id in the same order", "P4 case order is DAL-EVAL-001,003,010", "P5 case order is DAL-EVAL-001..012", "all route and case digest placeholders must resolve before series_status=ready"],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "eval-run-manifest_schema_v1.0.json").write_text(json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    specs, registry_hash = build_transition_registry()
    build_test_contracts(specs, registry_hash)
    build_eval_schema()
    print(json.dumps({"transition_specs": len(specs), "test_variants": len(json.loads((OUT / 'test-manifest_v1.2.json').read_text())["test_variants"]), "registry_sha256": registry_hash}, sort_keys=True))


if __name__ == "__main__":
    main()
