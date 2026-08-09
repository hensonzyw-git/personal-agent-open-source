#!/usr/bin/env python3
"""Verify generated transition contracts against a separately frozen authority.

The authority JSON is deliberately not written by build_contract_manifests.py.
This verifier is read-only: a registry/fixture/oracle change must be reviewed and
the authority must be explicitly re-frozen in a separate change before it can
pass.  Hash agreement inside the generated artifact family is never treated as
semantic agreement with the authority.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_GENERATED_DIR = ROOT / "manifests"
DEFAULT_AUTHORITY = DEFAULT_GENERATED_DIR / "transition-oracle-authority_v1.0.json"
AUTHORITY_KEYS = {
    "schema_version",
    "authority_version",
    "source_boundary",
    "frozen_transition_registry_sha256",
    "semantic_counts",
    "transition_specs",
    "entries",
    "authority_sha256",
}
ORACLE_RESULT_FIELDS = {
    "schema_version",
    "test_id",
    "variant_id",
    "run_gate",
    "pre_state",
    "injection_operation",
    "expected_state_trace",
    "expected_event_trace",
    "expected_receipts",
    "expected_external_effect_trace",
    "expected_final_snapshot",
    "expected_related_snapshots",
    "allowed_write_set",
    "forbidden_side_effects",
    "expected_atomic_companion_transitions",
    "scenario_assertions",
    "coverage_ref",
}


class AuthorityVerificationError(ValueError):
    """The generated family is not semantically identical to the authority."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


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


def transition_fixtures(fixtures: dict) -> dict[str, dict]:
    return {
        ref: fixture
        for ref, fixture in fixtures["fixtures"].items()
        if "transition_command" in fixture
        or fixture.get("resolver_sequence_kind") == "transition_commands"
    }


def expected_entries(manifest: dict, fixtures: dict, oracles: dict) -> dict[str, dict]:
    manifest_by_fixture = {row["fixture_ref"]: row for row in manifest["test_variants"]}
    entries: dict[str, dict] = {}
    for fixture_ref, fixture in sorted(transition_fixtures(fixtures).items()):
        if fixture_ref not in manifest_by_fixture:
            raise AuthorityVerificationError(f"transition fixture absent from manifest: {fixture_ref}")
        row = manifest_by_fixture[fixture_ref]
        oracle_id = row["oracle_id"]
        oracle = oracles["oracles"].get(oracle_id)
        if oracle is None:
            raise AuthorityVerificationError(f"transition oracle absent: {oracle_id}")
        if set(oracle) != ORACLE_RESULT_FIELDS:
            raise AuthorityVerificationError(f"transition oracle field drift: {oracle_id}")
        if "transition_command" in fixture:
            resolver_input = {"kind": "single", "commands": [fixture["transition_command"]]}
        else:
            resolver_input = {"kind": "sequence", "commands": fixture["operation_sequence"]}
        if not resolver_input["commands"]:
            raise AuthorityVerificationError(f"empty transition resolver input: {fixture_ref}")
        entries[oracle_id] = {
            "fixture_ref": fixture_ref,
            "coverage_ref": fixture["coverage_ref"],
            "pre_state": fixture["pre_state"],
            "resolver_input": resolver_input,
            "trusted_resolver_context": fixture.get("trusted_resolver_context"),
            "authoritative_context": fixture.get("authoritative_context"),
            "expected_result": oracle,
        }
    return entries


def expected_specs(transition_registry: dict) -> dict[str, dict]:
    return {
        row["spec_id"]: row
        for row in transition_registry["specs"]
    }


def semantic_counts(transition_registry: dict, entries: dict[str, dict]) -> dict[str, int]:
    single = sum(entry["resolver_input"]["kind"] == "single" for entry in entries.values())
    sequence = sum(entry["resolver_input"]["kind"] == "sequence" for entry in entries.values())
    commands = sum(len(entry["resolver_input"]["commands"]) for entry in entries.values())
    return {
        "transition_specs": len(transition_registry["specs"]),
        "transition_oracle_entries": len(entries),
        "single_command_entries": single,
        "sequence_entries": sequence,
        "resolver_command_objects": commands,
        "complete_pre_state_command_result_entries": sum(
            bool(entry["pre_state"])
            and bool(entry["resolver_input"]["commands"])
            and bool(entry["expected_result"])
            for entry in entries.values()
        ),
    }


