#!/usr/bin/env python3
"""Verify the controller-dispatch machine contract against a frozen authority.

The authority freezes the dispatch-table rows, the fifteen graph oracles, and
the schema bytes, so a coherent generator edit cannot silently redefine the
controller routing while keeping its own hashes internally consistent.  It is
never written by ``build_controller_dispatch_manifests.py``.
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
DEFAULT_AUTHORITY = DEFAULT_DIR / "controller-dispatch-authority_v1.0.json"

AUTHORITY_KEYS = {
    "schema_version", "authority_version", "source_boundary",
    "frozen_schema_sha256", "frozen_registry_sha256",
    "frozen_transition_registry_sha256", "frozen_fixture_catalog_sha256",
    "frozen_oracle_catalog_sha256", "frozen_manifest_sha256",
    "semantic_counts", "dispatch_rows", "oracle_entries", "authority_sha256",
}

GENERATED_ROOT_FIELDS = {
    "schema": {"schema_version", "title", "type", "additionalProperties", "required", "properties"},
    "registry": {"schema_version", "transition_registry_sha256", "node_taxonomy",
                 "adapter_seam", "dispatch_rows", "universal_routes",
                 "block_feature_routes", "registry_sha256"},
    "fixtures": {"schema_version", "transition_registry_sha256", "fixtures", "catalog_sha256"},
    "oracles": {"schema_version", "transition_registry_sha256", "oracles", "catalog_sha256"},
    "manifest": {"schema_version", "manifest_version", "registry_sha256",
                 "transition_registry_sha256", "fixture_catalog_sha256",
                 "oracle_catalog_sha256", "test_variants", "manifest_sha256"},
}

ORACLE_FIELDS = {
    "schema_version", "test_id", "variant_id", "run_gate", "pre_state",
    "injection_operation", "expected_dispatch", "expected_state_trace",
    "expected_event_trace", "expected_receipts", "expected_external_effect_trace",
    "expected_final_snapshot", "expected_related_snapshots", "allowed_write_set",
    "forbidden_side_effects", "expected_atomic_companion_transitions",
    "scenario_assertions", "coverage_ref",
}


class AuthorityVerificationError(ValueError):
    """Generated controller-dispatch semantics differ from the frozen authority."""


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


def expected_dispatch_rows(registry: dict) -> dict[str, dict]:
    rows = {}
    for row in registry["dispatch_rows"]:
        state = row["state"]
        if state in rows:
            raise AuthorityVerificationError(f"duplicate dispatch state: {state}")
        rows[state] = {
            "node_type": row["node_type"],
            "orchestration_action": row["orchestration_action"],
            "resulting_commands": row["resulting_commands"],
        }
    return rows


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
        entries[oracle_id] = {
            "test_id": oracle["test_id"],
            "variant_id": oracle["variant_id"],
            "pre_state": oracle["pre_state"],
            "expected_dispatch": oracle["expected_dispatch"],
            "expected_state_trace": oracle["expected_state_trace"],
            "expected_event_trace": oracle["expected_event_trace"],
            "expected_receipts": oracle["expected_receipts"],
            "expected_final_snapshot": oracle["expected_final_snapshot"],
            "allowed_write_set": oracle["allowed_write_set"],
            "forbidden_side_effects": oracle["forbidden_side_effects"],
            "coverage_ref": oracle["coverage_ref"],
        }
    return entries


def semantic_counts(registry: dict, entries: dict[str, dict]) -> dict[str, int]:
    rows = registry["dispatch_rows"]
    node_types = [row["node_type"] for row in rows]
    return {
        "dispatch_rows": len(rows),
        "universal_routes": len(registry["universal_routes"]),
        "block_feature_routes": len(registry["block_feature_routes"]),
        "graph_variants": len(entries),
        "provider_nodes": node_types.count("provider"),
        "deterministic_nodes": node_types.count("deterministic"),
        "gate_nodes": node_types.count("gate"),
        "terminal_nodes": node_types.count("terminal"),
        "round_1_variants": sum(
            entry["expected_dispatch"]["round"] == 1 for entry in entries.values()
        ),
        "round_2_variants": sum(
            entry["expected_dispatch"]["round"] == 2 for entry in entries.values()
        ),
        "zero_write_variants": sum(
            not entry["allowed_write_set"] for entry in entries.values()
        ),
        "block_variants": sum(
            entry["expected_final_snapshot"]["state"] == "needs_human"
            for entry in entries.values()
        ),
    }


def _assert_self_hash(value: dict, field: str, label: str) -> None:
    if digest(hashed_payload(value, field)) != value[field]:
        raise AuthorityVerificationError(f"generated {label} self-hash mismatch")


def _verify_loaded(
    authority: dict,
    schema: dict, schema_bytes: bytes,
    registry: dict, fixtures: dict, oracles: dict, manifest: dict,
) -> dict[str, int]:
    if set(authority) != AUTHORITY_KEYS:
        raise AuthorityVerificationError("dispatch authority root fields are not closed")
    if authority["schema_version"] != "dal.controller-dispatch-authority/1.0":
        raise AuthorityVerificationError("dispatch authority schema version mismatch")
    if authority["authority_version"] != "1.0":
        raise AuthorityVerificationError("dispatch authority version mismatch")
    if authority["source_boundary"] != "static-independent-review-authority; never generated by build_controller_dispatch_manifests.py":
        raise AuthorityVerificationError("dispatch authority source boundary mismatch")
    if digest(hashed_payload(authority, "authority_sha256")) != authority["authority_sha256"]:
        raise AuthorityVerificationError("dispatch authority self-hash mismatch")

    for label, value in (
        ("schema", schema), ("registry", registry), ("fixtures", fixtures),
        ("oracles", oracles), ("manifest", manifest),
    ):
        if set(value) != GENERATED_ROOT_FIELDS[label]:
            raise AuthorityVerificationError(f"generated {label} root fields are not closed")

    if hashlib.sha256(schema_bytes).hexdigest() != authority["frozen_schema_sha256"]:
        raise AuthorityVerificationError("dispatch schema bytes differ from authority")

    for value, field, label in (
        (registry, "registry_sha256", "registry"),
        (fixtures, "catalog_sha256", "fixture catalog"),
        (oracles, "catalog_sha256", "oracle catalog"),
        (manifest, "manifest_sha256", "manifest"),
    ):
        _assert_self_hash(value, field, label)

    if authority["frozen_registry_sha256"] != registry["registry_sha256"]:
        raise AuthorityVerificationError("registry hash differs from authority")
    if authority["frozen_transition_registry_sha256"] != registry["transition_registry_sha256"]:
        raise AuthorityVerificationError("transition registry hash differs from authority")
    if authority["frozen_fixture_catalog_sha256"] != fixtures["catalog_sha256"]:
        raise AuthorityVerificationError("fixture catalog hash differs from authority")
    if authority["frozen_oracle_catalog_sha256"] != oracles["catalog_sha256"]:
        raise AuthorityVerificationError("oracle catalog hash differs from authority")
    if authority["frozen_manifest_sha256"] != manifest["manifest_sha256"]:
        raise AuthorityVerificationError("manifest hash differs from authority")

    rows = expected_dispatch_rows(registry)
    if authority["dispatch_rows"] != rows:
        raise AuthorityVerificationError("dispatch rows differ from authority")
    entries = expected_oracle_entries(manifest, fixtures, oracles)
    if authority["oracle_entries"] != entries:
        raise AuthorityVerificationError("oracle entries differ from authority")
    counts = semantic_counts(registry, entries)
    if authority["semantic_counts"] != counts:
        raise AuthorityVerificationError("dispatch authority denominator mismatch")
    return counts


def verify_authority(directory: Path = DEFAULT_DIR, authority_path: Path = DEFAULT_AUTHORITY) -> dict[str, int]:
    schema_path = directory / "controller-dispatch_schema_v1.0.json"
    return _verify_loaded(
        load_json(authority_path),
        load_json(schema_path), schema_path.read_bytes(),
        load_json(directory / "controller-dispatch-registry_v1.0.json"),
        load_json(directory / "controller-dispatch-fixtures_v1.0.json"),
        load_json(directory / "controller-dispatch-oracles_v1.0.json"),
        load_json(directory / "controller-dispatch-manifest_v1.0.json"),
    )


def mutation_self_test(directory: Path = DEFAULT_DIR, authority_path: Path = DEFAULT_AUTHORITY) -> list[str]:
    authority = load_json(authority_path)
    schema_path = directory / "controller-dispatch_schema_v1.0.json"
    source = {
        "schema": load_json(schema_path),
        "schema_bytes": schema_path.read_bytes(),
        "registry": load_json(directory / "controller-dispatch-registry_v1.0.json"),
        "fixtures": load_json(directory / "controller-dispatch-fixtures_v1.0.json"),
        "oracles": load_json(directory / "controller-dispatch-oracles_v1.0.json"),
        "manifest": load_json(directory / "controller-dispatch-manifest_v1.0.json"),
    }

    def rehash(value: dict, field: str) -> None:
        value[field] = digest(hashed_payload(value, field))

    def rehash_chain(values: dict) -> None:
        rehash(values["registry"], "registry_sha256")
        rehash(values["fixtures"], "catalog_sha256")
        rehash(values["oracles"], "catalog_sha256")
        rehash(values["manifest"], "manifest_sha256")

    def node_type_mutation(values: dict) -> None:
        values["registry"]["dispatch_rows"][1]["node_type"] = "deterministic"

    def command_mutation(values: dict) -> None:
        values["registry"]["dispatch_rows"][1]["resulting_commands"][0]["command_type"] = "record_review/pass"
        rehash_chain(values)

    def oracle_write_mutation(values: dict) -> None:
        for oracle in values["oracles"]["oracles"].values():
            if oracle["variant_id"] == "provider_node_empty_response":
                oracle["allowed_write_set"] = ["aggregate"]
                rehash_chain(values)
                return

    def oracle_round_mutation(values: dict) -> None:
        for oracle in values["oracles"]["oracles"].values():
            if oracle["variant_id"] == "round_dispatch_first":
                oracle["expected_dispatch"]["round"] = 2
                rehash_chain(values)
                return

    def schema_enum_mutation(values: dict) -> None:
        values["schema"]["properties"]["dispatch_rows"]["items"]["properties"]["node_type"]["enum"].append("mystery")

    def schema_required_mutation(values: dict) -> None:
        values["schema"]["required"].remove("universal_routes")

    passed: list[str] = []
    for label, mutate, recompute_bytes in (
        ("node_type", node_type_mutation, False),
        ("command", command_mutation, False),
        ("oracle_write", oracle_write_mutation, False),
        ("oracle_round", oracle_round_mutation, False),
        ("schema_enum", schema_enum_mutation, True),
        ("schema_required", schema_required_mutation, True),
    ):
        values = copy.deepcopy(source)
        mutate(values)
        schema_bytes = values["schema_bytes"]
        if recompute_bytes:
            schema_bytes = (json.dumps(values["schema"], ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        try:
            _verify_loaded(
                authority, values["schema"], schema_bytes, values["registry"],
                values["fixtures"], values["oracles"], values["manifest"],
            )
        except AuthorityVerificationError:
            passed.append(label)
        else:
            raise AuthorityVerificationError(f"single-sided {label} mutation escaped dispatch authority")
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
