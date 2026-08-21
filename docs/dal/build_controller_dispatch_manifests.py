#!/usr/bin/env python3
"""Build the controller-dispatch machine contract (freeze pack §9).

Machine counterpart of `DAL-Controller编排冻结包_v0.1.md` §2–§6.  It emits the
closed `dal.controller-dispatch/1.0` JSON Schema, the dispatch-table registry
rows, and the fifteen `DAL-T-GRAPH-001` graph-level adversarial oracles plus
their fixtures and manifest rows.  It is documentation tooling only: it does
not import or execute DAL runtime, GitHub, Worker, provider, or Personal Agent
code, and it never touches credentials.

The static authority that freezes the semantics of these files lives in
`manifests/controller-dispatch-authority_v1.0.json` and is verified by
`verify_controller_dispatch_authority.py`; `refreeze_controller_dispatch.py`
re-derives each variant independently and splices a targeted change, following
the `refreeze_controller_decisions.py` (Task #11) pattern.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dal_jcs import canonical_bytes

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "manifests"

# The frozen transition registry this contract builds on (§4.3 two-layer
# relationship).  Pinned so any drift in the machine truth is detected.
_TRANSITION_REGISTRY = json.loads(
    (OUT / "transition-spec-registry_v1.0.json").read_text(encoding="utf-8")
)
TRANSITION_REGISTRY_SHA256 = _TRANSITION_REGISTRY["registry_sha256"]
# The 47 real command_type names — the authoritative vocabulary.  A command the
# graph emits that is not one of these is a fabricated transition (§7).
COMMAND_TYPES = frozenset(spec["command_type"] for spec in _TRANSITION_REGISTRY["specs"])

BASE_WRITES = ["aggregate", "business_event", "transition_receipt", "audit"]
FULL_WRITES = BASE_WRITES + [
    "decision_create", "decision_projection", "notification_outbox",
]
APPLIED_RECEIPT = [
    {"code": "APPLIED", "count": 1, "schema_version": "dal.transition-receipt/1.0"}
]
BLOCK_EVENT = ["feature.blocked"]
FORBIDDEN_BASE = ["unapproved_external_effect", "production_access"]

# ---------------------------------------------------------------------------
# §3 node taxonomy (closed enum, dal.controller-node/1.0)
# ---------------------------------------------------------------------------
NODE_TAXONOMY = {
    "provider": {
        "definition": "该状态需要一次受控 provider 调用",
        "orchestration": "adapter(seam) -> consume_provider_stream -> 节点业务 handler(s) -> 迁移",
    },
    "deterministic": {
        "definition": "该状态只用 controller 已持有的确定性事实自行判定并立即迁移",
        "orchestration": "无 provider、不等待；跑 gate（测试 receipt / approval 有效性 / SHA / hash 绑定）-> 迁移",
    },
    "gate": {
        "definition": "该状态无 provider、无确定性判定，必须等待人工 decision 或新到达的外部事实",
        "orchestration": "只接受 action-bound approval / 外部 read-back（GitHub / 生产）-> 迁移",
    },
    "terminal": {
        "definition": "终态",
        "orchestration": "无出边、无编排动作；任何迁移请求 fail closed",
    },
}

# §6 adapter seam (closed enum, dal.controller-adapter-seam/1.0)
ADAPTER_SEAM = {
    "injected": {
        "definition": "以 normalized 事件流（dal.codex-adapter-response/1.0）驱动 provider 节点",
        "constraint": "MUST NOT 派生 subprocess、MUST NOT 触碰网络/凭据",
    },
    "subprocess": {
        "definition": "派生 codex exec 子进程、固定 cwd/--ephemeral/--sandbox read-only/max_tool_calls=0",
        "constraint": "MUST 绑定有效 dal.provider-preflight-result/1.0（DAL006 §5）；无 receipt 不得派生",
    },
}

# ---------------------------------------------------------------------------
# §4.1 per-state main routes (23 rows).  resulting_commands 逐字对齐
# transition-spec-registry_v1.0.json 的真实 command_type。
# to_state 为 null + to_family 表示「唯一目标族在生成期展开」（checkpoint /
# reason 绑定的 merged|deployed 等），精确目标不在调度表内裁决。
# ---------------------------------------------------------------------------
DISPATCH_ROWS = [
    ("intake", "deterministic", "validate_feature_shape",
     [("start_plan", "planning", None)]),
    ("planning", "provider", "planning_compose",
     [("record_plan", "awaiting_plan_review", None)]),
    ("awaiting_plan_review", "gate", "human_plan_approval",
     [("approve_plan", "approved", None), ("request_revision", "planning", None)]),
    ("approved", "deterministic", "approval_validity_and_sha_binding",
     [("start_provider", "coding", None)]),
    ("coding", "provider", "coding_compose_forward_ref",
     [("record_provider_result", "verifying", None)]),
    ("verifying", "deterministic", "test_receipt_gate",
     [("record_verification/pass", "reviewing", None),
      ("record_verification/fixable_fail", "fixing", None),
      ("record_verification/blocked", "blocked_test", None)]),
    ("reviewing", "provider", "reviewing_compose",
     [("record_review/pass", "verified", None), ("record_review/findings", "fixing", None)]),
    ("fixing", "provider", "fixing_compose_forward_ref",
     [("record_fix_result", "verifying", None),
      ("block_feature", "needs_human", "REVIEW_LOOP_LIMIT")]),
    ("verified", "deterministic", "merge_candidate_sha_binding",
     [("record_merge_candidate", "awaiting_merge", None)]),
    ("awaiting_merge", "gate", "human_merge_and_github_readback",
     [("record_merge_approval", "awaiting_merge", None),
      ("record_observed_merge", "merged", None),
      ("record_unapproved_observed_merge", "merged", None),
      ("start_managed_merge", "awaiting_merge", None),
      ("record_managed_merge", "merged", None)]),
    ("merged", "gate", "deploy_scope_and_approval",
     [("complete_without_deploy", "completed", None),
      ("record_deploy_approval", "merged", None),
      ("start_managed_deploy", "merged", None),
      ("record_observed_deployment", "deployed", None),
      ("record_deployment", "deployed", None),
      ("record_unapproved_observed_deployment", "deployed", None)]),
    ("deployed", "gate", "production_verification_readback",
     [("complete_after_deploy", "completed", None)]),
    ("completed", "terminal", None, []),
    ("blocked_requirement", "gate", "human_supply_requirement",
     [("supply_requirement", "planning", None)]),
    ("blocked_usage", "gate", "frozen_route_resume_or_fallback",
     [("resume_frozen_route", None, "checkpoint"), ("start_approved_fallback", None, "checkpoint")]),
    ("blocked_auth", "gate", "human_auth_and_probe",
     [("resume_after_auth", None, "checkpoint")]),
    ("blocked_test", "gate", "human_takeover_or_continue_fix",
     [("continue_fix", "fixing", None)]),
    ("blocked_external_prerequisite", "gate", "prerequisite_complete_and_preflight",
     [("resume_after_prerequisite", None, "checkpoint")]),
    ("blocked_unknown", "gate", "effect_inventory",
     [("resume_checkpoint", None, "checkpoint"),
      ("require_reconciliation", "reconciliation_required", None)]),
    ("reconciliation_required", "gate", "authoritative_readback_and_approval",
     [("resume_checkpoint", None, "checkpoint"),
      ("accept_merge_result", "merged", None),
      ("accept_deploy_result", "deployed", None),
      ("open_recovery", "needs_human", None)]),
    ("needs_human", "gate", "action_bound_approval_by_reason",
     [("retry_from_checkpoint", None, "checkpoint"),
      ("replan", "planning", None),
      ("resume_with_budget", None, "checkpoint"),
      ("continue_fix", "fixing", None),
      ("accept_current_fact", None, "merged|deployed"),
      ("open_recovery_case", "needs_human", None),
      ("complete_verified_recovery", None, "merged|deployed")]),
    ("paused", "gate", "resume_and_drift_probe",
     [("resume_checkpoint", None, "checkpoint")]),
    ("cancelled", "terminal", None, []),
]

# §4.2 universal fail-closed commands (from_state 集权威在 §2.3.1 / registry)
UNIVERSAL_ROUTES = [
    ("pause_feature", "USER_PAUSE", "paused",
     ["planning", "awaiting_plan_review", "approved", "coding", "verifying",
      "reviewing", "fixing", "verified", "awaiting_merge"]),
    ("cancel_feature", "CANCEL", "cancelled",
     ["intake", "planning", "awaiting_plan_review", "approved", "coding",
      "verifying", "reviewing", "fixing", "verified", "awaiting_merge",
      "blocked_requirement", "blocked_usage", "blocked_auth", "blocked_test",
      "blocked_external_prerequisite", "blocked_unknown", "needs_human", "paused"]),
    ("block_unknown", "UNKNOWN_NO_EXTERNAL_INTENT", "blocked_unknown",
     ["intake", "planning", "awaiting_plan_review", "approved", "coding",
      "verifying", "reviewing", "fixing", "verified", "awaiting_merge",
      "merged", "deployed"]),
    ("require_reconciliation", "EXTERNAL_RESULT_UNKNOWN", "reconciliation_required",
     ["intake", "planning", "awaiting_plan_review", "approved", "coding",
      "verifying", "reviewing", "fixing", "verified", "awaiting_merge",
      "merged", "deployed", "blocked_unknown"]),
]

# §4.2 block_feature reason family (reason -> from_state 集 -> 唯一目标)
BLOCK_FEATURE_ROUTES = [
    ("REQUIREMENT_MISSING", ["planning"], "blocked_requirement"),
    ("USAGE_LIMIT", ["planning", "coding", "reviewing", "fixing"], "blocked_usage"),
    ("AUTH_REQUIRED", ["planning", "coding", "reviewing", "fixing"], "blocked_auth"),
    ("EXTERNAL_PREREQUISITE",
     ["planning", "approved", "coding", "verifying", "reviewing", "fixing"],
     "blocked_external_prerequisite"),
    ("TEST_BLOCKED", ["fixing"], "blocked_test"),
    ("REVIEW_LOOP_LIMIT", ["fixing"], "needs_human"),
    ("TRANSIENT_RETRY_EXHAUSTED", ["planning", "coding", "reviewing", "fixing"], "needs_human"),
    ("PROVIDER_CONTRACT_FAILURE", ["planning", "coding", "reviewing", "fixing"], "needs_human"),
    ("BUDGET_LIMIT", ["planning", "coding", "reviewing", "fixing"], "needs_human"),
    ("POLICY_FAILURE",
     ["planning", "awaiting_plan_review", "approved", "coding", "verifying",
      "reviewing", "fixing", "verified", "awaiting_merge"], "needs_human"),
    ("GIT_CONFLICT",
     ["approved", "coding", "verifying", "reviewing", "fixing", "verified",
      "awaiting_merge"], "needs_human"),
    ("STATE_DRIFT",
     ["awaiting_plan_review", "approved", "coding", "verifying", "reviewing",
      "fixing", "verified", "awaiting_merge", "paused"], "needs_human"),
    ("POST_EFFECT_EXCEPTION", ["merged", "deployed"], "needs_human"),
]

# The 23 closed states (derived from DISPATCH_ROWS).
STATES = [row[0] for row in DISPATCH_ROWS]
assert len(STATES) == 23 and len(set(STATES)) == 23

NODE_BY_STATE = {row[0]: row[1] for row in DISPATCH_ROWS}


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# ---------------------------------------------------------------------------
# §5/§6 dispatch derivation.  Given a state and the controller-held facts plus
# the injected provider stream, derive the deterministic dispatch decision and
# the resulting transition.  This is the generator's own encoding of the frozen
# rules; `refreeze_controller_dispatch.py` re-derives independently.
# ---------------------------------------------------------------------------
def dispatch_decision(state: str, facts: dict, seam: str, stream: list | None,
                      attempted_command: str | None, provider_attempted: bool) -> dict:
    node_type = NODE_BY_STATE[state]

    if node_type == "terminal":
        return {
            "node_type": "terminal", "orchestration_action": None, "handler_sequence": [],
            "round": None, "resulting_command": None, "seam": seam,
            "outcome": "fail_closed_terminal",
        }

    # A command the graph emits that is not one of the 47 real command_types is
    # a fabricated transition (§7): the TransitionSpec registry rejects it with
    # ILLEGAL_TRANSITION before any node logic runs.
    if attempted_command is not None and attempted_command not in COMMAND_TYPES:
        return {
            "node_type": node_type, "orchestration_action": None, "handler_sequence": [],
            "round": None, "resulting_command": None, "seam": seam,
            "outcome": "illegal_transition",
        }

    if node_type == "deterministic":
        if provider_attempted:
            return {
                "node_type": "deterministic",
                "orchestration_action": "deterministic_test_receipt_gate",
                "handler_sequence": [], "round": None, "resulting_command": None,
                "seam": seam, "outcome": "deterministic_provider_rejected",
            }
        # The main-route command for a deterministic state is its only legal
        # deterministic migration; a fabricated command is illegal.
        row = DISPATCH_ROWS[STATES.index(state)]
        commands = [c for c in row[3]]
        legal = {c[0] for c in commands}
        if attempted_command is not None and attempted_command not in legal:
            return {
                "node_type": "deterministic", "orchestration_action": row[2],
                "handler_sequence": [], "round": None, "resulting_command": None,
                "seam": seam, "outcome": "illegal_transition",
            }
        return {
            "node_type": "deterministic", "orchestration_action": row[2],
            "handler_sequence": [], "round": None,
            "resulting_command": commands[0][0] if commands else None,
            "seam": seam, "outcome": "deterministic_migrate",
        }

    # provider node ---------------------------------------------------------
    if state == "planning":
        action, handlers = "planning_compose", ["consume_provider_stream", "plan_cross_fields"]
    elif state == "reviewing":
        if facts["has_findings_receipt_since_plan"]:
            action, handlers = "reviewing_compose_round_2", \
                ["consume_provider_stream", "post_fix_verdict", "open_finding_set"]
            rnd = 2
        else:
            action, handlers = "reviewing_compose_round_1", \
                ["consume_provider_stream", "review_independence", "review_disposition"]
            rnd = 1
    else:  # coding / fixing (Wave 4 forward-ref, not refined here)
        action, handlers, rnd = "coding_or_fixing_forward_ref", [], None

    # cancelled is classified before any handler (§5 preface).
    if stream is not None and stream == ["cancelled"]:
        if facts["cancel_receipt_present"]:
            return {
                "node_type": "provider", "orchestration_action": "close_without_transition",
                "handler_sequence": [], "round": None, "resulting_command": None,
                "seam": seam, "outcome": "cancelled_close",
            }
        return {
            "node_type": "provider", "orchestration_action": action,
            "handler_sequence": handlers, "round": rnd if state == "reviewing" else None,
            "resulting_command": None, "seam": seam, "outcome": "cancelled_no_receipt",
        }

    # empty stream -> consume_provider_stream single-final gate fails (§7).
    if stream is not None and len(stream) == 0:
        return {
            "node_type": "provider", "orchestration_action": action,
            "handler_sequence": handlers, "round": rnd if state == "reviewing" else None,
            "resulting_command": None, "seam": seam, "outcome": "empty_response",
        }

    # subprocess seam without preflight (§6) is blocked before any provider call.
    if seam == "subprocess" and not facts["preflight_receipt_present"]:
        return {
            "node_type": "provider", "orchestration_action": action,
            "handler_sequence": [], "round": rnd if state == "reviewing" else None,
            "resulting_command": None, "seam": seam, "outcome": "subprocess_ungated",
        }

    # wrong route: reviewing handler combo on a non-reviewing provider state.
    if state == "planning" and attempted_command == "reviewing_handler_combo":
        return {
            "node_type": "provider", "orchestration_action": "fail_closed_unknown_tuple",
            "handler_sequence": [], "round": None, "resulting_command": None,
            "seam": seam, "outcome": "wrong_route",
        }

    # drift in the injected stream -> consume_provider_stream contract failure.
    if stream is not None and stream and stream[0].get("drift"):
        return {
            "node_type": "provider", "orchestration_action": action,
            "handler_sequence": handlers, "round": rnd if state == "reviewing" else None,
            "resulting_command": None, "seam": seam, "outcome": "drift",
        }

    # independence reuse on a round-1 review -> POLICY_DENIED before disposition.
    if state == "reviewing" and stream is not None and stream and stream[0].get("independence_reuse"):
        return {
            "node_type": "provider", "orchestration_action": "reviewing_compose_round_1",
            "handler_sequence": ["consume_provider_stream", "review_independence", "review_disposition"],
            "round": 1, "resulting_command": None, "seam": seam,
            "outcome": "independence_policy_denied",
        }

    # loop-limit guard fires in fixing before any coder derivation (§4.1/§5.3).
    if state == "fixing" and facts["review_fix_cycle_count"] >= 3:
        return {
            "node_type": "provider", "orchestration_action": "fixing_compose_forward_ref",
            "handler_sequence": [], "round": None, "resulting_command": "block_feature",
            "seam": seam, "outcome": "loop_limit",
        }

    # happy path: a normal stream produces the node's resulting command.
    resulting = None
    if state == "planning":
        resulting = "record_plan"
    elif state == "reviewing":
        disposition = stream[0].get("disposition", "approve") if stream else "approve"
        gaps = bool(stream[0].get("acceptance_gaps")) if stream else False
        if facts["has_findings_receipt_since_plan"]:
            verdict = stream[0].get("verdict", "verified") if stream else "verified"
            resulting = "record_review/pass" if verdict == "verified" else "record_review/findings"
        else:
            if disposition == "request_changes" or gaps:
                resulting = "record_review/findings"
            else:
                resulting = "record_review/pass"
    return {
        "node_type": "provider", "orchestration_action": action,
        "handler_sequence": handlers, "round": rnd if state == "reviewing" else None,
        "resulting_command": resulting, "seam": seam, "outcome": "provider_migrate",
    }


def transition_outcome(state: str, outcome: str, resulting: str) -> dict:
    """Map a dispatch decision to the transition that actually fires."""
    zero = {
        "state_trace": [state], "event_trace": [], "receipts": [],
        "writes": [], "reason": None, "final_state": state,
    }
    block = lambda reason, to, ev: {
        "state_trace": [state, to], "event_trace": [ev], "receipts": APPLIED_RECEIPT,
        "writes": FULL_WRITES, "reason": reason, "final_state": to,
    }
    if outcome in ("fail_closed_terminal", "deterministic_provider_rejected",
                   "illegal_transition", "cancelled_close", "wrong_route",
                   "independence_policy_denied"):
        return zero
    if outcome == "cancelled_no_receipt":
        return block("PROVIDER_CONTRACT_FAILURE", "needs_human", "feature.blocked")
    if outcome == "empty_response":
        return block("PROVIDER_CONTRACT_FAILURE", "needs_human", "feature.blocked")
    if outcome == "drift":
        return block("PROVIDER_CONTRACT_FAILURE", "needs_human", "feature.blocked")
    if outcome == "subprocess_ungated":
        return block("AUTH_REQUIRED", "blocked_auth", "feature.blocked")
    if outcome == "loop_limit":
        return block("REVIEW_LOOP_LIMIT", "needs_human", "feature.blocked")
    # provider_migrate
    if resulting == "record_plan":
        return {
            "state_trace": [state, "awaiting_plan_review"], "event_trace": ["plan.ready"],
            "receipts": APPLIED_RECEIPT, "writes": FULL_WRITES, "reason": None,
            "final_state": "awaiting_plan_review",
        }
    if resulting == "record_review/pass":
        return {
            "state_trace": [state, "verified"], "event_trace": ["review.completed"],
            "receipts": APPLIED_RECEIPT, "writes": BASE_WRITES, "reason": None,
            "final_state": "verified",
        }
    if resulting == "record_review/findings":
        return {
            "state_trace": [state, "fixing"], "event_trace": ["fix.requested"],
            "receipts": APPLIED_RECEIPT, "writes": BASE_WRITES, "reason": None,
            "final_state": "fixing",
        }
    raise SystemExit(f"unmapped resulting command {resulting}")


# ---------------------------------------------------------------------------
# §附: the 15 DAL-T-GRAPH-001 variants (inputs only — outcomes are derived).
# ---------------------------------------------------------------------------
def _facts(**overrides) -> dict:
    base = {
        "record_plan_receipt_present": True,
        "has_findings_receipt_since_plan": False,
        # Decoy context: findings receipts belonging to an earlier plan.  Round
        # determination keys off "since the current plan's record_plan" only, so
        # a non-zero prior count MUST NOT steer the dispatch (§5.2).
        "prior_findings_receipts": 0,
        "review_fix_cycle_count": 0,
        "cancel_receipt_present": False,
        "preflight_receipt_present": True,
    }
    base.update(overrides)
    return base


VARIANTS = [
    # name, state, seam, stream, facts, attempted_command, provider_attempted
    ("provider_node_empty_response", "planning", "injected", [], _facts(), None, False),
    ("provider_node_cancelled", "planning", "injected", ["cancelled"],
     _facts(cancel_receipt_present=True), None, False),
    ("provider_node_cancelled_no_receipt", "planning", "injected", ["cancelled"],
     _facts(cancel_receipt_present=False), None, False),
    ("provider_node_wrong_route", "planning", "injected", [{"type": "final", "content": "done"}],
     _facts(), "reviewing_handler_combo", False),
    ("handler_pass_fail_composition", "reviewing", "injected",
     [{"type": "final", "independence_reuse": True, "disposition": "approve"}],
     _facts(), None, False),
    ("deterministic_node_no_provider", "verifying", "injected", [{"type": "final"}],
     _facts(), None, True),
    ("transition_legality", "planning", "injected", [{"type": "final", "content": "done"}],
     _facts(), "fabricated_command", False),
    ("round_dispatch_first", "reviewing", "injected",
     [{"type": "final", "disposition": "approve"}], _facts(), None, False),
    ("round_dispatch_post_fix", "reviewing", "injected",
     [{"type": "final", "verdict": "verified"}],
     _facts(has_findings_receipt_since_plan=True), None, False),
    ("round_dispatch_gaps_only", "reviewing", "injected",
     [{"type": "final", "verdict": "changes_requested", "acceptance_gaps": [{"acceptance_id": "A-1"}]}],
     _facts(has_findings_receipt_since_plan=True), None, False),
    ("round_dispatch_after_replan", "reviewing", "injected",
     [{"type": "final", "disposition": "approve"}],
     _facts(record_plan_receipt_present=True, has_findings_receipt_since_plan=False,
            prior_findings_receipts=2),
     None, False),
    ("loop_limit_in_fixing", "fixing", "injected", [{"type": "final"}],
     _facts(review_fix_cycle_count=3), None, False),
    ("seam_injection_no_subprocess", "planning", "injected",
     [{"type": "final", "content": "done"}], _facts(), None, False),
    ("seam_subprocess_gated", "planning", "subprocess", [{"type": "final", "content": "done"}],
     _facts(preflight_receipt_present=False), None, False),
    ("drift_fail_closed", "planning", "injected",
     [{"type": "final", "content": "done", "drift": True}], _facts(), None, False),
]


def _variant_oracle(name: str, state: str, facts: dict, seam: str, stream: list | None,
                    attempted_command: str | None, provider_attempted: bool) -> dict:
    decision = dispatch_decision(state, facts, seam, stream, attempted_command, provider_attempted)
    tx = transition_outcome(state, decision["outcome"], decision["resulting_command"])
    forbidden = list(FORBIDDEN_BASE)
    if decision["outcome"] in ("deterministic_provider_rejected", "wrong_route",
                               "illegal_transition", "fail_closed_terminal"):
        forbidden.append("provider_call")
    if decision["outcome"] == "cancelled_close":
        forbidden.append("provider_handler_invocation")
    if decision["outcome"] == "subprocess_ungated":
        forbidden.append("subprocess_spawn")
    if seam == "injected":
        # §6: injected seam MUST NOT spawn any subprocess.
        forbidden.append("subprocess_spawn")
    if decision["outcome"] == "independence_policy_denied":
        forbidden.append("disposition_adoption")
    if decision["outcome"] == "loop_limit":
        forbidden.append("coder_derivation")
    # forbidden_side_effects is a set semantically; canonical sort makes the
    # oracle/authority comparison independent of clause order.
    forbidden = sorted(forbidden)
    coverage_ref = {
        "empty_response": "BLK-CONTRACT--planning",
        "cancelled_no_receipt": "BLK-CONTRACT--planning",
        "drift": "BLK-CONTRACT--planning",
        "subprocess_ungated": "BLK-AUTH--planning",
        "loop_limit": "BLK-LOOP--fixing",
        "cancelled_close": "SM-CANCEL--planning",
    }.get(decision["outcome"])
    return {
        "schema_version": "dal.controller-dispatch-oracle/1.0",
        "test_id": "DAL-T-GRAPH-001",
        "variant_id": name,
        "run_gate": "G3",
        "pre_state": {"entity_type": "feature", "state": state, "version": 7},
        "injection_operation": {
            "operation_id": name, "schema_version": "dal.test-injection/1.0",
            "injection_point": f"dal.inject/DAL-T-GRAPH-001/{name}/G3/1.0",
        },
        "expected_dispatch": {
            "node_type": decision["node_type"],
            "orchestration_action": decision["orchestration_action"],
            "handler_sequence": decision["handler_sequence"],
            "round": decision["round"],
            "resulting_command": decision["resulting_command"],
            "seam": seam,
        },
        "expected_state_trace": tx["state_trace"],
        "expected_event_trace": tx["event_trace"],
        "expected_receipts": tx["receipts"],
        "expected_external_effect_trace": [],
        "expected_final_snapshot": {
            "entity_type": "feature", "reason_code": tx["reason"],
            "reason_owner": "feature" if tx["reason"] else None, "state": tx["final_state"],
        },
        "expected_related_snapshots": [],
        "allowed_write_set": tx["writes"],
        "forbidden_side_effects": forbidden,
        "expected_atomic_companion_transitions": [],
        "scenario_assertions": [],
        "coverage_ref": coverage_ref,
    }


def _variant_fixture(name: str, state: str, facts: dict, seam: str, stream: list | None,
                     attempted_command: str | None, provider_attempted: bool) -> dict:
    return {
        "schema_version": "dal.controller-dispatch-fixture/1.0",
        "test_id": "DAL-T-GRAPH-001",
        "variant_id": name,
        "run_gate": "G3",
        "pre_state": {"entity_id": "fixture-entity", "entity_type": "feature",
                      "state": state, "version": 7},
        "controller_facts": facts,
        "seam": seam,
        "injected_provider_stream": stream,
        "attempted_resulting_command": attempted_command,
        "provider_attempted": provider_attempted,
        "transition_registry_sha256": TRANSITION_REGISTRY_SHA256,
    }


def _build_schema() -> dict:
    states_enum = STATES
    command_types = sorted({
        c[0] for row in DISPATCH_ROWS for c in row[3]
    } | {r[0] for r in UNIVERSAL_ROUTES} | {"block_feature"})
    node_types = sorted(NODE_TAXONOMY)
    return {
        "schema_version": "dal.controller-dispatch/1.0",
        "title": "controller 调度表闭包 schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "node_taxonomy", "adapter_seam", "dispatch_rows",
                     "universal_routes", "block_feature_routes"],
        "properties": {
            "schema_version": {"const": "dal.controller-dispatch/1.0"},
            "node_taxonomy": {
                "type": "object", "additionalProperties": False,
                "propertyNames": {"enum": node_types},
                "required": node_types,
            },
            "adapter_seam": {
                "type": "object", "additionalProperties": False,
                "propertyNames": {"enum": ["injected", "subprocess"]},
                "required": ["injected", "subprocess"],
            },
            "dispatch_rows": {
                "type": "array", "minItems": 23, "maxItems": 23,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["state", "node_type", "orchestration_action", "resulting_commands"],
                    "properties": {
                        "state": {"enum": states_enum},
                        "node_type": {"enum": node_types},
                        "orchestration_action": {"type": ["string", "null"]},
                        "resulting_commands": {
                            "type": "array",
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "required": ["command_type", "to_state", "to_family"],
                                "properties": {
                                    "command_type": {"enum": command_types},
                                    "to_state": {"type": ["string", "null"]},
                                    "to_family": {"type": ["string", "null"]},
                                },
                            },
                        },
                    },
                },
            },
            "universal_routes": {
                "type": "array", "minItems": 4, "maxItems": 4,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["command_type", "reason_family", "to_state", "from_states"],
                    "properties": {
                        "command_type": {"enum": command_types},
                        "reason_family": {"type": "string"},
                        "to_state": {"enum": states_enum},
                        "from_states": {"type": "array", "items": {"enum": states_enum}},
                    },
                },
            },
            "block_feature_routes": {
                "type": "array", "minItems": 13, "maxItems": 13,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["reason_code", "from_states", "to_state"],
                    "properties": {
                        "reason_code": {"type": "string"},
                        "from_states": {"type": "array", "items": {"enum": states_enum}},
                        "to_state": {"enum": states_enum},
                    },
                },
            },
        },
    }


def _build_registry() -> dict:
    def command_obj(command_type: str, to_state: str | None, to_family: str | None) -> dict:
        return {"command_type": command_type, "to_state": to_state, "to_family": to_family}

    rows = [
        {
            "state": state, "node_type": node_type, "orchestration_action": action,
            "resulting_commands": [
                command_obj(ct, ts, tf) for (ct, ts, tf) in commands
            ],
        }
        for (state, node_type, action, commands) in DISPATCH_ROWS
    ]
    value = {
        "schema_version": "dal.controller-dispatch-registry/1.0",
        "transition_registry_sha256": TRANSITION_REGISTRY_SHA256,
        "node_taxonomy": NODE_TAXONOMY,
        "adapter_seam": ADAPTER_SEAM,
        "dispatch_rows": rows,
        "universal_routes": [
            {"command_type": ct, "reason_family": rf, "to_state": to, "from_states": fs}
            for (ct, rf, to, fs) in UNIVERSAL_ROUTES
        ],
        "block_feature_routes": [
            {"reason_code": rc, "from_states": fs, "to_state": to}
            for (rc, fs, to) in BLOCK_FEATURE_ROUTES
        ],
    }
    value["registry_sha256"] = digest({k: v for k, v in value.items() if k != "registry_sha256"})
    return value


def main() -> None:
    schema = _build_schema()
    (OUT / "controller-dispatch_schema_v1.0.json").write_text(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    registry = _build_registry()
    (OUT / "controller-dispatch-registry_v1.0.json").write_text(
        json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    fixtures = {}
    oracles = {}
    manifest_rows = []
    for (name, state, seam, stream, facts, attempted, provider_attempted) in VARIANTS:
        fixtures[f"dal.controller-dispatch.fixture/DAL-T-GRAPH-001/{name}/G3/1.0"] = \
            _variant_fixture(name, state, facts, seam, stream, attempted, provider_attempted)
        oracles[f"dal.controller-dispatch.oracle/DAL-T-GRAPH-001/{name}/G3/1.0"] = \
            _variant_oracle(name, state, facts, seam, stream, attempted, provider_attempted)
        manifest_rows.append({
            "test_id": "DAL-T-GRAPH-001",
            "variant_id": name,
            "run_gate": "G3",
            "owner_tasks": ["DAL-011"],
            "fixture_ref": f"dal.controller-dispatch.fixture/DAL-T-GRAPH-001/{name}/G3/1.0",
            "oracle_id": f"dal.controller-dispatch.oracle/DAL-T-GRAPH-001/{name}/G3/1.0",
        })

    fixture_catalog = {
        "schema_version": "dal.controller-dispatch-fixture-catalog/1.0",
        "transition_registry_sha256": TRANSITION_REGISTRY_SHA256,
        "fixtures": fixtures,
    }
    fixture_catalog["catalog_sha256"] = digest(
        {k: v for k, v in fixture_catalog.items() if k != "catalog_sha256"})
    (OUT / "controller-dispatch-fixtures_v1.0.json").write_text(
        json.dumps(fixture_catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    oracle_catalog = {
        "schema_version": "dal.controller-dispatch-oracle-catalog/1.0",
        "transition_registry_sha256": TRANSITION_REGISTRY_SHA256,
        "oracles": oracles,
    }
    oracle_catalog["catalog_sha256"] = digest(
        {k: v for k, v in oracle_catalog.items() if k != "catalog_sha256"})
    (OUT / "controller-dispatch-oracles_v1.0.json").write_text(
        json.dumps(oracle_catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    manifest = {
        "schema_version": "dal.controller-dispatch-manifest/1.0",
        "manifest_version": "1.0",
        "registry_sha256": registry["registry_sha256"],
        "transition_registry_sha256": TRANSITION_REGISTRY_SHA256,
        "fixture_catalog_sha256": fixture_catalog["catalog_sha256"],
        "oracle_catalog_sha256": oracle_catalog["catalog_sha256"],
        "test_variants": manifest_rows,
    }
    manifest["manifest_sha256"] = digest(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"})
    (OUT / "controller-dispatch-manifest_v1.0.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "dispatch_rows": len(DISPATCH_ROWS),
        "universal_routes": len(UNIVERSAL_ROUTES),
        "block_feature_routes": len(BLOCK_FEATURE_ROUTES),
        "graph_variants": len(VARIANTS),
        "transition_registry_sha256": TRANSITION_REGISTRY_SHA256,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
