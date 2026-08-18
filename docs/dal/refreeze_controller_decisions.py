#!/usr/bin/env python3
"""Targeted authority re-freeze for the four controller decision functions.

Covers DAL-T-PLAN-XFIELD-001 / DAL-T-DISPOSITION-001 / DAL-T-OPENSET-001 /
DAL-T-FIXDIFF-001 (freeze pack `DAL021-024_合同冻结包_v0.1.md` §4–§6).  The
script re-derives every variant's legal outcome from the contract rules
independently of the generator's own classification, proves that no unrelated
authority row differs from the generated expectation, then splices only the
four operation specs and their thirty-four entries/rows into the two static
authorities.  It deliberately cannot rebuild an entire authority.
"""

from __future__ import annotations

import hashlib
import json
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFESTS = ROOT / "manifests"
sys.path.insert(0, str(ROOT))

from dal_jcs import canonical_bytes  # noqa: E402
from verify_operation_oracle_authority import (  # noqa: E402
    expected_entries,
    expected_specs,
    semantic_counts as operation_counts,
)
from verify_test_manifest_authority import (  # noqa: E402
    expected_manifest_rows,
    semantic_counts as manifest_counts,
)


TARGET_TEST_IDS = frozenset(
    {
        "DAL-T-PLAN-XFIELD-001", "DAL-T-DISPOSITION-001",
        "DAL-T-OPENSET-001", "DAL-T-FIXDIFF-001",
    }
)
TARGET_OPERATION_IDS = frozenset(
    {
        "OP-PLAN-XFIELD-001", "OP-DISPOSITION-001",
        "OP-OPENSET-001", "OP-FIXDIFF-001",
    }
)
BASE_WRITES = ["aggregate", "business_event", "transition_receipt", "audit"]
BLOCK_WRITES = BASE_WRITES + [
    "decision_create", "decision_projection", "notification_outbox",
]
FIX_EVENTS = ["fix.requested"]
BLOCK_EVENTS = ["feature.blocked"]
APPLIED_RECEIPT = [
    {"code": "APPLIED", "count": 1, "schema_version": "dal.transition-receipt/1.0"}
]


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
    return digest({key: item for key, item in value.items() if key != field})


def _target_entry(key: str) -> bool:
    return any(f"/{test_id}/" in key for test_id in TARGET_TEST_IDS)


def _target_manifest_row(key: str) -> bool:
    return any(key.startswith(f"{test_id}/") for test_id in TARGET_TEST_IDS)


def _diff_parts(diff_text: str) -> tuple[list[str], list[str]]:
    """Return (deleted_lines, added_lines) of a single-hunk unified diff."""
    deleted: list[str] = []
    added: list[str] = []
    for line in diff_text.split("\n")[1:]:
        if line.startswith("-"):
            deleted.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return deleted, added


def _inputs(fixtures: dict, test_id: str, variant: str) -> tuple[dict, list[dict]]:
    fixture = fixtures[f"dal.fixture/{test_id}/{variant}/G3/1.0"]
    payload = fixture["operation_sequence"][0]["input"]
    return payload["authoritative_facts"], payload["injected_results"]


PLAN_EXPECTED_RULES = {
    "identity_mismatch": {"identity_mismatch"},
    "paths_overlap_file_in_dir": {"paths_overlap"},
    "paths_overlap_dir_in_dir": {"paths_overlap"},
    "paths_overlap_equal": {"paths_overlap"},
    "order_gap": {"order_gap"},
    "dependency_not_earlier": {"dependency_not_earlier"},
    "unknown_verification": {"unknown_verification"},
    "digest_drift": {"digest_drift"},
}


