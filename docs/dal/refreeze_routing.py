#!/usr/bin/env python3
"""Targeted authority re-freeze for the routing handoff.

Follows `refreeze_coder_adapter.py`: it re-derives every
`DAL-T-ROUTING-CONTRACT-001` variant's classifier outcome from the fixture facts
and injected evidence, using a *separate* encoding of the frozen §6 precedence
tree — not `build_routing_manifests.py`'s `classify()`.  It then proves that no
unrelated authority row differs from the generated expectation, splices only the
routing-oracle entries, and recomputes the authority self-hash.

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
from verify_routing_authority import (  # noqa: E402
    expected_oracle_entries,
    semantic_counts,
)


TARGET_TEST_ID = "DAL-T-ROUTING-CONTRACT-001"

BLOCK_WRITES = [
    "aggregate", "business_event", "transition_receipt", "audit",
    "decision_create", "decision_projection", "notification_outbox",
]
APPLIED_RECEIPT = [
    {"code": "APPLIED", "count": 1, "schema_version": "dal.transition-receipt/1.0"}
]

SLOT_NAMES = frozenset({"primary", "fallback", "classifier", "review"})
FALLBACK_ALLOWED = frozenset({"transient", "usage_limit", "auth", "contract_failure"})

FAILURE_TO_REASON = {
    "policy_failure": "POLICY_FAILURE",
    "contract_failure": "PROVIDER_CONTRACT_FAILURE",
}
REASON_TO_STATE = {
    "POLICY_FAILURE": "needs_human",
    "PROVIDER_CONTRACT_FAILURE": "needs_human",
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


# ---------------------------------------------------------------------------
# Independent re-derivation of the thirteen variants.  The classifier is a pure
# function of (facts, injected); the transition, handoff state, write set, event
# and coverage label all follow from that pair plus the injection signature.
# ---------------------------------------------------------------------------
def _integrity(snapshot: dict, observed: dict, digest_value: str) -> bool:
    frozen = snapshot["classifier"]
    return (
        observed.get("provider") == frozen["provider"]
        and observed.get("model") == frozen["model"]
        and observed.get("digest_pre") == digest_value
        and observed.get("digest_post") == digest_value
    )


def _configured(snapshot: dict) -> bool:
    slot = snapshot.get("fallback")
    return slot is not None and bool(slot.get("provider")) and bool(slot.get("model"))


def _classify(facts: dict, injected: dict) -> tuple:
    snapshot = facts["routing_snapshot"]
    observed = injected["observed_classifier"]
    if not _integrity(snapshot, observed, facts["classifier_digest"]):
        return ("blocked", "policy_failure")
    requested = facts["requested_slot"]
    if requested not in SLOT_NAMES:
        return ("blocked", "contract_failure")
    if requested != "fallback":
        return ("fallback_denied", None)
    if not _configured(snapshot):
        return ("no_fallback_route", None)
    if injected["primary_failure_class"] in FALLBACK_ALLOWED:
        return ("fallback_allowed", None)
    return ("fallback_denied", None)


def _coverage(result_status: str, failure_class: str | None, injected: dict) -> str:
    #: The coverage label's short-form failure-class segment, matching the
    #: generator's explicit labels (e.g. `FALLBACK-DENIED-BUDGET--coding`).
    short = {
        "transient": "TRANSIENT",
        "usage_limit": "USAGE",
        "auth": "AUTH",
        "contract_failure": "CONTRACT",
        "budget_limit": "BUDGET",
        "policy_failure": "POLICY",
        "task_failure": "TASK",
    }
    if result_status == "blocked":
        if failure_class == "policy_failure":
            return "BLK-POLICY-CLASSIFIER--coding"
        return "BLK-CONTRACT-SLOT--coding"
    if result_status == "fallback_allowed":
        return f"FALLBACK-ALLOWED-{short[injected['primary_failure_class']]}--coding"
    if result_status == "no_fallback_route":
        return "FALLBACK-UNCONFIGURED--coding"
    if injected["primary_failure_class"] is None:
        return "FALLBACK-DENIED-CANCEL--coding"
    return f"FALLBACK-DENIED-{short[injected['primary_failure_class']]}--coding"


def derive(fixture: dict) -> dict:
    command = fixture["operation_sequence"][0]
    facts = command["input"]["authoritative_facts"]
    injected = command["input"]["injected_results"]

    result_status, failure_class = _classify(facts, injected)
    reason = None if failure_class is None else FAILURE_TO_REASON[failure_class]
    final_state = "coding" if failure_class is None else REASON_TO_STATE[reason]
    state_trace = ["coding"] if failure_class is None else ["coding", final_state]
    event_trace = [] if failure_class is None else ["feature.blocked"]
    writes = [] if failure_class is None else sorted(BLOCK_WRITES)
    handoff_state = (
        dict(facts["work_state"]) if result_status == "fallback_allowed" else None
    )

    return {
        "result_status": result_status,
        "failure_class": failure_class,
        "handoff_state": handoff_state,
        "state_trace": state_trace,
        "event_trace": event_trace,
        "receipts": APPLIED_RECEIPT,
        "writes": writes,
        "final_state": final_state,
        "reason_code": reason,
        "coverage": _coverage(result_status, failure_class, injected),
    }


def refreeze_targeted() -> None:
    registry = load(MANIFESTS / "routing-registry_v1.0.json")
    fixtures = load(MANIFESTS / "routing-fixtures_v1.0.json")
    oracles = load(MANIFESTS / "routing-oracles_v1.0.json")
    manifest = load(MANIFESTS / "routing-manifest_v1.0.json")

    for oracle_id, oracle in sorted(oracles["oracles"].items()):
        variant = oracle["variant_id"]
        fixture = fixtures["fixtures"][f"dal.routing.fixture/{TARGET_TEST_ID}/{variant}/G3/1.0"]
        derived = derive(fixture)
        if oracle["expected_result_status"] != derived["result_status"]:
            raise SystemExit(f"{variant}: result status drift")
        if oracle["expected_failure_class"] != derived["failure_class"]:
            raise SystemExit(f"{variant}: failure class drift")
        if oracle["expected_handoff_state"] != derived["handoff_state"]:
            raise SystemExit(f"{variant}: handoff state drift")
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

    authority_path = MANIFESTS / "routing-authority_v1.0.json"
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
