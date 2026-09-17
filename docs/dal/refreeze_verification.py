#!/usr/bin/env python3
"""Targeted authority re-freeze for the deterministic verification.

Follows `refreeze_coder_adapter.py`: it re-derives every
`DAL-T-VERIFICATION-CONTRACT-001` variant's classifier outcome from the fixture
facts and injected evidence, using a *separate* encoding of the frozen §6
precedence tree — not `build_verification_manifests.py`'s `classify()`.  It then
proves that no unrelated authority row differs from the generated expectation,
splices only the verification-oracle entries, and recomputes the authority
self-hash.

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
from verify_verification_authority import (  # noqa: E402
    expected_oracle_entries,
    semantic_counts,
)


TARGET_TEST_ID = "DAL-T-VERIFICATION-CONTRACT-001"

REGISTRY_STAGES = ("diff", "format", "lint", "build", "test")
CHECK_STAGES = ("format", "lint", "build", "test")
STAGE_RESULT_FIELDS = frozenset({"command", "exit_code"})

CONTRACT_VERSION = "dal.verification-report/1.0"
VERIFIED_WRITES = ["aggregate", "transition_receipt"]
BLOCK_WRITES = [
    "aggregate", "business_event", "transition_receipt", "audit",
    "decision_create", "decision_projection", "notification_outbox",
]
APPLIED_RECEIPT = [
    {"code": "APPLIED", "count": 1, "schema_version": "dal.transition-receipt/1.0"}
]

FAILURE_TO_REASON = {
    "task_failure": "TEST_BLOCKED",
    "contract_failure": "PROVIDER_CONTRACT_FAILURE",
    "policy_failure": "POLICY_FAILURE",
}
REASON_TO_STATE = {
    "TEST_BLOCKED": "blocked_test",
    "PROVIDER_CONTRACT_FAILURE": "needs_human",
    "POLICY_FAILURE": "needs_human",
}


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


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_argv(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(token, str) and token for token in value)
    )


# ---------------------------------------------------------------------------
# Independent re-derivation of the twelve variants.
# ---------------------------------------------------------------------------
def _jsonable(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {
            (
                key if isinstance(key, str) else f"<{type(key).__name__}:{key!r}>"
            ): _jsonable(val)
            for key, val in value.items()
        }
    return f"<{type(value).__name__}:{value!r}>"


def _report_hash(facts: dict, injected: dict) -> str:
    body = {
        "schema_version": CONTRACT_VERSION,
        "base_sha": facts["base_sha"],
        "diff_sha": injected["diff_sha"],
        "stage_results": _jsonable(injected["stage_results"]),
    }
    return digest(body)


def _classify(facts: dict, injected: dict) -> tuple:
    """(result_status, failure_class, reason_code) per the frozen §6 tree."""
    registry = facts["registry_commands"]
    stage_results = injected["stage_results"]
    reasons: list[str] = []
    swapped = False

    for stage in REGISTRY_STAGES:
        declared = registry[stage]
        observed = stage_results.get(stage)
        if observed is None:
            reasons.append(f"missing stage result: {stage}")
            continue
        if not isinstance(observed, dict) or frozenset(observed) != STAGE_RESULT_FIELDS:
            reasons.append(f"malformed stage result: {stage}")
            continue
        observed_command = observed.get("command")
        if not _is_argv(observed_command):
            reasons.append(f"malformed command for stage: {stage}")
            continue
        if list(observed_command) != list(declared):
            swapped = True
        exit_code = observed.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            reasons.append(f"malformed exit code for stage: {stage}")

    for stage in stage_results:
        if not isinstance(stage, str) or stage not in registry:
            reasons.append(f"undeclared stage: {stage!r}")

    if swapped:
        return ("failed", "policy_failure", "POLICY_FAILURE")
    if reasons:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")

    diff_stage = stage_results.get("diff")
    if isinstance(diff_stage, dict):
        diff_code = diff_stage.get("exit_code")
        if isinstance(diff_code, int) and not isinstance(diff_code, bool) and diff_code != 0:
            return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    if not injected["diff"]:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    if injected["diff_base_sha"] != facts["base_sha"]:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    if injected["diff_sha"] != _sha256_text(injected["diff"]):
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")

    for stage in CHECK_STAGES:
        if stage_results[stage]["exit_code"] != 0:
            return ("blocked", "task_failure", "TEST_BLOCKED")

    return ("succeeded", None, None)


def _coverage(result_status: str, failure_class: str | None) -> str:
    if result_status == "succeeded":
        return "VERIFY-SUCCEED--verifying"
    if failure_class == "policy_failure":
        return "BLK-POLICY--verifying"
    if failure_class == "contract_failure":
        return "BLK-CONTRACT--verifying"
    return "BLK-TASK--verifying"


def derive(fixture: dict) -> dict:
    command = fixture["operation_sequence"][0]
    facts = command["input"]["authoritative_facts"]
    injected = command["input"]["injected_results"]

    result_status, failure_class, reason = _classify(facts, injected)
    if result_status == "succeeded":
        final_state = "verified"
        state_trace = ["verifying", "verified"]
        event_trace: list = []
        writes = sorted(VERIFIED_WRITES)
        last_verified_sha = facts["base_sha"]
    else:
        final_state = REASON_TO_STATE[reason]
        state_trace = ["verifying", final_state]
        event_trace = ["feature.blocked"]
        writes = sorted(BLOCK_WRITES)
        last_verified_sha = facts["prior_last_verified_sha"]

    return {
        "result_status": result_status,
        "failure_class": failure_class,
        "last_verified_sha": last_verified_sha,
        "report_hash": _report_hash(facts, injected),
        "state_trace": state_trace,
        "event_trace": event_trace,
        "receipts": APPLIED_RECEIPT,
        "writes": writes,
        "final_state": final_state,
        "reason_code": reason,
        "coverage": _coverage(result_status, failure_class),
    }


def refreeze_targeted() -> None:
    registry = load(MANIFESTS / "verification-registry_v1.0.json")
    fixtures = load(MANIFESTS / "verification-fixtures_v1.0.json")
    oracles = load(MANIFESTS / "verification-oracles_v1.0.json")
    manifest = load(MANIFESTS / "verification-manifest_v1.0.json")

    for oracle_id, oracle in sorted(oracles["oracles"].items()):
        variant = oracle["variant_id"]
        fixture = fixtures["fixtures"][f"dal.verification.fixture/{TARGET_TEST_ID}/{variant}/G3/1.0"]
        derived = derive(fixture)
        if oracle["expected_result_status"] != derived["result_status"]:
            raise SystemExit(f"{variant}: result status drift")
        if oracle["expected_failure_class"] != derived["failure_class"]:
            raise SystemExit(f"{variant}: failure class drift")
        if oracle["expected_last_verified_sha"] != derived["last_verified_sha"]:
            raise SystemExit(f"{variant}: last verified sha drift")
        if oracle["expected_report_hash"] != derived["report_hash"]:
            raise SystemExit(f"{variant}: report hash drift")
        if oracle["expected_state_trace"] != derived["state_trace"]:
            raise SystemExit(f"{variant}: state trace drift")
        if oracle["expected_event_trace"] != derived["event_trace"]:
            raise SystemExit(f"{variant}: event trace drift")
        if oracle["expected_receipts"] != derived["receipts"]:
            raise SystemExit(f"{variant}: receipt drift")
        if oracle["allowed_write_set"] != derived["writes"]:
            raise SystemExit(f"{variant}: write set drift")
        if oracle["expected_final_snapshot"]["state"] != derived["final_state"]:
            raise SystemExit(f"{variant}: final state drift")
        if oracle["expected_final_snapshot"]["reason_code"] != derived["reason_code"]:
            raise SystemExit(f"{variant}: reason drift")
        if oracle["coverage_ref"] != derived["coverage"]:
            raise SystemExit(f"{variant}: coverage drift")

    authority_path = MANIFESTS / "verification-authority_v1.0.json"
    authority = load(authority_path)
    generated_entries = expected_oracle_entries(manifest, fixtures, oracles)

    def target(key: str) -> bool:
        return f"/{TARGET_TEST_ID}/" in key

    unexpected_entries = {
        key for key, value in generated_entries.items()
        if authority["oracle_entries"].get(key) != value and not target(key)
    }
    if unexpected_entries:
        raise SystemExit(f"unrelated oracle entry drift: {sorted(unexpected_entries)}")

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
