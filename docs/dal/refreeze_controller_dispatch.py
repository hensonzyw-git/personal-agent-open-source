#!/usr/bin/env python3
"""Targeted authority re-freeze for the controller dispatch graph.

Follows `refreeze_controller_decisions.py` (Task #11): it re-derives every
`DAL-T-GRAPH-001` variant's dispatch decision and transition outcome from the
fixture facts, using a *separate* encoding of the frozen rules (§3 node
taxonomy, §4.1/§4.2 dispatch + universal + block_feature routes, §5 provider
composition, §6 seam gating, §7 threat vectors).  It then proves that no
unrelated authority row differs from the generated expectation, splices only
the fifteen graph-oracle entries, and recomputes the authority self-hash.

It deliberately cannot rebuild an entire authority.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFESTS = ROOT / "manifests"
sys.path.insert(0, str(ROOT))

from dal_jcs import canonical_bytes  # noqa: E402
from verify_controller_dispatch_authority import (  # noqa: E402
    expected_dispatch_rows,
    expected_oracle_entries,
    semantic_counts,
)


TARGET_TEST_ID = "DAL-T-GRAPH-001"

BASE_WRITES = ["aggregate", "business_event", "transition_receipt", "audit"]
FULL_WRITES = BASE_WRITES + [
    "decision_create", "decision_projection", "notification_outbox",
]
APPLIED_RECEIPT = [
    {"code": "APPLIED", "count": 1, "schema_version": "dal.transition-receipt/1.0"}
]
FORBIDDEN_BASE = ["unapproved_external_effect", "production_access"]


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def rehash(value: dict, field: str) -> str:
    return digest({k: v for k, v in value.items() if k != field})


# ---------------------------------------------------------------------------
# Independent re-derivation of the fifteen variants.  Node classification is a
# pure function of state; the dispatch decision and transition are pure
# functions of (state, facts, seam, stream, attempted_command, provider_attempted).
# ---------------------------------------------------------------------------
NODE_BY_STATE = {
    "intake": "deterministic", "planning": "provider",
    "awaiting_plan_review": "gate", "approved": "deterministic",
    "coding": "provider", "verifying": "deterministic", "reviewing": "provider",
    "fixing": "provider", "verified": "deterministic", "awaiting_merge": "gate",
    "merged": "gate", "deployed": "gate", "completed": "terminal",
    "blocked_requirement": "gate", "blocked_usage": "gate", "blocked_auth": "gate",
    "blocked_test": "gate", "blocked_external_prerequisite": "gate",
    "blocked_unknown": "gate", "reconciliation_required": "gate",
    "needs_human": "gate", "paused": "gate", "cancelled": "terminal",
}

# The 68 real command_type names, mirrored from the frozen transition registry
# (244 feature + 21 recovery_case + 11 external_effect specs).
REAL_COMMAND_TYPES = {
    "accept_current_fact", "accept_deploy_result", "accept_merge_result",
    "approve_plan", "approve_recovery", "block_feature", "block_recovery",
    "block_recovery_investigation", "block_recovery_start", "block_unknown",
    "cancel_feature", "cancel_recovery", "claim_external_effect",
    "complete_after_deploy", "complete_verified_recovery", "complete_without_deploy",
    "continue_fix", "create_feature", "open_recovery", "open_recovery_case",
    "pause_feature", "rearm_external_effect", "record_deploy_approval",
    "record_deployment", "record_effect_dispatch", "record_effect_not_executed",
    "record_effect_unknown", "record_fix_result", "record_managed_merge",
    "record_merge_approval", "record_merge_candidate", "record_observed_deployment",
    "record_observed_merge", "record_plan", "record_provider_result",
    "record_reconciled_completed", "record_reconciled_not_executed",
    "record_reconciliation_unknown", "record_recovery_execution",
    "record_recovery_proposal", "record_review/findings", "record_review/pass",
    "record_unapproved_observed_deployment", "record_unapproved_observed_merge",
    "record_verification/blocked", "record_verification/fixable_fail",
    "record_verification/pass", "reinvestigate_recovery",
    "release_expired_effect_claim", "replace_recovery_proposal", "replan",
    "request_revision", "require_reconciliation", "resume_after_auth",
    "resume_after_prerequisite", "resume_checkpoint", "resume_frozen_route",
    "resume_with_budget", "retry_from_checkpoint", "start_approved_fallback",
    "start_effect_reconciliation", "start_managed_deploy", "start_managed_merge",
    "start_plan", "start_provider", "start_recovery", "supply_requirement",
    "verify_recovery",
}

# §4.1 per-state dispatch-row resulting commands (independent mirror of the
# frozen dispatch table).  A real command_type absent from its state's row is an
# "unknown dispatch tuple" -> CONTRACT_SCHEMA_INVALID (§4.3/§7), distinct from a
# fabricated command the registry rejects as ILLEGAL_TRANSITION.
ROW_COMMANDS = {
    "intake": {"start_plan"},
    "planning": {"record_plan"},
    "awaiting_plan_review": {"approve_plan", "request_revision"},
    "approved": {"start_provider"},
    "coding": {"record_provider_result"},
    "verifying": {"record_verification/pass", "record_verification/fixable_fail",
                  "record_verification/blocked"},
    "reviewing": {"record_review/pass", "record_review/findings"},
    "fixing": {"record_fix_result", "block_feature"},
    "verified": {"record_merge_candidate"},
    "awaiting_merge": {"record_merge_approval", "record_observed_merge",
                       "record_unapproved_observed_merge", "start_managed_merge",
                       "record_managed_merge"},
    "merged": {"complete_without_deploy", "record_deploy_approval",
               "start_managed_deploy", "record_observed_deployment",
               "record_deployment", "record_unapproved_observed_deployment"},
    "deployed": {"complete_after_deploy"},
    "completed": set(),
    "blocked_requirement": {"supply_requirement"},
    "blocked_usage": {"resume_frozen_route", "start_approved_fallback"},
    "blocked_auth": {"resume_after_auth"},
    "blocked_test": {"continue_fix"},
    "blocked_external_prerequisite": {"resume_after_prerequisite"},
    "blocked_unknown": {"resume_checkpoint", "require_reconciliation"},
    "reconciliation_required": {"resume_checkpoint", "accept_merge_result",
                                "accept_deploy_result", "open_recovery"},
    "needs_human": {"retry_from_checkpoint", "replan", "resume_with_budget",
                    "continue_fix", "accept_current_fact", "open_recovery_case",
                    "complete_verified_recovery"},
    "paused": {"resume_checkpoint"},
    "cancelled": set(),
}


def _round(facts: dict) -> int:
    # §5.2: round = whether a record_review/findings receipt exists since the
    # current plan's record_plan receipt.  prior_findings_receipts (an earlier
    # plan) must not steer this.
    return 2 if facts["has_findings_receipt_since_plan"] else 1


def _assemble(state, node_type, orchestration, handlers, rnd, resulting,
              extra, seam, trace, events, receipts, writes, reason, coverage):
    forbidden = list(FORBIDDEN_BASE) + list(extra)
    if seam == "injected":
        # §6: injected seam MUST NOT spawn any subprocess.
        forbidden.append("subprocess_spawn")
    # forbidden_side_effects is a set semantically; canonical sort matches the
    # generator and makes the comparison order-independent.
    forbidden = sorted(forbidden)
    return {
        "dispatch": {
            "node_type": node_type,
            "orchestration_action": orchestration,
            "handler_sequence": [] if handlers is None else handlers,
            "round": rnd,
            "resulting_command": resulting,
            "seam": seam,
        },
        "trace": trace,
        "events": events,
        "receipts": receipts,
        "writes": writes,
        "final": {"state": trace[-1], "reason_code": reason},
        "forbidden": forbidden,
        "coverage": coverage,
    }


def derive(fixture: dict) -> dict:
    """Return {dispatch, trace, events, receipts, writes, final, forbidden, coverage}."""
    state = fixture["pre_state"]["state"]
    facts = fixture["controller_facts"]
    seam = fixture["seam"]
    stream = fixture["injected_provider_stream"]
    attempted = fixture["attempted_resulting_command"]
    provider_attempted = fixture["provider_attempted"]
    node_type = NODE_BY_STATE[state]

    handlers: list[str] = []
    orchestration = None
    resulting = None
    rnd = None

    if node_type == "terminal":
        orchestration, resulting = None, None
    elif node_type == "deterministic":
        orchestration = {
            "intake": "validate_feature_shape",
            "approved": "approval_validity_and_sha_binding",
            "verified": "merge_candidate_sha_binding",
            "verifying": "test_receipt_gate",
        }.get(state)
        resulting = None if provider_attempted else {
            "intake": "start_plan", "approved": "start_provider",
            "verified": "record_merge_candidate", "verifying": "record_verification/pass",
        }.get(state)
    elif node_type == "provider":
        if state == "planning":
            orchestration, handlers = "planning_compose", ["consume_provider_stream", "plan_cross_fields"]
        elif state == "reviewing":
            rnd = _round(facts)
            if rnd == 2:
                orchestration, handlers = "reviewing_compose_round_2", \
                    ["consume_provider_stream", "post_fix_verdict", "open_finding_set"]
            else:
                orchestration, handlers = "reviewing_compose_round_1", \
                    ["consume_provider_stream", "review_independence", "review_disposition"]
        else:
            orchestration = "coding_or_fixing_forward_ref"
        resulting = {"planning": "record_plan", "reviewing": "record_review/pass"}.get(state)

    # Universal legality gate: a fabricated command_type is ILLEGAL_TRANSITION.
    if attempted is not None and attempted not in REAL_COMMAND_TYPES:
        return _assemble(state, node_type, None, [], None, None, ["provider_call"], seam,
                         [state], [], [], [], None, "ILLEGAL_TRANSITION--planning")

    # §4.3 unknown dispatch tuple: a real command_type absent from this state's
    # dispatch row -> CONTRACT_SCHEMA_INVALID (controller-side), distinct from
    # the fabricated-command gate above.
    if attempted is not None and attempted not in ROW_COMMANDS[state]:
        return _assemble(state, node_type, "fail_closed_unknown_tuple", [], None, None,
                         ["provider_call"], seam, [state], [], [], [], None,
                         "CONTRACT_SCHEMA_INVALID--planning")

    if node_type == "deterministic" and provider_attempted:
        return _assemble(state, node_type, orchestration, [], None, None, ["provider_call"], seam,
                         [state], [], [], [], None, None)

    if node_type == "provider":
        if stream == ["cancelled"]:
            if facts["cancel_receipt_present"]:
                return _assemble(state, node_type, "close_without_transition", [], None, None,
                                 ["provider_handler_invocation"], seam, [state], [], [], [], None,
                                 "SM-CANCEL--planning")
            return _assemble(state, node_type, orchestration, handlers, rnd, None, [], seam,
                             [state, "needs_human"], BLOCK_EVENT, APPLIED_RECEIPT,
                             FULL_WRITES, "PROVIDER_CONTRACT_FAILURE", "BLK-CONTRACT--planning")
        if stream is not None and len(stream) == 0:
            return _assemble(state, node_type, orchestration, handlers, rnd, None, [], seam,
                             [state, "needs_human"], BLOCK_EVENT, APPLIED_RECEIPT,
                             FULL_WRITES, "PROVIDER_CONTRACT_FAILURE", "BLK-CONTRACT--planning")
        if seam == "subprocess" and not facts["preflight_receipt_present"]:
            return _assemble(state, node_type, orchestration, [], rnd, None, ["subprocess_spawn"], seam,
                             [state, "blocked_auth"], BLOCK_EVENT, APPLIED_RECEIPT,
                             FULL_WRITES, "AUTH_REQUIRED", "BLK-AUTH--planning")
        if stream and stream[0].get("drift"):
            return _assemble(state, node_type, orchestration, handlers, rnd, None, [], seam,
                             [state, "needs_human"], BLOCK_EVENT, APPLIED_RECEIPT,
                             FULL_WRITES, "PROVIDER_CONTRACT_FAILURE", "BLK-CONTRACT--planning")
        if state == "reviewing" and stream and stream[0].get("independence_reuse"):
            return _assemble(state, node_type, "reviewing_compose_round_1",
                             ["consume_provider_stream", "review_independence", "review_disposition"],
                             1, None, ["disposition_adoption"], seam, [state], [], [], [], None, None)
        if state == "fixing" and facts["review_fix_cycle_count"] >= 3:
            return _assemble(state, node_type, "fixing_compose_forward_ref", [], None,
                             "block_feature", ["coder_derivation"], seam,
                             [state, "needs_human"], BLOCK_EVENT, APPLIED_RECEIPT,
                             FULL_WRITES, "REVIEW_LOOP_LIMIT", "BLK-LOOP--fixing")
        if state == "reviewing":
            if rnd == 2:
                verdict = stream[0].get("verdict", "verified") if stream else "verified"
                resulting = "record_review/pass" if verdict == "verified" else "record_review/findings"
            else:
                disp = stream[0].get("disposition", "approve") if stream else "approve"
                gaps = bool(stream[0].get("acceptance_gaps")) if stream else False
                resulting = "record_review/findings" if (disp == "request_changes" or gaps) else "record_review/pass"

    if resulting == "record_plan":
        return _assemble(state, node_type, orchestration, handlers, rnd, "record_plan", [], seam,
                         [state, "awaiting_plan_review"], ["plan.ready"], APPLIED_RECEIPT,
                         FULL_WRITES, None, None)
    if resulting == "record_review/pass":
        return _assemble(state, node_type, orchestration, handlers, rnd, "record_review/pass", [], seam,
                         [state, "verified"], ["review.completed"], APPLIED_RECEIPT,
                         BASE_WRITES, None, None)
    if resulting == "record_review/findings":
        return _assemble(state, node_type, orchestration, handlers, rnd, "record_review/findings", [], seam,
                         [state, "fixing"], ["fix.requested"], APPLIED_RECEIPT,
                         BASE_WRITES, None, None)
    return _assemble(state, node_type, orchestration, handlers, rnd, None, [], seam,
                     [state], [], [], [], None, None)


BLOCK_EVENT = ["feature.blocked"]


def refreeze_targeted() -> None:
    registry = load(MANIFESTS / "controller-dispatch-registry_v1.0.json")
    fixtures = load(MANIFESTS / "controller-dispatch-fixtures_v1.0.json")
    oracles = load(MANIFESTS / "controller-dispatch-oracles_v1.0.json")
    manifest = load(MANIFESTS / "controller-dispatch-manifest_v1.0.json")

    # Independent re-derivation against the frozen oracle.
    for oracle_id, oracle in sorted(oracles["oracles"].items()):
        variant = oracle["variant_id"]
        fixture = fixtures["fixtures"][f"dal.controller-dispatch.fixture/{TARGET_TEST_ID}/{variant}/G3/1.0"]
        derived = derive(fixture)
        if oracle["expected_dispatch"] != derived["dispatch"]:
            raise SystemExit(
                f"{variant}: dispatch drift\noracle={oracle['expected_dispatch']}\nderived={derived['dispatch']}")
        if oracle["expected_state_trace"] != derived["trace"]:
            raise SystemExit(f"{variant}: state trace drift")
        if oracle["expected_event_trace"] != derived["events"]:
            raise SystemExit(f"{variant}: event trace drift")
        if oracle["expected_receipts"] != derived["receipts"]:
            raise SystemExit(f"{variant}: receipt drift")
        if oracle["allowed_write_set"] != derived["writes"]:
            raise SystemExit(f"{variant}: write set drift")
        if oracle["expected_final_snapshot"]["state"] != derived["final"]["state"]:
            raise SystemExit(f"{variant}: final state drift")
        if oracle["expected_final_snapshot"]["reason_code"] != derived["final"]["reason_code"]:
            raise SystemExit(f"{variant}: reason drift")
        if oracle["forbidden_side_effects"] != derived["forbidden"]:
            raise SystemExit(f"{variant}: forbidden drift")
        if oracle["coverage_ref"] != derived["coverage"]:
            raise SystemExit(f"{variant}: coverage drift")

    # No unrelated drift: recompute the derived authority rows and splice only
    # the graph-oracle entries (all of them, since this is the whole family).
    authority_path = MANIFESTS / "controller-dispatch-authority_v1.0.json"
    authority = load(authority_path)
    generated_rows = expected_dispatch_rows(registry)
    generated_entries = expected_oracle_entries(manifest, fixtures, oracles)

    def target(key: str) -> bool:
        return f"/{TARGET_TEST_ID}/" in key

    unexpected_rows = {
        key for key, value in generated_rows.items()
        if authority["dispatch_rows"].get(key) != value
    }
    if unexpected_rows:
        raise SystemExit(f"unrelated dispatch row drift: {sorted(unexpected_rows)}")
    unexpected_entries = {
        key for key, value in generated_entries.items()
        if authority["oracle_entries"].get(key) != value and not target(key)
    }
    if unexpected_entries:
        raise SystemExit(f"unrelated oracle entry drift: {sorted(unexpected_entries)}")

    authority["dispatch_rows"] = generated_rows
    for key, value in generated_entries.items():
        if target(key):
            authority["oracle_entries"][key] = value
    authority["semantic_counts"] = semantic_counts(registry, generated_entries)
    authority["authority_sha256"] = rehash(authority, "authority_sha256")
    write(authority_path, authority)


def main() -> None:
    refreeze_targeted()
    print(json.dumps({"targeted_refreeze": [TARGET_TEST_ID]}, sort_keys=True))


if __name__ == "__main__":
    main()