def _verify_loaded(
    authority: dict,
    transition_registry: dict,
    manifest: dict,
    fixtures: dict,
    oracles: dict,
) -> dict[str, int]:
    if set(authority) != AUTHORITY_KEYS:
        raise AuthorityVerificationError("authority root fields are not closed")
    if authority["schema_version"] != "dal.transition-oracle-authority/1.0":
        raise AuthorityVerificationError("authority schema version mismatch")
    if authority["authority_version"] != "1.0":
        raise AuthorityVerificationError("authority version mismatch")
    if authority["source_boundary"] != "static-independent-review-authority; never generated by build_contract_manifests.py":
        raise AuthorityVerificationError("authority source boundary mismatch")
    if digest(hashed_payload(authority, "authority_sha256")) != authority["authority_sha256"]:
        raise AuthorityVerificationError("authority self-hash mismatch")
    if digest(hashed_payload(transition_registry, "registry_sha256")) != transition_registry["registry_sha256"]:
        raise AuthorityVerificationError("generated transition registry self-hash mismatch")
    if authority["frozen_transition_registry_sha256"] != transition_registry["registry_sha256"]:
        raise AuthorityVerificationError("transition registry hash differs from independent authority")

    specs = expected_specs(transition_registry)
    if authority["transition_specs"] != specs:
        raise AuthorityVerificationError("transition registry semantics differ from independent authority")
    entries = expected_entries(manifest, fixtures, oracles)
    if authority["entries"] != entries:
        raise AuthorityVerificationError("pre-state/command/context/expected-result differs from independent authority")
    counts = semantic_counts(transition_registry, entries)
    if authority["semantic_counts"] != counts:
        raise AuthorityVerificationError("transition authority denominator mismatch")
    if counts["complete_pre_state_command_result_entries"] != counts["transition_oracle_entries"]:
        raise AuthorityVerificationError("not every transition oracle freezes complete semantic input and result")
    return counts


def verify_authority(generated_dir: Path, authority_path: Path = DEFAULT_AUTHORITY) -> dict[str, int]:
    return _verify_loaded(
        load_json(authority_path),
        load_json(generated_dir / "transition-spec-registry_v1.0.json"),
        load_json(generated_dir / "test-manifest_v1.2.json"),
        load_json(generated_dir / "test-fixtures_v1.0.json"),
        load_json(generated_dir / "test-oracles_v1.0.json"),
    )


def mutation_self_test(generated_dir: Path, authority_path: Path = DEFAULT_AUTHORITY) -> list[str]:
    authority = load_json(authority_path)
    source = {
        "transition": load_json(generated_dir / "transition-spec-registry_v1.0.json"),
        "manifest": load_json(generated_dir / "test-manifest_v1.2.json"),
        "fixtures": load_json(generated_dir / "test-fixtures_v1.0.json"),
        "oracles": load_json(generated_dir / "test-oracles_v1.0.json"),
    }
    first_spec = source["transition"]["specs"][0]
    target = first_spec["spec_id"]

    def command_mutation(values: dict) -> None:
        spec = values["transition"]["specs"][0]
        original = spec["command_type"]
        spec["command_type"] = f"{original}__mutated"
        for fixture in values["fixtures"]["fixtures"].values():
            if fixture.get("coverage_ref") == target and "transition_command" in fixture:
                fixture["transition_command"]["command_type"] = spec["command_type"]

    def event_mutation(values: dict) -> None:
        spec = values["transition"]["specs"][0]
        original = spec["event_type"]
        spec["event_type"] = f"{original}__mutated"
        for oracle in values["oracles"]["oracles"].values():
            if oracle.get("coverage_ref") == target:
                oracle["expected_event_trace"] = [
                    spec["event_type"] if event == original else event
                    for event in oracle["expected_event_trace"]
                ]

    def state_mutation(values: dict) -> None:
        spec = values["transition"]["specs"][0]
        original = spec["to_state"]
        spec["to_state"] = f"{original}__mutated"
        for oracle in values["oracles"]["oracles"].values():
            if oracle.get("coverage_ref") == target and oracle["expected_final_snapshot"]["state"] == original:
                oracle["expected_final_snapshot"]["state"] = spec["to_state"]
                oracle["expected_state_trace"][-1] = spec["to_state"]

    def write_set_mutation(values: dict) -> None:
        spec = values["transition"]["specs"][0]
        spec["atomic_write_set"] = [*spec["atomic_write_set"], "mutated_write"]
        for oracle in values["oracles"]["oracles"].values():
            if oracle.get("coverage_ref") == target and oracle["expected_receipts"] and oracle["expected_receipts"][0]["code"] == "APPLIED":
                oracle["allowed_write_set"] = [*oracle["allowed_write_set"], "mutated_write"]

    passed: list[str] = []
    for label, mutate in (
        ("command", command_mutation),
        ("event", event_mutation),
        ("to_state", state_mutation),
        ("write_set", write_set_mutation),
    ):
        values = copy.deepcopy(source)
        mutate(values)
        try:
            _verify_loaded(
                authority,
                values["transition"],
                values["manifest"],
                values["fixtures"],
                values["oracles"],
            )
        except AuthorityVerificationError:
            passed.append(label)
        else:
            raise AuthorityVerificationError(f"single-sided {label} mutation escaped authority")
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
