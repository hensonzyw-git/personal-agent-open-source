#!/usr/bin/env python3
"""Build the routing handoff machine contract (freeze pack §8).

Machine counterpart of `DAL027_fallback_classifier_合同冻结包_v0.1.md`.  It emits
the closed `dal.routing-request/1.0` and `dal.routing-response/1.0` JSON Schemas,
the slot-vocabulary + fallback-decision registry, and the thirteen
`DAL-T-ROUTING-CONTRACT-001` adversarial oracles plus their fixtures and manifest
rows.  It is documentation tooling only: it does not import or execute DAL
runtime, Worker, provider, or Personal Agent code, and it never touches
credentials.

The static authority that freezes the semantics of these files lives in
`manifests/routing-authority_v1.0.json` and is verified by
`verify_routing_authority.py`; `refreeze_routing.py` re-derives each variant
independently and splices a targeted change, following the
`refreeze_coder_adapter.py` pattern.

Like `build_coder_adapter_manifests.py` this builder does not depend on the
transition-spec registry: the routing classifier is a *forward-ref* handler
that has not yet been wired into the dispatch graph, so its oracles freeze only
the classifier's own `(result_status, failure_class, reason_code)` outcome, the
feature transition it implies, the handoff state it preserves, and the coverage
label.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dal_jcs import canonical_bytes

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "manifests"

OPERATION_SPEC_ID = "OP-ROUTING-CONTRACT-001"
COMMAND_TYPE = "consume_handoff"
SERVICE_ACTOR = "service"
EVIDENCE_SOURCE = "routing-adapter"
CONTRACT_VERSION = "dal.routing-resolution/1.0"
TEST_ID = "DAL-T-ROUTING-CONTRACT-001"

FEATURE_TRANSITION_RECEIPT_SCHEMA = "dal.transition-receipt/1.0"

# Frozen classifier anchors (docs/DAL027 §2–§6).  The slot vocabulary is taken
# verbatim from the frozen `routing-freeze_schema_v1.0.json` `routing[].slot`
# role names; the machine classifier and the verify tooling must agree on it.
SLOT_NAMES = ["fallback", "classifier", "primary", "review"]
SLOT_FIELDS = ["model", "provider"]
WORK_STATE_FIELDS = ["base_sha", "diff_sha", "last_verified_sha", "tests_receipt"]
CLASSIFIER_OBSERVED_FIELDS = ["digest_post", "digest_pre", "model", "provider"]

BASE_SHA = "0123456789abcdef0123456789abcdef01234567"  # 40 hex
DIFF_SHA = "d" * 64
TESTS_RECEIPT = "dal.transition-receipt/1.0:receipt-7"
LAST_VERIFIED_SHA = "fedcba9876543210fedcba9876543210fedcba98"  # 40 hex
CLASSIFIER_DIGEST = "b" * 64

# Frozen routing snapshot (provider + model per slot; no per-slot digest — the
# classifier digest travels as its own fact, matching the freeze schema).
PRIMARY_SLOT = {"provider": "ccr", "model": "DeepSeek/deepseek-v4-pro"}
FALLBACK_SLOT = {"provider": "changhe", "model": "Changhe/glm-5.2"}
CLASSIFIER_SLOT = {"provider": "glm", "model": "GLM/glm-4.5-air"}
REVIEW_SLOT = {"provider": "codex", "model": "Codex/codex-5.6-sol"}

BLOCK_WRITE_SET = [
    "aggregate", "business_event", "transition_receipt", "audit",
    "decision_create", "decision_projection", "notification_outbox",
]
BLOCK_EVENT = "feature.blocked"
APPLIED_RECEIPT = [
    {"code": "APPLIED", "count": 1, "schema_version": "dal.transition-receipt/1.0"}
]
FORBIDDEN_BASE = [
    "provider_call", "network_call", "filesystem_write", "process_spawn",
    "production_access",
]

FACT_FIELDS = frozenset({
    "requested_slot", "routing_snapshot", "classifier_digest", "work_state",
})
INJECTED_FIELDS = frozenset({
    "primary_failure_class", "observed_classifier",
})

FAILURE_TO_REASON = {
    "policy_failure": "POLICY_FAILURE",
    "contract_failure": "PROVIDER_CONTRACT_FAILURE",
}
REASON_TO_STATE = {
    "POLICY_FAILURE": "needs_human",
    "PROVIDER_CONTRACT_FAILURE": "needs_human",
}
#: The primary failure classes a fallback may repair. `task_failure` is
#: deliberately absent — swapping models cannot fix failing tests (DAL-030).
FALLBACK_ALLOWED_CLASSES = ["auth", "contract_failure", "transient", "usage_limit"]

#: The closed routing outcome vocabulary — a decision layer, never a new
#: failure class.
RESULT_STATUS_ENUM = [
    "blocked", "fallback_allowed", "fallback_denied", "no_fallback_route",
]

_HEX = "0123456789abcdef"


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _is_sha40_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX for c in value)


# ---------------------------------------------------------------------------
# §6 classification (the generator's own encoding of the frozen rules).
# ---------------------------------------------------------------------------
def _classifier_integrity(snapshot: dict, observed: dict, digest_value: str) -> bool:
    frozen = snapshot["classifier"]
    if observed.get("provider") != frozen["provider"]:
        return False
    if observed.get("model") != frozen["model"]:
        return False
    if observed.get("digest_pre") != digest_value:
        return False
    if observed.get("digest_post") != digest_value:
        return False
    return True


def _fallback_configured(snapshot: dict) -> bool:
    slot = snapshot.get("fallback")
    if slot is None:
        return False
    return bool(slot.get("provider")) and bool(slot.get("model"))


def classify(facts: dict, injected: dict) -> tuple:
    """Return (result_status, failure_class, reason_code) per §6 precedence."""
    snapshot = facts["routing_snapshot"]
    observed = injected["observed_classifier"]
    # 1. classifier integrity (leak/policy check first)
    if not _classifier_integrity(snapshot, observed, facts["classifier_digest"]):
        return ("blocked", "policy_failure", "POLICY_FAILURE")
    # 2. unknown slot request fails closed
    requested = facts["requested_slot"]
    if requested not in SLOT_NAMES:
        return ("blocked", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    # 3. only the fallback slot is a handoff target
    if requested != "fallback":
        return ("fallback_denied", None, None)
    # 4. an unconfigured fallback must not be invented
    if not _fallback_configured(snapshot):
        return ("no_fallback_route", None, None)
    # 5. the primary failure class decides eligibility
    if injected["primary_failure_class"] in FALLBACK_ALLOWED_CLASSES:
        return ("fallback_allowed", None, None)
    return ("fallback_denied", None, None)


def transition_outcome(
    result_status: str, failure_class: str | None, work_state: dict
) -> dict:
    """Map a classification to the feature transition and handoff it implies."""
    if result_status != "blocked":
        return {
            "state_trace": ["coding"], "event_trace": [], "receipts": APPLIED_RECEIPT,
            "writes": [], "reason": None, "final_state": "coding",
            "handoff_state": dict(work_state) if result_status == "fallback_allowed" else None,
        }
    reason = FAILURE_TO_REASON[failure_class]
    final_state = REASON_TO_STATE[reason]
    return {
        "state_trace": ["coding", final_state], "event_trace": [BLOCK_EVENT],
        "receipts": APPLIED_RECEIPT, "writes": sorted(BLOCK_WRITE_SET),
        "reason": reason, "final_state": final_state, "handoff_state": None,
    }


# ---------------------------------------------------------------------------
# The 13 frozen variants (inputs only — outcomes are derived).
# ---------------------------------------------------------------------------
def _snapshot(*, with_fallback: bool = True) -> dict:
    value = {
        "primary": dict(PRIMARY_SLOT),
        "classifier": dict(CLASSIFIER_SLOT),
        "review": dict(REVIEW_SLOT),
    }
    if with_fallback:
        value["fallback"] = dict(FALLBACK_SLOT)
    return value


def _work_state() -> dict:
    return {
        "base_sha": BASE_SHA,
        "diff_sha": DIFF_SHA,
        "tests_receipt": TESTS_RECEIPT,
        "last_verified_sha": LAST_VERIFIED_SHA,
    }


def _facts(**overrides: object) -> dict:
    base: dict = {
        "requested_slot": "fallback",
        "routing_snapshot": _snapshot(),
        "classifier_digest": CLASSIFIER_DIGEST,
        "work_state": _work_state(),
    }
    base.update(overrides)
    return base


def _observed(**overrides: object) -> dict:
    base: dict = {
        "provider": CLASSIFIER_SLOT["provider"],
        "model": CLASSIFIER_SLOT["model"],
        "digest_pre": CLASSIFIER_DIGEST,
        "digest_post": CLASSIFIER_DIGEST,
    }
    base.update(overrides)
    return base


def _injected(**overrides: object) -> dict:
    base: dict = {
        "primary_failure_class": None,
        "observed_classifier": _observed(),
    }
    base.update(overrides)
    return base


VARIANTS = [
    # (name, fact_overrides, injected_overrides, coverage_ref)
    ("primary_transient", {}, {"primary_failure_class": "transient"},
     "FALLBACK-ALLOWED-TRANSIENT--coding"),
    ("primary_usage_limit", {}, {"primary_failure_class": "usage_limit"},
     "FALLBACK-ALLOWED-USAGE--coding"),
    ("primary_auth", {}, {"primary_failure_class": "auth"},
     "FALLBACK-ALLOWED-AUTH--coding"),
    ("primary_contract_failure", {}, {"primary_failure_class": "contract_failure"},
     "FALLBACK-ALLOWED-CONTRACT--coding"),
    ("primary_budget_limit", {}, {"primary_failure_class": "budget_limit"},
     "FALLBACK-DENIED-BUDGET--coding"),
    ("primary_policy_failure", {}, {"primary_failure_class": "policy_failure"},
     "FALLBACK-DENIED-POLICY--coding"),
    ("primary_task_failure", {}, {"primary_failure_class": "task_failure"},
     "FALLBACK-DENIED-TASK--coding"),
    ("primary_cancelled", {}, {"primary_failure_class": None},
     "FALLBACK-DENIED-CANCEL--coding"),
    ("classifier_modified", {},
     {"observed_classifier": _observed(provider="evil.example.com:443")},
     "BLK-POLICY-CLASSIFIER--coding"),
    ("classifier_replaced", {},
     {"observed_classifier": _observed(model="DeepSeek/deepseek-v4-flash")},
     "BLK-POLICY-CLASSIFIER--coding"),
    ("classifier_digest_drift", {},
     {"observed_classifier": _observed(digest_pre="c" * 64)},
     "BLK-POLICY-CLASSIFIER--coding"),
    ("unknown_slot_request", {"requested_slot": "hack"}, {},
     "BLK-CONTRACT-SLOT--coding"),
    ("fallback_unconfigured", {"routing_snapshot": _snapshot(with_fallback=False)}, {},
     "FALLBACK-UNCONFIGURED--coding"),
]


def _command(name: str, facts: dict, injected: dict) -> dict:
    return {
        "operation_spec_id": OPERATION_SPEC_ID,
        "schema_version": "dal.test-operation-command/1.0",
        "operation_id": name,
        "idempotency_key": name,
        "actor_type": SERVICE_ACTOR,
        "evidence_source_type": EVIDENCE_SOURCE,
        "input": {
            "schema_version": "dal.operation-input/1.0",
            "target": {"entity_id": "fixture-entity", "entity_type": "feature",
                       "state": "coding", "version": 7},
            "action_sequence": [{"command": COMMAND_TYPE, "contract_version": CONTRACT_VERSION}],
            "authoritative_facts": facts,
            "injected_results": injected,
        },
    }


def _variant_fixture(name: str, fact_overrides: dict, injected_overrides: dict) -> dict:
    facts = _facts(**fact_overrides)
    injected = _injected(**injected_overrides)
    return {
        "schema_version": "dal.routing-fixture/1.0",
        "test_id": TEST_ID,
        "variant_id": name,
        "run_gate": "G3",
        "pre_state": {"entity_id": "fixture-entity", "entity_type": "feature",
                      "state": "coding", "version": 7},
        "operation_sequence": [_command(name, facts, injected)],
    }


def _variant_oracle(name: str, fact_overrides: dict, injected_overrides: dict,
                    coverage_ref: str) -> dict:
    facts = _facts(**fact_overrides)
    injected = _injected(**injected_overrides)
    result_status, failure_class, _reason = classify(facts, injected)
    tx = transition_outcome(result_status, failure_class, facts["work_state"])
    return {
        "schema_version": "dal.routing-oracle/1.0",
        "test_id": TEST_ID,
        "variant_id": name,
        "run_gate": "G3",
        "pre_state": {"entity_type": "feature", "state": "coding", "version": 7},
        "expected_result_status": result_status,
        "expected_failure_class": failure_class,
        "expected_handoff_state": tx["handoff_state"],
        "expected_state_trace": tx["state_trace"],
        "expected_event_trace": tx["event_trace"],
        "expected_receipts": tx["receipts"],
        "expected_external_effect_trace": [],
        "expected_final_snapshot": {
            "entity_type": "feature",
            "reason_code": tx["reason"],
            "reason_owner": "feature" if tx["reason"] else None,
            "state": tx["final_state"],
        },
        "allowed_write_set": tx["writes"],
        "forbidden_side_effects": sorted(FORBIDDEN_BASE),
        "coverage_ref": coverage_ref,
    }


# ---------------------------------------------------------------------------
# Schemas + registry.
# ---------------------------------------------------------------------------
def _build_request_schema() -> dict:
    slot_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["provider", "model"],
        "properties": {
            "provider": {"type": "string"},
            "model": {"type": "string"},
        },
    }
    return {
        "$id": "dal.routing-request/1.0",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "The trusted (protected) half of the routing handoff. routing_snapshot carries the frozen slot table (primary/classifier required, fallback/review optional); a slot with empty provider/model is unconfigured, never an invented route. classifier_digest is the frozen classifier fingerprint; work_state is the four-field state a fallback must inherit verbatim.",
        "schema_version": "dal.routing-request/1.0",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "operation_spec_id", "contract_version",
                     "authoritative_facts"],
        "properties": {
            "schema_version": {"const": "dal.routing-request/1.0"},
            "operation_spec_id": {"const": OPERATION_SPEC_ID},
            "contract_version": {"const": CONTRACT_VERSION},
            "authoritative_facts": {
                "type": "object", "additionalProperties": False,
                "required": sorted(FACT_FIELDS),
                "properties": {
                    "requested_slot": {"type": "string", "minLength": 1},
                    "routing_snapshot": {
                        "type": "object", "additionalProperties": False,
                        "required": ["primary", "classifier"],
                        "properties": {
                            "primary": slot_schema,
                            "fallback": slot_schema,
                            "classifier": slot_schema,
                            "review": slot_schema,
                        },
                    },
                    "classifier_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "work_state": {
                        "type": "object", "additionalProperties": False,
                        "required": sorted(WORK_STATE_FIELDS),
                        "properties": {
                            "base_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                            "diff_sha": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "tests_receipt": {"type": "string", "minLength": 1},
                            "last_verified_sha": {
                                "type": ["string", "null"],
                                "pattern": "^[0-9a-f]{40}$",
                            },
                        },
                    },
                },
            },
        },
    }


def _build_response_schema() -> dict:
    reason_enum = sorted(FAILURE_TO_REASON.values())
    return {
        "$id": "dal.routing-response/1.0",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "The routing classifier result. result_status is the handoff decision (blocked / fallback_allowed / fallback_denied / no_fallback_route); failure_class is only ever policy_failure or contract_failure and only for the fail-closed blocks. handoff_state is the preserved four-field work state for fallback_allowed, null otherwise.",
        "schema_version": "dal.routing-response/1.0",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "result_status", "failure_class",
                     "reason_code", "final_state", "handoff_state"],
        "properties": {
            "schema_version": {"const": "dal.routing-response/1.0"},
            "result_status": {"enum": RESULT_STATUS_ENUM},
            "failure_class": {"enum": ["contract_failure", "policy_failure", None],
                              "type": ["string", "null"]},
            "reason_code": {"enum": reason_enum + [None], "type": ["string", "null"]},
            "final_state": {"type": "string", "minLength": 1},
            "handoff_state": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": sorted(WORK_STATE_FIELDS),
                "properties": {
                    "base_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                    "diff_sha": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "tests_receipt": {"type": "string", "minLength": 1},
                    "last_verified_sha": {
                        "type": ["string", "null"],
                        "pattern": "^[0-9a-f]{40}$",
                    },
                },
            },
        },
    }


def _build_registry() -> dict:
    value = {
        "schema_version": "dal.routing-registry/1.0",
        "operation_spec_id": OPERATION_SPEC_ID,
        "command_type": COMMAND_TYPE,
        "contract_version": CONTRACT_VERSION,
        "service_actor": SERVICE_ACTOR,
        "evidence_source": EVIDENCE_SOURCE,
        "feature_transition_receipt_schema": FEATURE_TRANSITION_RECEIPT_SCHEMA,
        "slot_names": sorted(SLOT_NAMES),
        "slot_fields": sorted(SLOT_FIELDS),
        "work_state_fields": sorted(WORK_STATE_FIELDS),
        "classifier_observed_fields": sorted(CLASSIFIER_OBSERVED_FIELDS),
        "fact_fields": sorted(FACT_FIELDS),
        "injected_fields": sorted(INJECTED_FIELDS),
        "fallback_allowed_classes": FALLBACK_ALLOWED_CLASSES,
        "failure_to_reason": FAILURE_TO_REASON,
        "reason_to_state": REASON_TO_STATE,
        "result_status_enum": RESULT_STATUS_ENUM,
        "classification_precedence": [
            "classifier_integrity", "unknown_slot_request", "non_fallback_slot",
            "fallback_unconfigured", "primary_failure_class_eligibility",
        ],
        "block_event": BLOCK_EVENT,
        "block_write_set": sorted(BLOCK_WRITE_SET),
    }
    value["registry_sha256"] = digest(
        {k: v for k, v in value.items() if k != "registry_sha256"}
    )
    return value


def _write(name: str, value: dict) -> None:
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    _write("routing-request_schema_v1.0.json", _build_request_schema())
    _write("routing-response_schema_v1.0.json", _build_response_schema())

    registry = _build_registry()
    _write("routing-registry_v1.0.json", registry)

    fixtures = {}
    oracles = {}
    manifest_rows = []
    for (name, fact_overrides, injected_overrides, coverage_ref) in VARIANTS:
        fixtures[f"dal.routing.fixture/{TEST_ID}/{name}/G3/1.0"] = \
            _variant_fixture(name, fact_overrides, injected_overrides)
        oracles[f"dal.routing.oracle/{TEST_ID}/{name}/G3/1.0"] = \
            _variant_oracle(name, fact_overrides, injected_overrides, coverage_ref)
        manifest_rows.append({
            "test_id": TEST_ID,
            "variant_id": name,
            "run_gate": "G3",
            "owner_tasks": ["DAL-027"],
            "fixture_ref": f"dal.routing.fixture/{TEST_ID}/{name}/G3/1.0",
            "oracle_id": f"dal.routing.oracle/{TEST_ID}/{name}/G3/1.0",
        })

    fixture_catalog = {
        "schema_version": "dal.routing-fixture-catalog/1.0",
        "registry_sha256": registry["registry_sha256"],
        "fixtures": fixtures,
    }
    fixture_catalog["catalog_sha256"] = digest(
        {k: v for k, v in fixture_catalog.items() if k != "catalog_sha256"}
    )
    _write("routing-fixtures_v1.0.json", fixture_catalog)

    oracle_catalog = {
        "schema_version": "dal.routing-oracle-catalog/1.0",
        "registry_sha256": registry["registry_sha256"],
        "oracles": oracles,
    }
    oracle_catalog["catalog_sha256"] = digest(
        {k: v for k, v in oracle_catalog.items() if k != "catalog_sha256"}
    )
    _write("routing-oracles_v1.0.json", oracle_catalog)

    manifest = {
        "schema_version": "dal.routing-manifest/1.0",
        "manifest_version": "1.0",
        "registry_sha256": registry["registry_sha256"],
        "fixture_catalog_sha256": fixture_catalog["catalog_sha256"],
        "oracle_catalog_sha256": oracle_catalog["catalog_sha256"],
        "test_variants": manifest_rows,
    }
    manifest["manifest_sha256"] = digest(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    )
    _write("routing-manifest_v1.0.json", manifest)

    print(json.dumps({
        "routing_variants": len(VARIANTS),
        "slot_names": sorted(SLOT_NAMES),
        "registry_sha256": registry["registry_sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
