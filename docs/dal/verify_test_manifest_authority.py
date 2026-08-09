#!/usr/bin/env python3
"""Verify the complete DAL test manifest against a static external authority."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

from dal_jcs import canonical_bytes


ROOT = Path(__file__).resolve().parent
DEFAULT_GENERATED_DIR = ROOT / "manifests"
DEFAULT_AUTHORITY = DEFAULT_GENERATED_DIR / "test-manifest-authority_v1.0.json"
AUTHORITY_KEYS = {
    "schema_version", "authority_version", "source_boundary",
    "frozen_manifest_sha256", "semantic_counts", "manifest_rows", "authority_sha256",
}
ROW_FIELDS = {
    "test_id", "variant_id", "injection_schema_version", "injection_point",
    "fixture_schema_version", "fixture_ref", "fixture_sha256",
    "oracle_schema_version", "oracle_id", "oracle_sha256",
    "expected_entity_type", "expected_entity_state", "expected_reason_owner",
    "expected_reason_code", "expected_receipt_schema", "expected_receipt_code",
    "expected_effect_state", "owner_tasks", "run_gate",
}
HASHED_ARTIFACTS = (
    ("transition-spec-registry_v1.0.json", "registry_sha256"),
    ("evidence-schema-registry_v1.0.json", "registry_sha256"),
    ("guard-predicate-registry_v1.0.json", "registry_sha256"),
    ("operation-spec-registry_v1.0.json", "registry_sha256"),
    ("test-fixtures_v1.0.json", "catalog_sha256"),
    ("test-oracles_v1.0.json", "catalog_sha256"),
    ("test-manifest_v1.2.json", "manifest_sha256"),
)
ARTIFACT_ROOT_FIELDS = {
    "transition-spec-registry_v1.0.json": {"schema_version", "specs", "registry_sha256"},
    "evidence-schema-registry_v1.0.json": {"schema_version", "evidence_schemas", "registry_sha256"},
    "guard-predicate-registry_v1.0.json": {"schema_version", "guards", "registry_sha256"},
    "operation-spec-registry_v1.0.json": {
        "schema_version", "resolver_input_contract", "operation_specs", "registry_sha256",
    },
    "test-fixtures_v1.0.json": {
        "schema_version", "transition_registry_sha256", "evidence_registry_sha256",
        "guard_registry_sha256", "operation_registry_sha256", "fixtures", "catalog_sha256",
    },
    "test-oracles_v1.0.json": {
        "schema_version", "transition_registry_sha256", "evidence_registry_sha256",
        "guard_registry_sha256", "operation_registry_sha256", "oracles", "catalog_sha256",
    },
    "test-manifest_v1.2.json": {
        "schema_version", "manifest_version", "transition_registry_sha256",
        "evidence_registry_sha256", "guard_registry_sha256", "operation_registry_sha256",
        "test_variants", "manifest_sha256",
    },
}


class AuthorityVerificationError(ValueError):
    """The generated manifest differs from the frozen complete authority."""


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


def _assert_self_hash(value: dict, field: str, label: str) -> None:
    if digest(hashed_payload(value, field)) != value[field]:
        raise AuthorityVerificationError(f"generated {label} self-hash mismatch")


def _row_key(row: dict) -> str:
    return f"{row['test_id']}/{row['variant_id']}"


def expected_manifest_rows(manifest: dict, fixtures: dict, oracles: dict) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    fixture_refs: set[str] = set()
    oracle_ids: set[str] = set()
    for row in manifest["test_variants"]:
        if set(row) != ROW_FIELDS:
            raise AuthorityVerificationError(f"manifest row fields are not closed: {_row_key(row)}")
        key = _row_key(row)
        if key in rows:
            raise AuthorityVerificationError(f"duplicate manifest test/variant: {key}")
        fixture = fixtures["fixtures"].get(row["fixture_ref"])
        oracle = oracles["oracles"].get(row["oracle_id"])
        if fixture is None or oracle is None:
            raise AuthorityVerificationError(f"missing manifest fixture/oracle: {key}")
        if row["fixture_ref"] in fixture_refs or row["oracle_id"] in oracle_ids:
            raise AuthorityVerificationError(f"fixture/oracle reused by multiple manifest rows: {key}")
        fixture_refs.add(row["fixture_ref"])
        oracle_ids.add(row["oracle_id"])
        if row["fixture_sha256"] != digest(fixture) or row["oracle_sha256"] != digest(oracle):
            raise AuthorityVerificationError(f"fixture/oracle digest mismatch: {key}")

        receipts = oracle["expected_receipts"]
        first_receipt = receipts[0] if receipts else None
        effect_trace = oracle["expected_external_effect_trace"]
        expected_projection = {
            "test_id": oracle["test_id"],
            "variant_id": oracle["variant_id"],
            "injection_schema_version": fixture["injection_operation"]["schema_version"],
            "injection_point": fixture["injection_operation"]["injection_point"],
            "fixture_schema_version": fixture["schema_version"],
            "oracle_schema_version": oracle["schema_version"],
            "expected_entity_type": oracle["expected_final_snapshot"]["entity_type"],
            "expected_entity_state": oracle["expected_final_snapshot"]["state"],
            "expected_reason_owner": oracle["expected_final_snapshot"]["reason_owner"],
            "expected_reason_code": oracle["expected_final_snapshot"]["reason_code"],
            "expected_receipt_schema": first_receipt["schema_version"] if first_receipt else None,
            "expected_receipt_code": first_receipt["code"] if first_receipt else None,
            "expected_effect_state": effect_trace[-1] if effect_trace else None,
            "run_gate": oracle["run_gate"],
        }
        for field, expected in expected_projection.items():
            if row[field] != expected:
                raise AuthorityVerificationError(f"manifest/oracle projection mismatch: {key}/{field}")
        if fixture["test_id"] != row["test_id"] or fixture["variant_id"] != row["variant_id"]:
            raise AuthorityVerificationError(f"manifest/fixture identity mismatch: {key}")
        if fixture["run_gate"] != row["run_gate"] or fixture["injection_operation"] != oracle["injection_operation"]:
            raise AuthorityVerificationError(f"manifest fixture/oracle gate or injection mismatch: {key}")
        if not row["owner_tasks"] or len(row["owner_tasks"]) != len(set(row["owner_tasks"])):
            raise AuthorityVerificationError(f"manifest owner task set invalid: {key}")
        rows[key] = row

    if fixture_refs != set(fixtures["fixtures"]) or oracle_ids != set(oracles["oracles"]):
        raise AuthorityVerificationError("manifest does not bijectively cover fixture/oracle catalogs")
    return rows


def semantic_counts(rows: dict[str, dict]) -> dict[str, object]:
    gate_counts = Counter(row["run_gate"] for row in rows.values())
    owner_counts = Counter(owner for row in rows.values() for owner in row["owner_tasks"])
    return {
        "manifest_rows": len(rows),
        "unique_fixture_refs": len({row["fixture_ref"] for row in rows.values()}),
        "unique_oracle_ids": len({row["oracle_id"] for row in rows.values()}),
        "run_gate_counts": dict(sorted(gate_counts.items())),
        "owner_task_memberships": dict(sorted(owner_counts.items())),
        "complete_expected_projection_rows": sum(
            all(field in row for field in (
                "expected_entity_type", "expected_entity_state", "expected_reason_owner",
                "expected_reason_code", "expected_receipt_schema", "expected_receipt_code",
                "expected_effect_state",
            ))
            for row in rows.values()
        ),
    }


def _verify_loaded(authority: dict, artifacts: dict[str, dict]) -> dict[str, object]:
    if set(authority) != AUTHORITY_KEYS:
        raise AuthorityVerificationError("manifest authority root fields are not closed")
    if authority["schema_version"] != "dal.test-manifest-authority/1.0" or authority["authority_version"] != "1.0":
        raise AuthorityVerificationError("manifest authority version mismatch")
    if authority["source_boundary"] != "static-independent-review-authority; never generated by build_contract_manifests.py":
        raise AuthorityVerificationError("manifest authority source boundary mismatch")
    if digest(hashed_payload(authority, "authority_sha256")) != authority["authority_sha256"]:
        raise AuthorityVerificationError("manifest authority self-hash mismatch")
    for filename, hash_field in HASHED_ARTIFACTS:
        if set(artifacts[filename]) != ARTIFACT_ROOT_FIELDS[filename]:
            raise AuthorityVerificationError(f"generated artifact root fields are not closed: {filename}")
        _assert_self_hash(artifacts[filename], hash_field, filename)

    transition = artifacts["transition-spec-registry_v1.0.json"]
    evidence = artifacts["evidence-schema-registry_v1.0.json"]
    guard = artifacts["guard-predicate-registry_v1.0.json"]
    operation = artifacts["operation-spec-registry_v1.0.json"]
    fixtures = artifacts["test-fixtures_v1.0.json"]
    oracles = artifacts["test-oracles_v1.0.json"]
    manifest = artifacts["test-manifest_v1.2.json"]
    expected_registry_hashes = {
        "transition_registry_sha256": transition["registry_sha256"],
        "evidence_registry_sha256": evidence["registry_sha256"],
        "guard_registry_sha256": guard["registry_sha256"],
        "operation_registry_sha256": operation["registry_sha256"],
    }
    for field, expected in expected_registry_hashes.items():
        if manifest[field] != expected or fixtures[field] != expected or oracles[field] != expected:
            raise AuthorityVerificationError(f"cross-artifact registry reference mismatch: {field}")
    if authority["frozen_manifest_sha256"] != manifest["manifest_sha256"]:
        raise AuthorityVerificationError("manifest hash differs from independent authority")
    rows = expected_manifest_rows(manifest, fixtures, oracles)
    if authority["manifest_rows"] != rows:
        raise AuthorityVerificationError("complete manifest rows differ from independent authority")
    counts = semantic_counts(rows)
    if authority["semantic_counts"] != counts:
        raise AuthorityVerificationError("manifest authority denominator mismatch")
    if counts["complete_expected_projection_rows"] != counts["manifest_rows"]:
        raise AuthorityVerificationError("not every manifest row freezes a complete expected projection")
    return counts


def load_artifacts(generated_dir: Path) -> dict[str, dict]:
    return {filename: load_json(generated_dir / filename) for filename, _ in HASHED_ARTIFACTS}


def verify_authority(generated_dir: Path, authority_path: Path = DEFAULT_AUTHORITY) -> dict[str, object]:
    return _verify_loaded(load_json(authority_path), load_artifacts(generated_dir))


def mutation_self_test(generated_dir: Path, authority_path: Path = DEFAULT_AUTHORITY) -> list[str]:
    authority = load_json(authority_path)
    source = load_artifacts(generated_dir)

    def mutate_row(values: dict, field: str, value: object) -> None:
        manifest = values["test-manifest_v1.2.json"]
        manifest["test_variants"][0][field] = value
        manifest["manifest_sha256"] = digest(hashed_payload(manifest, "manifest_sha256"))

    def add_row(values: dict) -> None:
        manifest = values["test-manifest_v1.2.json"]
        row = copy.deepcopy(manifest["test_variants"][0])
        row["test_id"] = "DAL-T-MANIFEST-AUTHORITY-MUTATION"
        row["variant_id"] = "added_row"
        manifest["test_variants"].append(row)
        manifest["manifest_sha256"] = digest(hashed_payload(manifest, "manifest_sha256"))

    def add_catalog_root(values: dict, filename: str) -> None:
        catalog = values[filename]
        catalog["x-unapproved"] = True
        catalog["catalog_sha256"] = digest(hashed_payload(catalog, "catalog_sha256"))

    first = source["test-manifest_v1.2.json"]["test_variants"][0]
    mutations = (
        ("run_gate", lambda values: mutate_row(values, "run_gate", "G5")),
        ("owner_tasks", lambda values: mutate_row(values, "owner_tasks", ["DAL-050"])),
        ("expected_receipt", lambda values: mutate_row(values, "expected_receipt_code", "MUTATED")),
        ("expected_state", lambda values: mutate_row(values, "expected_entity_state", "mutated")),
        ("fixture_hash", lambda values: mutate_row(values, "fixture_sha256", "0" * 64)),
        ("oracle_hash", lambda values: mutate_row(values, "oracle_sha256", "0" * 64)),
        ("added_row", add_row),
        ("fixture_root_extra", lambda values: add_catalog_root(values, "test-fixtures_v1.0.json")),
        ("oracle_root_extra", lambda values: add_catalog_root(values, "test-oracles_v1.0.json")),
    )
    if first["run_gate"] == "G5" or first["owner_tasks"] == ["DAL-050"]:
        raise AuthorityVerificationError("manifest mutation anchors no longer distinguish the source row")
    passed: list[str] = []
    for label, mutate in mutations:
        values = copy.deepcopy(source)
        mutate(values)
        try:
            _verify_loaded(authority, values)
        except AuthorityVerificationError:
            passed.append(label)
        else:
            raise AuthorityVerificationError(f"{label} manifest mutation escaped authority")
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
