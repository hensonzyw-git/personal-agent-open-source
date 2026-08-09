#!/usr/bin/env python3
"""Verify operation contracts against a separately frozen semantic authority.

The authority is never written by ``build_contract_manifests.py``.  It freezes
the complete operation spec plus every resolver-visible command and expected
result so a coherent generator edit cannot silently redefine the business
operation while keeping its generated hashes internally consistent.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from dal_jcs import canonical_bytes


ROOT = Path(__file__).resolve().parent
DEFAULT_GENERATED_DIR = ROOT / "manifests"
DEFAULT_AUTHORITY = DEFAULT_GENERATED_DIR / "operation-oracle-authority_v1.0.json"
AUTHORITY_KEYS = {
    "schema_version",
    "authority_version",
    "source_boundary",
    "frozen_operation_registry_sha256",
    "frozen_evidence_registry_sha256",
    "frozen_guard_registry_sha256",
    "semantic_counts",
    "operation_specs",
    "entries",
    "authority_sha256",
}
ORACLE_RESULT_FIELDS = {
    "schema_version", "test_id", "variant_id", "run_gate", "pre_state",
    "injection_operation", "expected_state_trace", "expected_event_trace",
    "expected_receipts", "expected_external_effect_trace",
    "expected_final_snapshot", "expected_related_snapshots", "allowed_write_set",
    "forbidden_side_effects", "expected_atomic_companion_transitions",
    "scenario_assertions", "coverage_ref",
}
GENERATED_ROOT_FIELDS = {
    "operation": {"schema_version", "resolver_input_contract", "operation_specs", "registry_sha256"},
    "evidence": {"schema_version", "evidence_schemas", "registry_sha256"},
    "guard": {"schema_version", "guards", "registry_sha256"},
    "manifest": {
        "schema_version", "manifest_version", "transition_registry_sha256",
        "evidence_registry_sha256", "guard_registry_sha256", "operation_registry_sha256",
        "test_variants", "manifest_sha256",
    },
    "fixtures": {
        "schema_version", "transition_registry_sha256", "evidence_registry_sha256",
        "guard_registry_sha256", "operation_registry_sha256", "fixtures", "catalog_sha256",
    },
    "oracles": {
        "schema_version", "transition_registry_sha256", "evidence_registry_sha256",
        "guard_registry_sha256", "operation_registry_sha256", "oracles", "catalog_sha256",
    },
}


class AuthorityVerificationError(ValueError):
    """Generated operation semantics differ from the frozen authority."""


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


def expected_specs(operation_registry: dict) -> dict[str, dict]:
    return {
        row["operation_spec_id"]: row
        for row in operation_registry["operation_specs"]
    }


def expected_entries(
    operation_registry: dict,
    manifest: dict,
    fixtures: dict,
    oracles: dict,
) -> dict[str, dict]:
    manifest_rows = {
        (row["test_id"], row["variant_id"]): row
        for row in manifest["test_variants"]
    }
    entries: dict[str, dict] = {}
    for spec in sorted(operation_registry["operation_specs"], key=lambda row: row["operation_spec_id"]):
        test_id = "DAL-T-" + spec["operation_spec_id"].removeprefix("OP-")
        for variant, commands in sorted(spec["variant_input_contracts"].items()):
            key = (test_id, variant)
            if key not in manifest_rows:
                raise AuthorityVerificationError(f"operation variant absent from manifest: {key}")
            row = manifest_rows[key]
            fixture = fixtures["fixtures"].get(row["fixture_ref"])
            oracle = oracles["oracles"].get(row["oracle_id"])
            if fixture is None or oracle is None:
                raise AuthorityVerificationError(f"operation fixture/oracle absent: {key}")
            if fixture.get("resolver_sequence_kind") != "operation_commands":
                raise AuthorityVerificationError(f"operation resolver kind drift: {key}")
            if fixture.get("operation_sequence") != commands:
                raise AuthorityVerificationError(f"operation registry/fixture input drift: {key}")
            if set(oracle) != ORACLE_RESULT_FIELDS:
                raise AuthorityVerificationError(f"operation oracle field drift: {row['oracle_id']}")
            if not commands:
                raise AuthorityVerificationError(f"empty operation resolver input: {key}")
            entries[row["oracle_id"]] = {
                "operation_spec_id": spec["operation_spec_id"],
                "command_type": spec["command_type"],
                "fixture_ref": row["fixture_ref"],
                "pre_state": fixture["pre_state"],
                "resolver_input": {
                    "kind": "single" if len(commands) == 1 else "sequence",
                    "commands": commands,
                },
                "evidence_registry_sha256": fixture["evidence_registry_sha256"],
                "guard_registry_sha256": fixture["guard_registry_sha256"],
                "expected_result": oracle,
            }
    return entries


def semantic_counts(operation_registry: dict, entries: dict[str, dict]) -> dict[str, int]:
    specs = operation_registry["operation_specs"]
    return {
        "operation_specs": len(specs),
        "operation_oracle_entries": len(entries),
        "resolver_command_objects": sum(len(entry["resolver_input"]["commands"]) for entry in entries.values()),
        "single_command_entries": sum(entry["resolver_input"]["kind"] == "single" for entry in entries.values()),
        "sequence_entries": sum(entry["resolver_input"]["kind"] == "sequence" for entry in entries.values()),
        "common_input_specs": sum(spec["input_schema_version"] == "dal.operation-input/1.0" for spec in specs),
        "complete_pre_state_command_result_entries": sum(
            bool(entry["pre_state"])
            and bool(entry["resolver_input"]["commands"])
            and bool(entry["expected_result"])
            for entry in entries.values()
        ),
    }


def _assert_self_hash(value: dict, field: str, label: str) -> None:
    if digest(hashed_payload(value, field)) != value[field]:
        raise AuthorityVerificationError(f"generated {label} self-hash mismatch")


def _verify_loaded(
    authority: dict,
    operation_registry: dict,
    evidence_registry: dict,
    guard_registry: dict,
    manifest: dict,
    fixtures: dict,
    oracles: dict,
) -> dict[str, int]:
    if set(authority) != AUTHORITY_KEYS:
        raise AuthorityVerificationError("operation authority root fields are not closed")
    if authority["schema_version"] != "dal.operation-oracle-authority/1.0":
        raise AuthorityVerificationError("operation authority schema version mismatch")
    if authority["authority_version"] != "1.0":
        raise AuthorityVerificationError("operation authority version mismatch")
    if authority["source_boundary"] != "static-independent-review-authority; never generated by build_contract_manifests.py":
        raise AuthorityVerificationError("operation authority source boundary mismatch")
    if digest(hashed_payload(authority, "authority_sha256")) != authority["authority_sha256"]:
        raise AuthorityVerificationError("operation authority self-hash mismatch")

    for label, value in (
        ("operation", operation_registry), ("evidence", evidence_registry),
        ("guard", guard_registry), ("manifest", manifest),
        ("fixtures", fixtures), ("oracles", oracles),
    ):
        if set(value) != GENERATED_ROOT_FIELDS[label]:
            raise AuthorityVerificationError(f"generated {label} root fields are not closed")

    for value, field, label in (
        (operation_registry, "registry_sha256", "operation registry"),
        (evidence_registry, "registry_sha256", "evidence registry"),
        (guard_registry, "registry_sha256", "guard registry"),
        (manifest, "manifest_sha256", "manifest"),
        (fixtures, "catalog_sha256", "fixture catalog"),
        (oracles, "catalog_sha256", "oracle catalog"),
    ):
        _assert_self_hash(value, field, label)

    frozen = (
        ("frozen_operation_registry_sha256", operation_registry["registry_sha256"], "operation"),
        ("frozen_evidence_registry_sha256", evidence_registry["registry_sha256"], "evidence"),
        ("frozen_guard_registry_sha256", guard_registry["registry_sha256"], "guard"),
    )
    for authority_field, generated_hash, label in frozen:
        if authority[authority_field] != generated_hash:
            raise AuthorityVerificationError(f"{label} registry hash differs from operation authority")

    specs = expected_specs(operation_registry)
    if authority["operation_specs"] != specs:
        raise AuthorityVerificationError("operation routing/spec semantics differ from independent authority")
    entries = expected_entries(operation_registry, manifest, fixtures, oracles)
    if authority["entries"] != entries:
        raise AuthorityVerificationError("operation input/expected-result semantics differ from independent authority")
    counts = semantic_counts(operation_registry, entries)
    if authority["semantic_counts"] != counts:
        raise AuthorityVerificationError("operation authority denominator mismatch")
    if counts["complete_pre_state_command_result_entries"] != counts["operation_oracle_entries"]:
        raise AuthorityVerificationError("not every operation freezes complete input and result")
    return counts


def verify_authority(generated_dir: Path, authority_path: Path = DEFAULT_AUTHORITY) -> dict[str, int]:
    return _verify_loaded(
        load_json(authority_path),
        load_json(generated_dir / "operation-spec-registry_v1.0.json"),
        load_json(generated_dir / "evidence-schema-registry_v1.0.json"),
        load_json(generated_dir / "guard-predicate-registry_v1.0.json"),
        load_json(generated_dir / "test-manifest_v1.2.json"),
        load_json(generated_dir / "test-fixtures_v1.0.json"),
        load_json(generated_dir / "test-oracles_v1.0.json"),
    )


def mutation_self_test(generated_dir: Path, authority_path: Path = DEFAULT_AUTHORITY) -> list[str]:
    authority = load_json(authority_path)
    source = {
        "operation": load_json(generated_dir / "operation-spec-registry_v1.0.json"),
        "evidence": load_json(generated_dir / "evidence-schema-registry_v1.0.json"),
        "guard": load_json(generated_dir / "guard-predicate-registry_v1.0.json"),
        "manifest": load_json(generated_dir / "test-manifest_v1.2.json"),
        "fixtures": load_json(generated_dir / "test-fixtures_v1.0.json"),
        "oracles": load_json(generated_dir / "test-oracles_v1.0.json"),
    }
    first_spec = source["operation"]["operation_specs"][0]
    first_variant = sorted(first_spec["variant_input_contracts"])[0]
    test_id = "DAL-T-" + first_spec["operation_spec_id"].removeprefix("OP-")

    def command_mutation(values: dict) -> None:
        values["operation"]["operation_specs"][0]["command_type"] += "__mutated"

    def input_mutation(values: dict) -> None:
        spec = values["operation"]["operation_specs"][0]
        commands = spec["variant_input_contracts"][first_variant]
        commands[0]["input"]["action_sequence"][0]["command"] += "__mutated"
        for fixture in values["fixtures"]["fixtures"].values():
            if fixture.get("test_id") == test_id and fixture.get("variant_id") == first_variant:
                fixture["operation_sequence"] = copy.deepcopy(commands)

    def receipt_mutation(values: dict) -> None:
        for oracle in values["oracles"]["oracles"].values():
            if oracle["test_id"] == test_id and oracle["variant_id"] == first_variant:
                oracle["expected_receipts"][0]["code"] += "__mutated"
                return

    def write_set_mutation(values: dict) -> None:
        spec = values["operation"]["operation_specs"][0]
        spec["atomic_write_sets_by_variant"][first_variant].append("mutated_write")
        for oracle in values["oracles"]["oracles"].values():
            if oracle["test_id"] == test_id and oracle["variant_id"] == first_variant:
                oracle["allowed_write_set"].append("mutated_write")
                return

    def evidence_mutation(values: dict) -> None:
        values["evidence"]["evidence_schemas"][0]["json_schema"]["properties"]["mutated"] = {"type": "string"}

    def guard_mutation(values: dict) -> None:
        values["guard"]["guards"][0]["all_of"].append({"field": "mutated", "operator": "equals", "value": True})

    def catalog_root_mutation(values: dict, key: str) -> None:
        catalog = values[key]
        catalog["x-unapproved"] = True
        catalog["catalog_sha256"] = digest(hashed_payload(catalog, "catalog_sha256"))

    passed: list[str] = []
    for label, mutate in (
        ("command", command_mutation),
        ("input", input_mutation),
        ("receipt", receipt_mutation),
        ("write_set", write_set_mutation),
        ("evidence", evidence_mutation),
        ("guard", guard_mutation),
        ("fixture_root_extra", lambda values: catalog_root_mutation(values, "fixtures")),
        ("oracle_root_extra", lambda values: catalog_root_mutation(values, "oracles")),
    ):
        values = copy.deepcopy(source)
        mutate(values)
        try:
            _verify_loaded(
                authority, values["operation"], values["evidence"], values["guard"],
                values["manifest"], values["fixtures"], values["oracles"],
            )
        except AuthorityVerificationError:
            passed.append(label)
        else:
            raise AuthorityVerificationError(f"single-sided {label} mutation escaped operation authority")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated-dir", type=Path, default=DEFAULT_GENERATED_DIR)
    parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY)
    parser.add_argument("--mutation-self-test", action="store_true")
    args = parser.parse_args()
    counts = verify_authority(args.generated_dir, args.authority)
    mutations = mutation_self_test(args.generated_dir, args.authority) if args.mutation_self_test else []
    print(json.dumps({"authority": "PASS", "counts": counts, "mutations_rejected": mutations}, sort_keys=True))


if __name__ == "__main__":
    main()
