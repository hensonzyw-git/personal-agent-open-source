#!/usr/bin/env python3
"""Targeted authority re-freeze for the authorised binding and Dock amendment.

The script independently checks the affected semantics, proves that no
unrelated authority row differs from the generated expectation, then replaces
only the three affected operation specs and their test rows.  It deliberately
cannot rebuild an entire authority from generated artifacts.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta
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
        "DAL-T-APP-001", "DAL-T-APP-EXP-001", "DAL-T-STATEHASH-001",
        "DAL-T-ARTIFACTHASH-001", "DAL-T-DOCK-001",
    }
)
TARGET_OPERATION_IDS = frozenset(
    {
        "OP-APP-001", "OP-APP-EXP-001", "OP-STATEHASH-001",
        "OP-ARTIFACTHASH-001", "OP-DOCK-001",
    }
)
STATE_FIELDS = frozenset(
    {
        "schema_version", "feature_id", "feature_version", "feature_state",
        "checkpoint_state", "reason_code", "plan_version", "artifact_sha256",
        "repository_id", "base_sha", "result_sha", "last_verified_sha",
        "decision_frontier_version", "policy_version", "capability_epoch",
        "external_effect_inventory_sha256",
    }
)
ARTIFACT_FIELDS = frozenset(
    {
        "schema_version", "artifact_schema_version", "media_type", "feature_id",
        "artifact_kind", "artifact_version", "base_sha", "body_canonicalization",
        "body_sha256", "body_size", "acceptance_sha256", "allowed_paths_sha256",
    }
)
DOCK_WRITES = ["decision_projection", "operation_receipt", "audit"]


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


def parse_rfc3339(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _target_entry(key: str) -> bool:
    return any(f"/{test_id}/" in key for test_id in TARGET_TEST_IDS)


def _target_manifest_row(key: str) -> bool:
    return any(key.startswith(f"{test_id}/") for test_id in TARGET_TEST_IDS)


def verify_bindings(fixtures: dict, oracles: dict) -> None:
    for key, fixture in fixtures.items():
        if "/DAL-T-STATEHASH-001/" not in key and "/DAL-T-ARTIFACTHASH-001/" not in key:
            continue
        operation = fixture["operation_sequence"][0]
        facts = operation["input"]["authoritative_facts"]
        oracle_key = key.replace("dal.fixture/", "dal.oracle/")
        result = oracles[oracle_key]
        if result["allowed_write_set"] or result["expected_event_trace"]:
            raise SystemExit(f"{key}: binding refusal is not zero-write")
        if "/DAL-T-STATEHASH-001/" in key:
            protected = facts["protected_binding"]
            current = facts["current_binding"]
            if frozenset(protected) != STATE_FIELDS:
                raise SystemExit(f"{key}: protected StateBinding field set drifted")
            if protected["schema_version"] != "dal.state-binding/1.0":
                raise SystemExit(f"{key}: StateBinding schema drifted")
            receipt = "DECISION_STALE"
        else:
            protected = facts["protected_artifact"]
            current = facts["current_artifact"]
            if frozenset(protected) != ARTIFACT_FIELDS:
                raise SystemExit(f"{key}: protected ArtifactBinding field set drifted")
            if protected["schema_version"] != "dal.artifact-binding/1.0":
                raise SystemExit(f"{key}: ArtifactBinding schema drifted")
            body = operation["input"]["injected_results"][0]["canonical_body_utf8"].encode("utf-8")
            if len(body) != protected["body_size"] or hashlib.sha256(body).hexdigest() != protected["body_sha256"]:
                raise SystemExit(f"{key}: protected body readback contradicts binding")
            receipt = "APPROVAL_INVALID"
        if digest(protected) != facts["protected_binding_sha256"]:
            raise SystemExit(f"{key}: protected binding digest drifted")
        if facts["observed_binding_sha256"] != facts["protected_binding_sha256"]:
            raise SystemExit(f"{key}: observed digest is not the approved digest")
        if current == protected:
            raise SystemExit(f"{key}: refusal variant does not drift current authority")
        receipts = result["expected_receipts"]
        if receipts != [{"code": receipt, "count": 1, "schema_version": "dal.transition-receipt/1.0"}]:
            raise SystemExit(f"{key}: binding receipt semantics drifted")


def verify_approval_observed_digest(fixtures: dict) -> None:
    for key, fixture in fixtures.items():
        if "/DAL-T-APP-001/" not in key and "/DAL-T-APP-EXP-001/" not in key:
            continue
        operation = fixture["operation_sequence"][0]
        target = operation["input"]["target"]
        expected_binding = {
            "schema_version": "dal.state-binding/1.0",
            "feature_id": target["entity_id"],
            "feature_version": target["version"],
            "feature_state": target["state"],
            "checkpoint_state": None,
            "reason_code": None,
            "plan_version": None,
            "artifact_sha256": None,
            "repository_id": "repo-placeholder",
            "base_sha": "0" * 40,
            "result_sha": None,
            "last_verified_sha": None,
            "decision_frontier_version": 1,
            "policy_version": "dal-policy/1.0",
            "capability_epoch": 1,
            "external_effect_inventory_sha256": hashlib.sha256(b"").hexdigest(),
        }
        observed = operation["input"]["authoritative_facts"].get(
            "observed_state_sha256"
        )
        if observed != digest(expected_binding):
            raise SystemExit(f"{key}: approval observed state digest drifted")


def _rank(candidate: dict, now: datetime) -> int:
    if candidate["safety_or_irreversible"]:
        return 0
    if candidate["blocking_scope"] == "global":
        return 1
    if candidate["expires_at"] is not None and parse_rfc3339(candidate["expires_at"]) - now <= timedelta(minutes=15):
        return 2
    if candidate["blocking_scope"] == "local":
        return 3
    return 4


def _project(candidates: list[dict], now: datetime) -> tuple[list[str], dict, dict]:
    by_id = {item["decision_id"]: item for item in candidates}
    evictions: dict[str, str] = {}
    eligible: list[dict] = []
    for item in candidates:
        if item["status"] != "open":
            continue
        if item["expires_at"] is not None and now >= parse_rfc3339(item["expires_at"]):
            continue
        if any(by_id.get(dep, {}).get("status") not in {"resolved", "superseded"} for dep in item["depends_on"]):
            evictions[item["decision_id"]] = "dependency"
        else:
            eligible.append(item)
    eligible.sort(key=lambda item: (
        _rank(item, now),
        parse_rfc3339(item["expires_at"]) if item["expires_at"] is not None else datetime.max.replace(tzinfo=now.tzinfo),
        parse_rfc3339(item["created_at"]),
        item["decision_id"].encode("utf-8"),
    ))
    roots: set[str] = set()
    kept: list[dict] = []
    for item in eligible:
        if item["root_id"] in roots:
            evictions[item["decision_id"]] = "same_root"
        else:
            roots.add(item["root_id"])
            kept.append(item)
    for item in kept[5:]:
        evictions[item["decision_id"]] = "maximum_items"
    visible = kept[:5]
    return (
        [item["decision_id"] for item in visible],
        {item["decision_id"]: _rank(item, now) for item in visible},
        evictions,
    )


def verify_dock(fixtures: dict, oracles: dict) -> None:
    for key, fixture in fixtures.items():
        if "/DAL-T-DOCK-001/" not in key:
            continue
        operation = fixture["operation_sequence"][0]
        payload = operation["input"]
        if payload["authoritative_facts"] != {"source": "decision-store"}:
            raise SystemExit(f"{key}: resolver can still supply Dock candidates")
        source = payload["injected_results"]
        if len(source) != 1 or source[0].get("source") != "decision-store":
            raise SystemExit(f"{key}: no independent decision-store setup")
        ordered, ranks, evictions = _project(
            source[0]["candidates"], parse_rfc3339(source[0]["server_now"])
        )
        oracle = oracles[key.replace("dal.fixture/", "dal.oracle/")]
        assertions = {item["field"]: item for item in oracle["scenario_assertions"]}
        expected_values = {
            "ordered_decision_ids": ordered,
            "ranks": ranks,
            "evictions": evictions,
        }
        if any(assertions.get(name) != {"field": name, "operator": "equals", "value": value} for name, value in expected_values.items()):
            raise SystemExit(f"{key}: Dock projection semantics drifted")
        if oracle["allowed_write_set"] != DOCK_WRITES:
            raise SystemExit(f"{key}: Dock persistent write set drifted")
        if oracle["expected_event_trace"] != ["decision.created"]:
            raise SystemExit(f"{key}: Dock event semantics drifted")
        if oracle["expected_receipts"] != [{"code": "APPLIED", "count": 1, "schema_version": "dal.operation-receipt/1.0"}]:
            raise SystemExit(f"{key}: Dock receipt semantics drifted")


def refreeze_targeted() -> None:
    operation = load(MANIFESTS / "operation-spec-registry_v1.0.json")
    evidence = load(MANIFESTS / "evidence-schema-registry_v1.0.json")
    guard = load(MANIFESTS / "guard-predicate-registry_v1.0.json")
    manifest = load(MANIFESTS / "test-manifest_v1.2.json")
    fixtures = load(MANIFESTS / "test-fixtures_v1.0.json")
    oracles = load(MANIFESTS / "test-oracles_v1.0.json")
    verify_bindings(fixtures["fixtures"], oracles["oracles"])
    verify_approval_observed_digest(fixtures["fixtures"])
    verify_dock(fixtures["fixtures"], oracles["oracles"])

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