def verify_plan_cross_fields(fixtures: dict, oracles: dict) -> None:
    """§4: re-derive every cross-field rule from the plan structure itself."""
    for variant in [
        "plan_complete", "identity_mismatch", "paths_overlap_file_in_dir",
        "paths_overlap_dir_in_dir", "paths_overlap_equal", "order_gap",
        "dependency_not_earlier", "unknown_verification", "digest_drift",
    ]:
        facts, results = _inputs(fixtures, "DAL-T-PLAN-XFIELD-001", variant)
        plan = results[0]["plan"]
        tasks = sorted(plan["tasks"], key=lambda item: item["order"])
        violations: set[str] = set()
        manifest = facts["input_manifest"]
        if plan["feature_id"] != manifest["feature_id"] or plan["base_sha"] != manifest["base_sha"]:
            violations.add("identity_mismatch")
        paths = [
            entry
            for task in tasks
            for entry in task["allowed_paths"]
        ]
        for left, right in combinations(paths, 2):
            if left["path"] == right["path"]:
                violations.add("paths_overlap")
            for dir_entry, other in ((left, right), (right, left)):
                if dir_entry["path_type"] == "directory" and other["path"].startswith(dir_entry["path"] + "/"):
                    violations.add("paths_overlap")
        if [task["order"] for task in tasks] != list(range(1, len(tasks) + 1)):
            violations.add("order_gap")
        order_by_id = {task["task_id"]: task["order"] for task in tasks}
        for task in tasks:
            for dep in task["dependency_task_ids"]:
                if order_by_id.get(dep, 10**9) >= task["order"]:
                    violations.add("dependency_not_earlier")
        registry_ids = {rule["verification_id"] for rule in facts["repo_rules_registry"]}
        if any(
            vid not in registry_ids
            for criterion in plan["acceptance_criteria"]
            for vid in criterion["verification_ids"]
        ):
            violations.add("unknown_verification")
        if plan["allowed_paths_sha256"] != digest(paths):
            violations.add("digest_drift")
        # Each failure variant must isolate exactly one rule; the happy path none.
        if variant == "plan_complete":
            if violations:
                raise SystemExit(f"plan_complete is not clean: {sorted(violations)}")
            outcome = "verified"
        else:
            if violations != PLAN_EXPECTED_RULES[variant]:
                raise SystemExit(
                    f"PLAN-XFIELD/{variant}: violations {sorted(violations)} != expected {sorted(PLAN_EXPECTED_RULES[variant])}"
                )
            outcome = "failure"
        _check_oracle(oracles, "DAL-T-PLAN-XFIELD-001", variant, outcome, "planning")


def verify_disposition(fixtures: dict, oracles: dict) -> None:
    """§5: disposition is a pure function of coverage, findings, gaps, pins."""
    for variant in [
        "approve_clean", "coverage_incomplete", "provider_approve_with_findings",
        "provider_request_changes_clean", "request_changes_findings",
        "request_changes_gaps",
    ]:
        facts, results = _inputs(fixtures, "DAL-T-DISPOSITION-001", variant)
        review = results[0]
        covered = {entry["acceptance_id"] for entry in review["coverage"]}
        coverage_ok = covered == set(facts["plan_acceptance_ids"])
        recomputed = facts["recomputed_review_inputs"]
        pins_ok = (
            review["reviewed_input_manifest_sha256"] == recomputed["input_manifest_sha256"]
            and review["reviewed_diff_base_sha"] == recomputed["diff_base_sha"]
            and review["reviewed_result_sha"] == recomputed["result_sha"]
        )
        if review["disposition"] == "approve":
            outcome = "verified" if coverage_ok and pins_ok and not review["findings"] else "failure"
        else:
            named = bool(review["findings"]) or bool(review["acceptance_gaps"])
            outcome = "fixing" if coverage_ok and pins_ok and named else "failure"
        _check_oracle(oracles, "DAL-T-DISPOSITION-001", variant, outcome, "reviewing")


