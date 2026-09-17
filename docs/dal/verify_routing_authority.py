#!/usr/bin/env python3
"""Verify the routing handoff machine contract against a frozen authority.

The authority freezes the thirteen routing oracles, the request/response schema
bytes, and the registry semantics, so a coherent generator edit cannot silently
redefine the classifier's outcome while keeping its own hashes internally
consistent.  It is never written by ``build_routing_manifests.py``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from dal_jcs import canonical_bytes


ROOT = Path(__file__).resolve().parent
DEFAULT_DIR = ROOT / "manifests"
DEFAULT_AUTHORITY = DEFAULT_DIR / "routing-authority_v1.0.json"

AUTHORITY_KEYS = {
    "schema_version", "authority_version", "source_boundary",
    "frozen_request_schema_sha256", "frozen_response_schema_sha256",
    "frozen_registry_sha256", "frozen_fixture_catalog_sha256",
    "frozen_oracle_catalog_sha256", "frozen_manifest_sha256",
    "semantic_counts", "oracle_entries", "authority_sha256",
}

GENERATED_ROOT_FIELDS = {
    "request_schema": {"$id", "$schema", "$comment", "schema_version", "type",
                       "additionalProperties", "required", "properties"},
    "response_schema": {"$id", "$schema", "$comment", "schema_version", "type",
                        "additionalProperties", "required", "properties"},
    "registry": {"schema_version", "operation_spec_id", "command_type",
                 "contract_version", "service_actor", "evidence_source",
                 "feature_transition_receipt_schema", "slot_names", "slot_fields",
                 "work_state_fields", "classifier_observed_fields", "fact_fields",
                 "injected_fields", "fallback_allowed_classes", "failure_to_reason",
                 "reason_to_state", "result_status_enum", "classification_precedence",
                 "block_event", "block_write_set", "registry_sha256"},
    "fixtures": {"schema_version", "registry_sha256", "fixtures", "catalog_sha256"},
    "oracles": {"schema_version", "registry_sha256", "oracles", "catalog_sha256"},
    "manifest": {"schema_version", "manifest_version", "registry_sha256",
                 "fixture_catalog_sha256", "oracle_catalog_sha256",
                 "test_variants", "manifest_sha256"},
}

ORACLE_FIELDS = {
    "schema_version", "test_id", "variant_id", "run_gate", "pre_state",
    "expected_result_status", "expected_failure_class", "expected_handoff_state",
    "expected_state_trace", "expected_event_trace", "expected_receipts",
    "expected_external_effect_trace", "expected_final_snapshot",
    "allowed_write_set", "forbidden_side_effects", "coverage_ref",
}

#: The dimensions the authority re-derives per oracle.  Like the coder
#: authority, the routing authority freezes the classifier's own
#: `(result_status, failure_class)` pair plus the preserved handoff state — that
#: pair IS the semantic the oracles pin, and the comparator's generic trace
#: check does not cover it.
ORACLE_ENTRY_FIELDS = (
    "test_id", "variant_id", "pre_state",
    "expected_result_status", "expected_failure_class", "expected_handoff_state",
    "expected_state_trace", "expected_event_trace", "expected_receipts",
    "expected_final_snapshot", "allowed_write_set", "forbidden_side_effects",
    "coverage_ref",
)


class AuthorityVerificationError(ValueError):
    """Generated routing semantics differ from the frozen authority."""


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuthorityVerificationError(f"expected JSON object: {path}")
    return value


def hashed_payload(value: dict, hash_field: str) -> dict:
    payload = copy.deepcopy(value)
    payload.pop(hash_field, None)
    return payload


def expected_oracle_entries(
    manifest: dict, fixtures: dict, oracles: dict,
) -> dict[str, dict]:
    manifest_rows = {
        (row["test_id"], row["variant_id"]): row for row in manifest["test_variants"]
    }
    entries: dict[str, dict] = {}
    for oracle_id, oracle in sorted(oracles["oracles"].items()):
        key = (oracle["test_id"], oracle["variant_id"])
        if key not in manifest_rows:
            raise AuthorityVerificationError(f"oracle variant absent from manifest: {key}")
        row = manifest_rows[key]
        if row["oracle_id"] != oracle_id:
            raise AuthorityVerificationError(f"manifest/oracle id drift: {key}")
        fixture = fixtures["fixtures"].get(row["fixture_ref"])
        if fixture is None:
            raise AuthorityVerificationError(f"oracle fixture absent: {row['fixture_ref']}")
        if set(oracle) != ORACLE_FIELDS:
            raise AuthorityVerificationError(f"oracle field drift: {oracle_id}")
        if oracle["pre_state"] != {
            "entity_type": fixture["pre_state"]["entity_type"],
            "state": fixture["pre_state"]["state"],
            "version": fixture["pre_state"]["version"],
        }:
            raise AuthorityVerificationError(f"oracle/fixture pre-state drift: {oracle_id}")
        entries[oracle_id] = {field: oracle[field] for field in ORACLE_ENTRY_FIELDS}
    return entries


def semantic_counts(registry: dict, entries: dict[str, dict]) -> dict[str, int]:
    def count(predicate) -> int:
        return sum(predicate(entry) for entry in entries.values())

    return {
        "routing_variants": len(entries),
        "fallback_allowed_variants": count(
            lambda e: e["expected_result_status"] == "fallback_allowed"),
        "fallback_denied_variants": count(
            lambda e: e["expected_result_status"] == "fallback_denied"),
        "no_fallback_route_variants": count(
            lambda e: e["expected_result_status"] == "no_fallback_route"),
        "blocked_variants": count(
            lambda e: e["expected_result_status"] == "blocked"),
        "policy_failure_variants": count(
            lambda e: e["expected_failure_class"] == "policy_failure"),
        "contract_failure_variants": count(
            lambda e: e["expected_failure_class"] == "contract_failure"),
        "handoff_state_variants": count(
            lambda e: e["expected_handoff_state"] is not None),
        "zero_write_variants": count(lambda e: not e["allowed_write_set"]),
        "block_event_variants": count(
            lambda e: e["expected_event_trace"] == ["feature.blocked"]),
        "needs_human_variants": count(
            lambda e: e["expected_final_snapshot"]["state"] == "needs_human"),
    }


def _assert_self_hash(value: dict, field: str, label: str) -> None:
    if digest(hashed_payload(value, field)) != value[field]:
        raise AuthorityVerificationError(f"generated {label} self-hash mismatch")


def _verify_loaded(
    authority: dict,
    request_schema: dict, request_schema_bytes: bytes,
    response_schema: dict, response_schema_bytes: bytes,
    registry: dict, fixtures: dict, oracles: dict, manifest: dict,
) -> dict[str, int]:
    if set(authority) != AUTHORITY_KEYS:
        raise AuthorityVerificationError("routing authority root fields are not closed")
    if authority["schema_version"] != "dal.routing-authority/1.0":
        raise AuthorityVerificationError("routing authority schema version mismatch")
    if authority["authority_version"] != "1.0":
        raise AuthorityVerificationError("routing authority version mismatch")
    if authority["source_boundary"] != "static-independent-review-authority; never generated by build_routing_manifests.py":
        raise AuthorityVerificationError("routing authority source boundary mismatch")
    if digest(hashed_payload(authority, "authority_sha256")) != authority["authority_sha256"]:
        raise AuthorityVerificationError("routing authority self-hash mismatch")

    for label, value in (
        ("request_schema", request_schema), ("response_schema", response_schema),
        ("registry", registry), ("fixtures", fixtures), ("oracles", oracles),
        ("manifest", manifest),
    ):
        if set(value) != GENERATED_ROOT_FIELDS[label]:
            raise AuthorityVerificationError(f"generated {label} root fields are not closed")

    if hashlib.sha256(request_schema_bytes).hexdigest() != authority["frozen_request_schema_sha256"]:
        raise AuthorityVerificationError("request schema bytes differ from authority")
    if hashlib.sha256(response_schema_bytes).hexdigest() != authority["frozen_response_schema_sha256"]:
        raise AuthorityVerificationError("response schema bytes differ from authority")

    for value, field, label in (
        (registry, "registry_sha256", "registry"),
        (fixtures, "catalog_sha256", "fixture catalog"),
        (oracles, "catalog_sha256", "oracle catalog"),
        (manifest, "manifest_sha256", "manifest"),
    ):
        _assert_self_hash(value, field, label)

    if authority["frozen_registry_sha256"] != registry["registry_sha256"]:
        raise AuthorityVerificationError("registry hash differs from authority")
    if authority["frozen_fixture_catalog_sha256"] != fixtures["catalog_sha256"]:
        raise AuthorityVerificationError("fixture catalog hash differs from authority")
    if authority["frozen_oracle_catalog_sha256"] != oracles["catalog_sha256"]:
        raise AuthorityVerificationError("oracle catalog hash differs from authority")
    if authority["frozen_manifest_sha256"] != manifest["manifest_sha256"]:
        raise AuthorityVerificationError("manifest hash differs from authority")

    entries = expected_oracle_entries(manifest, fixtures, oracles)
    if authority["oracle_entries"] != entries:
        raise AuthorityVerificationError("oracle entries differ from authority")
    counts = semantic_counts(registry, entries)
    if authority["semantic_counts"] != counts:
        raise AuthorityVerificationError("routing authority denominator mismatch")
    return counts


def verify_authority(directory: Path = DEFAULT_DIR, authority_path: Path = DEFAULT_AUTHORITY) -> dict[str, int]:
    request_path = directory / "routing-request_schema_v1.0.json"
    response_path = directory / "routing-response_schema_v1.0.json"
    return _verify_loaded(
        load_json(authority_path),
        load_json(request_path), request_path.read_bytes(),
        load_json(response_path), response_path.read_bytes(),
        load_json(directory / "routing-registry_v1.0.json"),
        load_json(directory / "routing-fixtures_v1.0.json"),
        load_json(directory / "routing-oracles_v1.0.json"),
        load_json(directory / "routing-manifest_v1.0.json"),
    )


def mutation_self_test(directory: Path = DEFAULT_DIR, authority_path: Path = DEFAULT_AUTHORITY) -> list[str]:
    authority = load_json(authority_path)
    request_path = directory / "routing-request_schema_v1.0.json"
    response_path = directory / "routing-response_schema_v1.0.json"
    source = {
        "request_schema": load_json(request_path),
        "request_schema_bytes": request_path.read_bytes(),
        "response_schema": load_json(response_path),
        "response_schema_bytes": response_path.read_bytes(),
        "registry": load_json(directory / "routing-registry_v1.0.json"),
        "fixtures": load_json(directory / "routing-fixtures_v1.0.json"),
        "oracles": load_json(directory / "routing-oracles_v1.0.json"),
        "manifest": load_json(directory / "routing-manifest_v1.0.json"),
    }

    def rehash(value: dict, field: str) -> None:
        value[field] = digest(hashed_payload(value, field))

    def rehash_chain(values: dict) -> None:
        rehash(values["registry"], "registry_sha256")
        rehash(values["fixtures"], "catalog_sha256")
        rehash(values["oracles"], "catalog_sha256")
        rehash(values["manifest"], "manifest_sha256")

    def oracle(values: dict, variant_id: str) -> dict:
        for o in values["oracles"]["oracles"].values():
            if o["variant_id"] == variant_id:
                return o
        raise AuthorityVerificationError(f"mutation anchor variant missing: {variant_id}")

    def failure_class_mutation(values: dict) -> None:
        oracle(values, "classifier_modified")["expected_failure_class"] = "contract_failure"
        rehash_chain(values)

    def reason_code_mutation(values: dict) -> None:
        oracle(values, "classifier_modified")["expected_final_snapshot"]["reason_code"] = \
            "PROVIDER_CONTRACT_FAILURE"
        rehash_chain(values)

    def oracle_write_mutation(values: dict) -> None:
        oracle(values, "primary_transient")["allowed_write_set"] = ["aggregate"]
        rehash_chain(values)

    def oracle_status_mutation(values: dict) -> None:
        oracle(values, "primary_task_failure")["expected_result_status"] = "fallback_allowed"
        rehash_chain(values)

    def registry_enum_mutation(values: dict) -> None:
        values["registry"]["fallback_allowed_classes"].append("task_failure")
        rehash_chain(values)

    def schema_enum_mutation(values: dict) -> None:
        values["response_schema"]["properties"]["result_status"]["enum"].append(
            "fallback_forced"
        )

    def schema_required_mutation(values: dict) -> None:
        values["request_schema"]["required"].remove("authoritative_facts")

    passed: list[str] = []
    for label, mutate, recompute_bytes in (
        ("failure_class", failure_class_mutation, False),
        ("reason_code", reason_code_mutation, False),
        ("oracle_write", oracle_write_mutation, False),
        ("oracle_status", oracle_status_mutation, False),
        ("registry_enum", registry_enum_mutation, False),
        ("schema_enum", schema_enum_mutation, "response"),
        ("schema_required", schema_required_mutation, "request"),
    ):
        values = copy.deepcopy(source)
        mutate(values)
        request_schema_bytes = values["request_schema_bytes"]
        response_schema_bytes = values["response_schema_bytes"]
        if recompute_bytes == "request":
            request_schema_bytes = (json.dumps(
                values["request_schema"], ensure_ascii=False, sort_keys=True, indent=2
            ) + "\n").encode("utf-8")
        elif recompute_bytes == "response":
            response_schema_bytes = (json.dumps(
                values["response_schema"], ensure_ascii=False, sort_keys=True, indent=2
            ) + "\n").encode("utf-8")
        try:
            _verify_loaded(
                authority,
                values["request_schema"], request_schema_bytes,
                values["response_schema"], response_schema_bytes,
                values["registry"], values["fixtures"], values["oracles"],
                values["manifest"],
            )
        except AuthorityVerificationError:
            passed.append(label)
        else:
            raise AuthorityVerificationError(f"single-sided {label} mutation escaped routing authority")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY)
    parser.add_argument("--mutation-self-test", action="store_true")
    args = parser.parse_args()
    counts = verify_authority(args.directory, args.authority)
    mutations = mutation_self_test(args.directory, args.authority) if args.mutation_self_test else []
    print(json.dumps({"authority": "PASS", "counts": counts, "mutations_rejected": mutations}, sort_keys=True))


if __name__ == "__main__":
    main()
