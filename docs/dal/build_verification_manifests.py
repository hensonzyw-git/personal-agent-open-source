#!/usr/bin/env python3
"""Build the deterministic-verification machine contract (freeze pack §8).

Machine counterpart of `DAL029_确定性验证流水线_合同冻结包_v0.1.md`.  It emits the
closed `dal.verification-request/1.0` and `dal.verification-response/1.0` JSON
Schemas, the stage/registry + verification-decision registry, and the eleven
`DAL-T-VERIFICATION-CONTRACT-001` adversarial oracles plus their fixtures and
manifest rows.  It is documentation tooling only: it does not import or execute
DAL runtime, Worker, provider, or Personal Agent code, and it never touches
credentials.

The static authority that freezes the semantics of these files lives in
`manifests/verification-authority_v1.0.json` and is verified by
`verify_verification_authority.py`; `refreeze_verification.py` re-derives each
variant independently and splices a targeted change, following the
`refreeze_coder_adapter.py` pattern.

Like `build_coder_adapter_manifests.py` this builder does not depend on the
transition-spec registry: the verification classifier is a *forward-ref* handler
that has not yet been wired into the dispatch graph, so its oracles freeze only
the classifier's own `(result_status, failure_class, reason_code)` outcome, the
`last_verified_sha` it advances or preserves, the `report_hash` that binds the
observed commands and exit codes, the feature transition it implies, and the
coverage label.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dal_jcs import canonical_bytes

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "manifests"

OPERATION_SPEC_ID = "OP-VERIFICATION-CONTRACT-001"
COMMAND_TYPE = "consume_verification"
SERVICE_ACTOR = "service"
EVIDENCE_SOURCE = "verification-adapter"
CONTRACT_VERSION = "dal.verification-report/1.0"
TEST_ID = "DAL-T-VERIFICATION-CONTRACT-001"

FEATURE_TRANSITION_RECEIPT_SCHEMA = "dal.transition-receipt/1.0"

#: The five registered stages, in worker order: `diff` is the patch capture; the
#: other four are the toolchain checks whose failure is a `task_failure`.
REGISTRY_STAGES = ["diff", "format", "lint", "build", "test"]
CHECK_STAGES = ["format", "lint", "build", "test"]
STAGE_RESULT_FIELDS = ["command", "exit_code"]

BASE_SHA = "0123456789abcdef0123456789abcdef01234567"  # 40 hex
PRIOR_LAST_VERIFIED_SHA = "fedcba9876543210fedcba9876543210fedcba98"  # 40 hex

DIFF_TEXT = (
    "diff --git a/src/personal_agent_dal/worker/verification.py "
    "b/src/personal_agent_dal/worker/verification.py\n"
    "new file mode 100644\n"
    "index 0000000..1234567\n"
    "--- /dev/null\n"
    "+++ b/src/personal_agent_dal/worker/verification.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def run_verification(repo, manifest):\n"
    "+    return execute_toolchain(repo, manifest)\n"
    "+\n"
)

#: The five registered commands, in worker order. `diff` is `git diff` against
#: the base the patch was taken against; the four check stages are the repo's
#: pinned toolchain commands (reused from `worker/toolchain.py`).
REGISTRY_COMMANDS = {
    "diff": ["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
    "format": ["make", "format"],
    "lint": ["make", "lint"],
    "build": ["make", "build"],
    "test": ["make", "test"],
}

VERIFIED_WRITE_SET = ["aggregate", "transition_receipt"]
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
    "base_sha", "registry_commands", "prior_last_verified_sha",
})
INJECTED_FIELDS = frozenset({
    "diff", "diff_sha", "diff_base_sha", "stage_results",
})

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

#: The closed verification outcome vocabulary: `succeeded`, `blocked`
#: (`task_failure` — the tests genuinely failed), or `failed`
#: (`contract_failure` / `policy_failure` — the feature needs a human).
RESULT_STATUS_ENUM = ["blocked", "failed", "succeeded"]

_HEX = "0123456789abcdef"


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_sha40_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX for c in value)


# ---------------------------------------------------------------------------
# §7 report body + hash.  The report body binds base_sha + diff_sha + the
# observed commands and exit codes; `report_hash` is its RFC 8785 JCS digest.
# ---------------------------------------------------------------------------
def _report_body(facts: dict, injected: dict) -> dict:
    stage_results = injected["stage_results"]
    commands: dict = {}
    exit_codes: dict = {}
    for stage in sorted(key for key in stage_results if isinstance(key, str)):
        observed = stage_results[stage]
        if isinstance(observed, dict):
            commands[stage] = observed.get("command")
            exit_codes[stage] = observed.get("exit_code")
    return {
        "schema_version": CONTRACT_VERSION,
        "base_sha": facts["base_sha"],
        "diff_sha": injected["diff_sha"],
        "commands": commands,
        "exit_codes": exit_codes,
    }


def _report_hash(facts: dict, injected: dict) -> str:
    return digest(_report_body(facts, injected))


# ---------------------------------------------------------------------------
# §6 classification (the generator's own encoding of the frozen rules).
# ---------------------------------------------------------------------------
def _registry_contract_reasons(facts: dict, injected: dict) -> tuple[list[str], bool]:
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
        if not isinstance(observed, dict) or frozenset(observed) != frozenset(STAGE_RESULT_FIELDS):
            reasons.append(f"malformed stage result: {stage}")
            continue
        observed_command = observed.get("command")
        if (
            not isinstance(observed_command, list)
            or not observed_command
            or not all(isinstance(token, str) and token for token in observed_command)
        ):
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

    return reasons, swapped


def _diff_reasons(facts: dict, injected: dict) -> list[str]:
    reasons: list[str] = []
    if not injected["diff"]:
        reasons.append("empty diff")
    if injected["diff_base_sha"] != facts["base_sha"]:
        reasons.append("diff not bound to base_sha")
    if injected["diff_sha"] != _sha256_text(injected["diff"]):
        reasons.append("diff_sha drift")
    return reasons


def classify(facts: dict, injected: dict) -> tuple:
    """Return (result_status, failure_class, reason_code, reasons) per §6."""
    reasons, swapped = _registry_contract_reasons(facts, injected)
    if swapped:
        return ("failed", "policy_failure", "POLICY_FAILURE", ())
    if reasons:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE", reasons)

    diff_reasons = _diff_reasons(facts, injected)
    if diff_reasons:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE", diff_reasons)

    stage_results = injected["stage_results"]
    for stage in CHECK_STAGES:
        if stage_results[stage]["exit_code"] != 0:
            return ("blocked", "task_failure", "TEST_BLOCKED", ())

    return ("succeeded", None, None, ())


def transition_outcome(
    result_status: str, reason_code: str | None, facts: dict
) -> dict:
    """Map a classification to the feature transition and `last_verified_sha`
    it implies.  Success advances `last_verified_sha` to `base_sha`; failure
    preserves the prior value and writes the seven-write block set."""
    if result_status == "succeeded":
        return {
            "state_trace": ["verifying", "verified"], "event_trace": [],
            "receipts": APPLIED_RECEIPT, "writes": sorted(VERIFIED_WRITE_SET),
            "reason": None, "final_state": "verified",
            "last_verified_sha": facts["base_sha"],
        }
    final_state = REASON_TO_STATE[reason_code]
    return {
        "state_trace": ["verifying", final_state], "event_trace": [BLOCK_EVENT],
        "receipts": APPLIED_RECEIPT, "writes": sorted(BLOCK_WRITE_SET),
        "reason": reason_code, "final_state": final_state,
        "last_verified_sha": facts["prior_last_verified_sha"],
    }


# ---------------------------------------------------------------------------
# The 11 frozen variants (inputs only — outcomes are derived).
# ---------------------------------------------------------------------------
def _facts(**overrides: object) -> dict:
    base: dict = {
        "base_sha": BASE_SHA,
        "registry_commands": {k: list(v) for k, v in REGISTRY_COMMANDS.items()},
        "prior_last_verified_sha": None,
    }
    base.update(overrides)
    return base


def _stage_results() -> dict:
    return {
        stage: {"command": list(cmd), "exit_code": 0}
        for stage, cmd in REGISTRY_COMMANDS.items()
    }


def _mutated_stage_results(
    *, exit_codes: dict | None = None, remove: tuple = (), command_overrides: dict | None = None,
) -> dict:
    stage_results = _stage_results()
    if exit_codes:
        for stage, code in exit_codes.items():
            stage_results[stage]["exit_code"] = code
    if command_overrides:
        for stage, cmd in command_overrides.items():
            stage_results[stage]["command"] = list(cmd)
    for stage in remove:
        del stage_results[stage]
    return stage_results


def _injected(**overrides: object) -> dict:
    base: dict = {
        "diff": DIFF_TEXT,
        "diff_sha": _sha256_text(DIFF_TEXT),
        "diff_base_sha": BASE_SHA,
        "stage_results": _stage_results(),
    }
    base.update(overrides)
    return base


VARIANTS = [
    # (name, fact_overrides, injected_overrides, coverage_ref)
    ("all_pass", {}, {},
     "VERIFY-SUCCEED--verifying"),
    ("diff_empty", {}, {"diff": ""},
     "BLK-CONTRACT--verifying"),
    ("diff_hash_drift", {}, {"diff_sha": "d" * 64},
     "BLK-CONTRACT--verifying"),
    ("diff_base_mismatch", {}, {"diff_base_sha": "0" * 40},
     "BLK-CONTRACT--verifying"),
    ("format_fail", {}, {"stage_results": _mutated_stage_results(exit_codes={"format": 1})},
     "BLK-TASK--verifying"),
    ("lint_fail", {}, {"stage_results": _mutated_stage_results(exit_codes={"lint": 1})},
     "BLK-TASK--verifying"),
    ("build_fail", {}, {"stage_results": _mutated_stage_results(exit_codes={"build": 1})},
     "BLK-TASK--verifying"),
    ("test_fail", {}, {"stage_results": _mutated_stage_results(exit_codes={"test": 1})},
     "BLK-TASK--verifying"),
    ("command_not_in_registry", {},
     {"stage_results": _mutated_stage_results(command_overrides={"test": ["make", "test", "--evil"]})},
     "BLK-POLICY--verifying"),
    ("missing_stage", {}, {"stage_results": _mutated_stage_results(remove=("test",))},
     "BLK-CONTRACT--verifying"),
    ("fail_preserves_last_verified_sha",
     {"prior_last_verified_sha": PRIOR_LAST_VERIFIED_SHA},
     {"stage_results": _mutated_stage_results(exit_codes={"test": 1})},
     "BLK-TASK--verifying"),
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
                       "state": "verifying", "version": 7},
            "action_sequence": [{"command": COMMAND_TYPE, "contract_version": CONTRACT_VERSION}],
            "authoritative_facts": facts,
            "injected_results": injected,
        },
    }


def _variant_fixture(name: str, fact_overrides: dict, injected_overrides: dict) -> dict:
    facts = _facts(**fact_overrides)
    injected = _injected(**injected_overrides)
    return {
        "schema_version": "dal.verification-fixture/1.0",
        "test_id": TEST_ID,
        "variant_id": name,
        "run_gate": "G3",
        "pre_state": {"entity_id": "fixture-entity", "entity_type": "feature",
                      "state": "verifying", "version": 7},
        "operation_sequence": [_command(name, facts, injected)],
    }


def _variant_oracle(name: str, fact_overrides: dict, injected_overrides: dict,
                    coverage_ref: str) -> dict:
    facts = _facts(**fact_overrides)
    injected = _injected(**injected_overrides)
    result_status, failure_class, reason_code, _reasons = classify(facts, injected)
    tx = transition_outcome(result_status, reason_code, facts)
    return {
        "schema_version": "dal.verification-oracle/1.0",
        "test_id": TEST_ID,
        "variant_id": name,
        "run_gate": "G3",
        "pre_state": {"entity_type": "feature", "state": "verifying", "version": 7},
        "expected_result_status": result_status,
        "expected_failure_class": failure_class,
        "expected_last_verified_sha": tx["last_verified_sha"],
        "expected_report_hash": _report_hash(facts, injected),
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
    return {
        "$id": "dal.verification-request/1.0",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "The trusted (protected) half of a verification run. registry_commands declares exactly the five registered stages (diff capture + format/lint/build/test), each a non-empty argv; base_sha is the base the diff was taken against; prior_last_verified_sha is the last verified SHA before this run (preserved on failure).",
        "schema_version": "dal.verification-request/1.0",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "operation_spec_id", "contract_version",
                     "authoritative_facts"],
        "properties": {
            "schema_version": {"const": "dal.verification-request/1.0"},
            "operation_spec_id": {"const": OPERATION_SPEC_ID},
            "contract_version": {"const": CONTRACT_VERSION},
            "authoritative_facts": {
                "type": "object", "additionalProperties": False,
                "required": sorted(FACT_FIELDS),
                "properties": {
                    "base_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                    "registry_commands": {
                        "type": "object", "additionalProperties": False,
                        "required": sorted(REGISTRY_STAGES),
                        "properties": {
                            stage: {
                                "type": "array", "items": {"type": "string"},
                                "minItems": 1,
                            }
                            for stage in REGISTRY_STAGES
                        },
                    },
                    "prior_last_verified_sha": {
                        "type": ["string", "null"],
                        "pattern": "^[0-9a-f]{40}$",
                    },
                },
            },
        },
    }


def _build_response_schema() -> dict:
    reason_enum = sorted(FAILURE_TO_REASON.values())
    return {
        "$id": "dal.verification-response/1.0",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "The verification classifier result. result_status is the deterministic verdict (succeeded / blocked / failed); failure_class is task_failure for a failed check stage, contract_failure for a drifted diff or malformed stage set, policy_failure for a swapped command. last_verified_sha advances to base_sha on success and preserves the prior value otherwise; report_hash binds base_sha + diff_sha + the observed commands and exit codes.",
        "schema_version": "dal.verification-response/1.0",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "result_status", "failure_class",
                     "reason_code", "final_state", "last_verified_sha", "report_hash"],
        "properties": {
            "schema_version": {"const": "dal.verification-response/1.0"},
            "result_status": {"enum": RESULT_STATUS_ENUM},
            "failure_class": {
                "enum": ["contract_failure", "policy_failure", "task_failure", None],
                "type": ["string", "null"],
            },
            "reason_code": {"enum": reason_enum + [None], "type": ["string", "null"]},
            "final_state": {"type": "string", "minLength": 1},
            "last_verified_sha": {
                "type": ["string", "null"],
                "pattern": "^[0-9a-f]{40}$",
            },
            "report_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    }


def _build_registry() -> dict:
    value = {
        "schema_version": "dal.verification-registry/1.0",
        "operation_spec_id": OPERATION_SPEC_ID,
        "command_type": COMMAND_TYPE,
        "contract_version": CONTRACT_VERSION,
        "service_actor": SERVICE_ACTOR,
        "evidence_source": EVIDENCE_SOURCE,
        "feature_transition_receipt_schema": FEATURE_TRANSITION_RECEIPT_SCHEMA,
        "registry_stages": list(REGISTRY_STAGES),
        "check_stages": list(CHECK_STAGES),
        "stage_result_fields": sorted(STAGE_RESULT_FIELDS),
        "fact_fields": sorted(FACT_FIELDS),
        "injected_fields": sorted(INJECTED_FIELDS),
        "failure_to_reason": FAILURE_TO_REASON,
        "reason_to_state": REASON_TO_STATE,
        "result_status_enum": RESULT_STATUS_ENUM,
        "classification_precedence": [
            "registry_command_swap", "registry_contract", "diff_integrity",
            "stage_exit_codes",
        ],
        "block_event": BLOCK_EVENT,
        "block_write_set": sorted(BLOCK_WRITE_SET),
        "verified_write_set": sorted(VERIFIED_WRITE_SET),
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
    _write("verification-request_schema_v1.0.json", _build_request_schema())
    _write("verification-response_schema_v1.0.json", _build_response_schema())

    registry = _build_registry()
    _write("verification-registry_v1.0.json", registry)

    fixtures = {}
    oracles = {}
    manifest_rows = []
    for (name, fact_overrides, injected_overrides, coverage_ref) in VARIANTS:
        fixtures[f"dal.verification.fixture/{TEST_ID}/{name}/G3/1.0"] = \
            _variant_fixture(name, fact_overrides, injected_overrides)
        oracles[f"dal.verification.oracle/{TEST_ID}/{name}/G3/1.0"] = \
            _variant_oracle(name, fact_overrides, injected_overrides, coverage_ref)
        manifest_rows.append({
            "test_id": TEST_ID,
            "variant_id": name,
            "run_gate": "G3",
            "owner_tasks": ["DAL-029"],
            "fixture_ref": f"dal.verification.fixture/{TEST_ID}/{name}/G3/1.0",
            "oracle_id": f"dal.verification.oracle/{TEST_ID}/{name}/G3/1.0",
        })

    fixture_catalog = {
        "schema_version": "dal.verification-fixture-catalog/1.0",
        "registry_sha256": registry["registry_sha256"],
        "fixtures": fixtures,
    }
    fixture_catalog["catalog_sha256"] = digest(
        {k: v for k, v in fixture_catalog.items() if k != "catalog_sha256"}
    )
    _write("verification-fixtures_v1.0.json", fixture_catalog)

    oracle_catalog = {
        "schema_version": "dal.verification-oracle-catalog/1.0",
        "registry_sha256": registry["registry_sha256"],
        "oracles": oracles,
    }
    oracle_catalog["catalog_sha256"] = digest(
        {k: v for k, v in oracle_catalog.items() if k != "catalog_sha256"}
    )
    _write("verification-oracles_v1.0.json", oracle_catalog)

    manifest = {
        "schema_version": "dal.verification-manifest/1.0",
        "manifest_version": "1.0",
        "registry_sha256": registry["registry_sha256"],
        "fixture_catalog_sha256": fixture_catalog["catalog_sha256"],
        "oracle_catalog_sha256": oracle_catalog["catalog_sha256"],
        "test_variants": manifest_rows,
    }
    manifest["manifest_sha256"] = digest(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    )
    _write("verification-manifest_v1.0.json", manifest)

    print(json.dumps({
        "verification_variants": len(VARIANTS),
        "registry_stages": list(REGISTRY_STAGES),
        "registry_sha256": registry["registry_sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