def verify_open_set(fixtures: dict, oracles: dict) -> None:
    """§6: the carried open set must be exactly resolved before 'verified'.

    Round 1 (empty chain) resolves the original review's findings; from round
    2 on, the required set is the chain's accumulated new findings.  New
    finding IDs may not reuse any ID the feature has already seen.
    """
    for variant in [
        "init_from_review", "carry_forward_exact", "carried_finding_omitted",
        "carried_finding_renamed", "new_finding_id_reused",
        "verified_with_new_findings", "remaining_declared",
    ]:
        facts, results = _inputs(fixtures, "DAL-T-OPENSET-001", variant)
        git_executor, reviewer = results
        verdict = reviewer["verdict"]
        if verdict["result_sha"] != facts["recomputed_result_sha"]:
            raise SystemExit(f"OPENSET/{variant}: verdict not bound to recomputed result")
        if (
            git_executor["anchor_tree_entry"] != {"mode": "100644", "type": "blob", "present": True}
            or not git_executor["previous_tree_entry"]["present"]
            or not git_executor["current_tree_entry"]["present"]
        ):
            raise SystemExit(f"OPENSET/{variant}: anchor tree entries not well-formed")
        original_ids = set(facts["original_review"]["finding_ids"])
        chain_findings = [
            item
            for entry in facts["prior_verdict_chain"]
            for item in entry["new_findings"]
        ]
        chain_ids = {item["finding_id"] for item in chain_findings}
        open_ids = chain_ids if chain_ids else original_ids
        resolutions = verdict["finding_resolutions"]
        closed = {item["finding_id"] for item in resolutions if item["status"] == "closed"}
        remaining = {item["finding_id"] for item in resolutions if item["status"] == "remaining"}
        unknown = {item["finding_id"] for item in resolutions} - open_ids
        new_ids = {item["finding_id"] for item in verdict["new_findings"]}
        collisions = new_ids & (original_ids | chain_ids)
        unresolved = open_ids - closed - remaining
        # A carried finding's line must actually be deleted by this round's increment.
        deleted, _ = _diff_parts(git_executor["increment_diff"])
        for finding in chain_findings:
            if f"old{finding['location']['line_start']}" not in deleted:
                raise SystemExit(f"OPENSET/{variant}: increment does not delete carried line")
        if verdict["verdict"] == "verified":
            outcome = (
                "verified"
                if not unresolved and not unknown and not collisions and not verdict["new_findings"]
                else "failure"
            )
        elif verdict["verdict"] == "changes_requested":
            outcome = (
                "fixing"
                if (remaining or verdict["new_findings"]) and not unknown and not collisions
                else "failure"
            )
        else:
            outcome = "failure"
        _check_oracle(oracles, "DAL-T-OPENSET-001", variant, outcome, "reviewing")


FIXDIFF_EXPECTED_RULES = {
    "verified_clean": set(),
    "evidence_role_violation": {"evidence_role"},
    "anchor_entry_not_blob": {"anchor_entry_not_blob"},
    "path_died_between_rounds": {"path_died_between_rounds"},
    "surviving_set_empty": {"surviving_set_empty"},
    "increment_missed_surviving_lines": {"increment_deletion"},
    "no_deletion_in_increment": {"increment_deletion"},
    "gap_closed_by_fix_diff_only": {"gap_evidence"},
    "gap_closed_by_test_receipts": set(),
    "new_finding_anchor_mismatch": {"new_finding_anchor"},
    "verified_with_unverified_acceptance": {"acceptance"},
    "changes_requested_declared": set(),
}


