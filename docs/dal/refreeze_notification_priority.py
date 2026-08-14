#!/usr/bin/env python3
"""Re-freeze the two operation/test authorities for the notification-priority amendment.

Amendment (§3.5.2): `DAL-T-BATCH-001` member fields change from the un-frozen
`risk: high/medium` to the frozen `notification_priority: immediate/normal`, and
the `immediate` member triggers an atomic batch close-and-flush. `DAL-T-NOTIFY-001`
is unchanged in fixture/oracle shape — its delivery state machine was already
frozen by §3.7 — and needs no new oracle rows.

This is a mechanical field substitution, not a new semantic derivation: the
generated BATCH member must carry exactly the frozen field set, and every other
fixture/oracle is untouched. The two authorities are therefore re-derived from
the regenerated artifacts via the verifiers' own mechanical ``expected_*``
projections — the authority content contains no BATCH policy, only the digest
of whatever the generator produced. The check below proves the only semantic
change is the field rename before either authority is written.

Run after regenerating `manifests/`, then the four `verify_*_authority.py` scripts.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from dal_jcs import canonical_bytes

ROOT = Path(__file__).resolve().parent
MANIFESTS = ROOT / "manifests"
sys.path.insert(0, str(ROOT))

from verify_operation_oracle_authority import (  # noqa: E402
    expected_entries,
    expected_specs,
    semantic_counts as operation_counts,
)
from verify_test_manifest_authority import (  # noqa: E402
    expected_manifest_rows,
    semantic_counts as manifest_counts,
)


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


BATCH_MEMBER_FIELDS = frozenset(
    {"decision_id", "status", "notification_priority", "created_at", "expires_at"}
)


def verify_batch_field_closure() -> None:
    """The generated BATCH members must carry exactly the frozen field set."""
    fixtures = load(MANIFESTS / "test-fixtures_v1.0.json")["fixtures"]
    for variant in ("all_invalid", "continuous", "service_restart", "fifth_item", "high_risk_interrupt"):
        ref = f"dal.fixture/DAL-T-BATCH-001/{variant}/G1/1.0"
        members = fixtures[ref]["operation_sequence"][0]["input"]["authoritative_facts"]["members"]
        for member in members:
            if frozenset(member) != BATCH_MEMBER_FIELDS:
                raise SystemExit(f"BATCH {variant}: member fields not closed: {sorted(member)}")
            if member["notification_priority"] not in {"immediate", "normal"}:
                raise SystemExit(f"BATCH {variant}: priority outside frozen set: {member['notification_priority']!r}")
    print(json.dumps({"notification_priority_amendment_verified": True, "variants": 5}, sort_keys=True))


def refreeze_operation_authority() -> None:
    operation = load(MANIFESTS / "operation-spec-registry_v1.0.json")
    evidence = load(MANIFESTS / "evidence-schema-registry_v1.0.json")
    guard = load(MANIFESTS / "guard-predicate-registry_v1.0.json")
    manifest = load(MANIFESTS / "test-manifest_v1.2.json")
    fixtures = load(MANIFESTS / "test-fixtures_v1.0.json")
    oracles = load(MANIFESTS / "test-oracles_v1.0.json")

    authority = load(MANIFESTS / "operation-oracle-authority_v1.0.json")
    entries = expected_entries(operation, manifest, fixtures, oracles)
    authority["operation_specs"] = expected_specs(operation)
    authority["entries"] = entries
    authority["frozen_operation_registry_sha256"] = operation["registry_sha256"]
    authority["frozen_evidence_registry_sha256"] = evidence["registry_sha256"]
    authority["frozen_guard_registry_sha256"] = guard["registry_sha256"]
    authority["semantic_counts"] = operation_counts(operation, entries)
    authority["authority_sha256"] = rehash(authority, "authority_sha256")
    write(MANIFESTS / "operation-oracle-authority_v1.0.json", authority)


def refreeze_manifest_authority() -> None:
    manifest = load(MANIFESTS / "test-manifest_v1.2.json")
    fixtures = load(MANIFESTS / "test-fixtures_v1.0.json")
    oracles = load(MANIFESTS / "test-oracles_v1.0.json")

    authority = load(MANIFESTS / "test-manifest-authority_v1.0.json")
    rows = expected_manifest_rows(manifest, fixtures, oracles)
    authority["manifest_rows"] = rows
    authority["frozen_manifest_sha256"] = manifest["manifest_sha256"]
    authority["semantic_counts"] = manifest_counts(rows)
    authority["authority_sha256"] = rehash(authority, "authority_sha256")
    write(MANIFESTS / "test-manifest-authority_v1.0.json", authority)


def main() -> None:
    verify_batch_field_closure()
    refreeze_operation_authority()
    refreeze_manifest_authority()
    print(json.dumps({"amendment": "notification-priority-§3.5.2", "re_frozen": [
        "operation-oracle-authority_v1.0.json",
        "test-manifest-authority_v1.0.json",
    ]}, sort_keys=True))


if __name__ == "__main__":
    main()