def verify_post_fix_verdict(fixtures: dict, oracles: dict) -> None:
    """§6 layer 2: anchors, surviving-set/increment algebra, evidence roles."""
    for variant in FIXDIFF_EXPECTED_RULES:
        facts, results = _inputs(fixtures, "DAL-T-FIXDIFF-001", variant)
        git_executor, reviewer = results
        verdict = reviewer["verdict"]
        roles = facts["manifest_roles"]
        anchors = facts["round_anchors"]
        if (
            anchors["anchor_sha"]
            != facts["original_review"]["finding_locations"]["F-1"]["anchor_sha"]
            or anchors["previous_result_sha"] != facts["prior_verdict_chain"][0]["result_sha"]
        ):
            raise SystemExit(f"FIXDIFF/{variant}: round anchors not bound to prior rounds")
        structural: set[str] = set()
        if git_executor["anchor_tree_entry"]["type"] != "blob" or not git_executor["anchor_tree_entry"]["present"]:
            structural.add("anchor_entry_not_blob")
        if not git_executor["previous_tree_entry"]["present"]:
            structural.add("path_died_between_rounds")
        _, surviving = _diff_parts(git_executor["anchor_translation_diff"])
        increment_deleted, _ = _diff_parts(git_executor["increment_diff"])
        has_closed = any(item["status"] == "closed" for item in verdict["finding_resolutions"])
        if has_closed and not surviving:
            structural.add("surviving_set_empty")
        if has_closed and any(line not in increment_deleted for line in surviving):
            structural.add("increment_deletion")
        for item in verdict["finding_resolutions"]:
            if item["status"] == "closed" and roles.get(item["evidence_sha256"][0]) != "fix_diff":
                structural.add("evidence_role")
        for item in verdict["acceptance_gap_resolutions"]:
            evidence = item["evidence_sha256"][0]
            receipt = facts["test_receipts"].get(evidence)
            if (
                roles.get(evidence) != "test_receipts"
                or receipt is None
                or receipt["verification_id"] not in facts["plan_verification_ids"].get(item["acceptance_id"], [])
            ):
                structural.add("gap_evidence")
        for finding in verdict["new_findings"]:
            if finding["location"]["anchor_sha"] != anchors["anchor_sha"]:
                structural.add("new_finding_anchor")
        if not verdict["acceptance_verified"]:
            structural.add("acceptance")
        if structural != FIXDIFF_EXPECTED_RULES[variant]:
            raise SystemExit(
                f"FIXDIFF/{variant}: structural {sorted(structural)} != expected {sorted(FIXDIFF_EXPECTED_RULES[variant])}"
            )
        if verdict["verdict"] == "verified":
            outcome = "verified" if not structural else "failure"
        else:
            has_remaining = any(item["status"] == "remaining" for item in verdict["finding_resolutions"])
            outcome = "fixing" if has_remaining and not structural else "failure"
        _check_oracle(oracles, "DAL-T-FIXDIFF-001", variant, outcome, "reviewing")


def _check_oracle(oracles: dict, test_id: str, variant: str, outcome: str, pre_state_name: str) -> None:
    key = f"dal.oracle/{test_id}/{variant}/G3/1.0"
    oracle = oracles[key]
    if outcome == "failure":
        writes, events, next_state = BLOCK_WRITES, BLOCK_EVENTS, "needs_human"
        reason = "PROVIDER_CONTRACT_FAILURE"
    elif outcome == "fixing":
        writes, events, next_state = BASE_WRITES, FIX_EVENTS, "fixing"
        reason = None
    else:
        writes = BASE_WRITES
        if test_id == "DAL-T-PLAN-XFIELD-001":
            events, next_state = ["plan.ready"], "awaiting_plan_review"
        else:
            events, next_state = ["review.completed"], "verified"
        reason = None
    expected_snapshot = {
        "entity_type": "feature", "reason_code": reason,
        "reason_owner": "feature" if reason else None, "state": next_state,
    }
    if oracle["allowed_write_set"] != writes:
        raise SystemExit(f"{key}: write set does not match outcome {outcome}")
    if oracle["expected_event_trace"] != events:
        raise SystemExit(f"{key}: event trace does not match outcome {outcome}")
    if oracle["expected_receipts"] != APPLIED_RECEIPT:
        raise SystemExit(f"{key}: receipt semantics drifted")
    if oracle["expected_state_trace"] != [pre_state_name, next_state]:
        raise SystemExit(f"{key}: state trace does not match outcome {outcome}")
    if oracle["expected_final_snapshot"] != expected_snapshot:
        raise SystemExit(f"{key}: final snapshot does not match outcome {outcome}")
    if (
        oracle["expected_external_effect_trace"]
        or oracle["expected_related_snapshots"]
        or oracle["expected_atomic_companion_transitions"]
        or oracle["scenario_assertions"]
    ):
        raise SystemExit(f"{key}: unexpected non-empty companion expectations")
    if oracle["forbidden_side_effects"] != ["unapproved_external_effect", "production_access"]:
        raise SystemExit(f"{key}: forbidden side effects drifted")


def refreeze_targeted() -> None:
    operation = load(MANIFESTS / "operation-spec-registry_v1.0.json")
    evidence = load(MANIFESTS / "evidence-schema-registry_v1.0.json")
    guard = load(MANIFESTS / "guard-predicate-registry_v1.0.json")
    manifest = load(MANIFESTS / "test-manifest_v1.2.json")
    fixtures = load(MANIFESTS / "test-fixtures_v1.0.json")
    oracles = load(MANIFESTS / "test-oracles_v1.0.json")
    verify_plan_cross_fields(fixtures["fixtures"], oracles["oracles"])
    verify_disposition(fixtures["fixtures"], oracles["oracles"])
    verify_open_set(fixtures["fixtures"], oracles["oracles"])
    verify_post_fix_verdict(fixtures["fixtures"], oracles["oracles"])

    operation_path = MANIFESTS / "operation-oracle-authority_v1.0.json"
    operation_authority = load(operation_path)
    generated_specs = expected_specs(operation)
    generated_entries = expected_entries(operation, manifest, fixtures, oracles)
    unexpected_specs = {
        key for key, value in generated_specs.items()
        if operation_authority["operation_specs"].get(key) != value and key not in TARGET_OPERATION_IDS
    }
    unexpected_entries = {
        key for key, value in generated_entries.items()
        if operation_authority["entries"].get(key) != value and not _target_entry(key)
    }
    if unexpected_specs or unexpected_entries:
        raise SystemExit(
            f"unrelated operation authority drift: specs={sorted(unexpected_specs)}, entries={sorted(unexpected_entries)}"
        )
    for key in TARGET_OPERATION_IDS:
        operation_authority["operation_specs"][key] = generated_specs[key]
    for key, value in generated_entries.items():
        if _target_entry(key):
            operation_authority["entries"][key] = value
    operation_authority["frozen_operation_registry_sha256"] = operation["registry_sha256"]
    operation_authority["frozen_evidence_registry_sha256"] = evidence["registry_sha256"]
    operation_authority["frozen_guard_registry_sha256"] = guard["registry_sha256"]
    operation_authority["semantic_counts"] = operation_counts(
        operation, operation_authority["entries"]
    )
    operation_authority["authority_sha256"] = rehash(
        operation_authority, "authority_sha256"
    )
    write(operation_path, operation_authority)

    manifest_path = MANIFESTS / "test-manifest-authority_v1.0.json"
    manifest_authority = load(manifest_path)
    generated_rows = expected_manifest_rows(manifest, fixtures, oracles)
    unexpected_rows = {
        key for key, value in generated_rows.items()
        if manifest_authority["manifest_rows"].get(key) != value and not _target_manifest_row(key)
    }
    if unexpected_rows:
        raise SystemExit(f"unrelated manifest authority drift: {sorted(unexpected_rows)}")
    for key, value in generated_rows.items():
        if _target_manifest_row(key):
            manifest_authority["manifest_rows"][key] = value
    manifest_authority["frozen_manifest_sha256"] = manifest["manifest_sha256"]
    manifest_authority["semantic_counts"] = manifest_counts(
        manifest_authority["manifest_rows"]
    )
    manifest_authority["authority_sha256"] = rehash(
        manifest_authority, "authority_sha256"
    )
    write(manifest_path, manifest_authority)


def main() -> None:
    refreeze_targeted()
    print(json.dumps({"targeted_refreeze": sorted(TARGET_TEST_IDS)}, sort_keys=True))


if __name__ == "__main__":
    main()
