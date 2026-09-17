#!/usr/bin/env python3
"""Verify ``dal.artifact-rebuild-receipt/1.0`` receipts under ``docs/evidence/artifact-receipts/``.

Each Wave 3 machine-artifact receipt claims three things, and each is
re-derived here from an independent source rather than trusted:

1. The receipt restates a frozen ``test-manifest_v1.2`` row — re-looked-up,
   with ``manifest_row_sha256``/``owner_tasks_sha256``/fixture/oracle hashes
   recomputed from the frozen row, the fixture/oracle catalogs and the
   hash-verified contract binding (``tests.dal.contract_loader``).
2. The claimed authority coverage holds — membership is recomputed against
   the authority files, and each oracle must belong to exactly one of the two
   oracle authorities.
3. The receipt binds the commit the rebuild ran against — ``implementation_sha``
   must exist, be an ancestor of HEAD, and the declared implementation paths
   must be byte-identical between that commit and HEAD (fail closed on drift).

Completeness is exact: the receipt directory must contain exactly the 14
Wave 3 §7 identities — no missing, no extra.

``--mutation-self-test`` re-runs the verifier against deliberately corrupted
copies in a throwaway directory (the repository is never touched) and fails
unless every mutation class is rejected.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.dal.contract_loader import FrozenContracts, content_hash  # noqa: E402

RECEIPT_SCHEMA = "dal.artifact-rebuild-receipt/1.0"
RECEIPTS_DIR = REPO_ROOT / "docs" / "evidence" / "artifact-receipts"
MANIFESTS_DIR = REPO_ROOT / "docs" / "dal" / "manifests"
MANIFEST_PATH = MANIFESTS_DIR / "test-manifest_v1.2.json"

#: The exact Wave 3 §7 identity set; the receipt directory must match it exactly.
WAVE3_IDENTITIES: frozenset[tuple[str, str]] = frozenset(
    {
        *[
            ("DAL-T-PROVIDER-CONTRACT-001", variant)
            for variant in (
                "empty",
                "multi_tool",
                "prose_tool",
                "malformed_args",
                "half_stream",
                "multi_final",
                "multi_turn",
                "context_drift",
            )
        ],
        *[
            ("DAL-T-REVIEW-INDEP-001", variant)
            for variant in (
                "same_session",
                "same_context",
                "same_independence_key",
                "synthetic_fresh",
                "live_fresh",
            )
        ],
        ("DAL-T-INJECTION-001", "provider_output"),
    }
)

RECEIPT_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "manifest_sha256",
        "test_id",
        "variant_id",
        "run_gate",
        "owner_tasks_sha256",
        "fixture_ref",
        "fixture_sha256",
        "oracle_id",
        "oracle_sha256",
        "manifest_row_sha256",
        "authority_coverage",
        "rebuild",
        "implementation_sha",
        "implementation_paths",
        "environment",
        "environment_digest",
        "status",
        "recorded_at",
    }
)

#: The only authority membership fields a receipt may carry.
COVERAGE_FIELDS: frozenset[str] = frozenset(
    {
        "transition_oracle_authority",
        "operation_oracle_authority",
        "test_manifest_authority",
        "test_manifest_row_present",
        "fixture_catalog_entry_present",
    }
)


class ArtifactReceiptError(ValueError):
    """Raised when a receipt fails verification; fail closed, never repair."""


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True
    )


def _manifest_row(manifest: dict, test_id: str, variant_id: str) -> dict[str, Any]:
    row = next(
        (
            row
            for row in manifest["test_variants"]
            if row["test_id"] == test_id and row["variant_id"] == variant_id
        ),
        None,
    )
    if row is None:
        raise ArtifactReceiptError(f"no frozen manifest row for {test_id}/{variant_id}")
    return row


def _authority_membership(oracle_id: str, fixture_ref: str) -> dict[str, bool]:
    transition = _load(MANIFESTS_DIR / "transition-oracle-authority_v1.0.json")
    operation = _load(MANIFESTS_DIR / "operation-oracle-authority_v1.0.json")
    manifest_authority = _load(MANIFESTS_DIR / "test-manifest-authority_v1.0.json")
    oracle_catalog = _load(MANIFESTS_DIR / "test-oracles_v1.0.json")
    return {
        "transition_oracle_authority": oracle_id in transition["entries"],
        "operation_oracle_authority": oracle_id in operation["entries"],
        "test_manifest_authority": any(
            row["oracle_id"] == oracle_id
            for row in manifest_authority["manifest_rows"].values()
        ),
        "test_manifest_row_present": oracle_id in oracle_catalog["oracles"],
        "fixture_catalog_entry_present": any(
            row["fixture_ref"] == fixture_ref
            for row in manifest_authority["manifest_rows"].values()
        ),
    }


def _verify_implementation_binding(receipt: dict[str, Any]) -> None:
    sha = receipt["implementation_sha"]
    paths = receipt["implementation_paths"]
    if _git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise ArtifactReceiptError(f"implementation_sha is not an existing commit: {sha}")
    if _git("merge-base", "--is-ancestor", sha, "HEAD").returncode != 0:
        raise ArtifactReceiptError(f"implementation_sha is not an ancestor of HEAD: {sha}")
    diff = _git("diff", "--quiet", sha, "HEAD", "--", *paths)
    if diff.returncode != 0:
        raise ArtifactReceiptError(
            "implementation paths drifted between implementation_sha and HEAD; "
            "the receipts no longer bind the current tree — regenerate them"
        )


def verify_receipt(
    body: dict[str, Any],
    *,
    manifest: dict,
    contracts: FrozenContracts,
    skip_implementation_binding: bool = False,
) -> None:
    """Verify one receipt body; raises :class:`ArtifactReceiptError` on any defect."""
    fields = set(body)
    if fields != set(RECEIPT_FIELDS):
        raise ArtifactReceiptError(
            f"closed field set violated: missing={sorted(set(RECEIPT_FIELDS) - fields)} "
            f"unknown={sorted(fields - set(RECEIPT_FIELDS))}"
        )
    if body["schema_version"] != RECEIPT_SCHEMA:
        raise ArtifactReceiptError(f"wrong schema_version: {body['schema_version']}")
    if body["status"] != "REBUILT":
        raise ArtifactReceiptError(f"wrong status: {body['status']!r}; REBUILT is the only valid value")

    identity = (body["test_id"], body["variant_id"])
    if identity not in WAVE3_IDENTITIES:
        raise ArtifactReceiptError(f"receipt identity outside the Wave 3 §7 set: {identity}")

    if body["manifest_sha256"] != manifest["manifest_sha256"]:
        raise ArtifactReceiptError("manifest_sha256 does not match the frozen manifest")

    row = _manifest_row(manifest, body["test_id"], body["variant_id"])

    row_mismatches = [
        field
        for field in ("run_gate", "fixture_ref", "fixture_sha256", "oracle_id", "oracle_sha256")
        if body[field] != row[field]
    ]
    if row_mismatches:
        raise ArtifactReceiptError(f"receipt disagrees with frozen row on {row_mismatches}")

    if body["owner_tasks_sha256"] != content_hash(list(row["owner_tasks"])):
        raise ArtifactReceiptError("owner_tasks_sha256 does not recompute")
    if body["manifest_row_sha256"] != content_hash(row):
        raise ArtifactReceiptError("manifest_row_sha256 does not recompute from the frozen row")

    variant = next(
        (v for v in contracts.variants(body["test_id"]) if v.variant_id == body["variant_id"]),
        None,
    )
    if variant is None:
        raise ArtifactReceiptError(
            f"contract loader binds no such variant: {body['test_id']}/{body['variant_id']}"
        )
    binding_mismatches = (
        []
        + (["run_gate"] if body["run_gate"] != variant.run_gate else [])
        + (["fixture_ref"] if body["fixture_ref"] != variant.fixture.ref else [])
        + (["fixture_sha256"] if body["fixture_sha256"] != content_hash(variant.fixture.body) else [])
        + (["oracle_id"] if body["oracle_id"] != variant.oracle.oracle_id else [])
        + (["oracle_sha256"] if body["oracle_sha256"] != content_hash(variant.oracle.body) else [])
        + (
            ["owner_tasks_sha256"]
            if body["owner_tasks_sha256"] != content_hash(list(variant.owner_tasks))
            else []
        )
    )
    if binding_mismatches:
        raise ArtifactReceiptError(
            f"receipt disagrees with the recomputed contract binding on {binding_mismatches}"
        )

    coverage = body["authority_coverage"]
    if set(coverage) != set(COVERAGE_FIELDS):
        raise ArtifactReceiptError(
            f"authority_coverage field set violated: {sorted(coverage)}"
        )
    expected_membership = _authority_membership(body["oracle_id"], body["fixture_ref"])
    if coverage != expected_membership:
        raise ArtifactReceiptError(
            f"authority_coverage disagrees with recomputed membership: {coverage}"
        )
    if (
        coverage["transition_oracle_authority"]
        == coverage["operation_oracle_authority"]
    ):
        raise ArtifactReceiptError(
            "oracle must belong to exactly one of the two oracle authorities"
        )
    if not (
        coverage["test_manifest_authority"]
        and coverage["test_manifest_row_present"]
        and coverage["fixture_catalog_entry_present"]
    ):
        raise ArtifactReceiptError("authority coverage incomplete")

    if not isinstance(body["implementation_paths"], list) or not body["implementation_paths"]:
        raise ArtifactReceiptError("implementation_paths must be a non-empty list")
    if body["environment_digest"] != content_hash(body["environment"]):
        raise ArtifactReceiptError("environment_digest does not recompute")

    rebuild = body["rebuild"]
    if not isinstance(rebuild, dict):
        raise ArtifactReceiptError("rebuild must be an object")
    if set(rebuild) != {"generator", "scope_files", "drift", "authorities_verified"}:
        raise ArtifactReceiptError(f"rebuild field set violated: {sorted(rebuild)}")
    if rebuild["drift"] != "none":
        raise ArtifactReceiptError(
            f"rebuild drifted ({rebuild['drift']!r}); receipts may not be written"
        )
    if rebuild["generator"] != "docs/dal/build_contract_manifests.py":
        raise ArtifactReceiptError(f"unknown rebuild generator: {rebuild['generator']}")
    if set(rebuild["authorities_verified"]) != {
        "transition-oracle-authority_v1.0.json",
        "operation-oracle-authority_v1.0.json",
        "test-manifest-authority_v1.0.json",
        "eval-schema-authority_v1.0.json",
    }:
        raise ArtifactReceiptError("rebuild must verify all four frozen authorities")
    if rebuild["scope_files"] != 20:
        raise ArtifactReceiptError(
            f"rebuild scope changed ({rebuild['scope_files']} files); "
            "the snapshot scope and this verifier disagree"
        )

    if not skip_implementation_binding:
        _verify_implementation_binding(body)


def verify_dir(
    receipts_dir: Path,
    *,
    skip_implementation_binding: bool = False,
) -> dict[str, int]:
    """Verify every receipt in ``receipts_dir`` and enforce exact completeness."""
    files = sorted(receipts_dir.glob("*.json"))
    if not files:
        raise ArtifactReceiptError(f"no receipts found under {receipts_dir}")

    manifest = _load(MANIFEST_PATH)
    contracts = FrozenContracts()

    seen: set[tuple[str, str]] = set()
    for path in files:
        body = _load(path)
        verify_receipt(
            body,
            manifest=manifest,
            contracts=contracts,
            skip_implementation_binding=skip_implementation_binding,
        )
        identity = (body["test_id"], body["variant_id"])
        if identity in seen:
            raise ArtifactReceiptError(f"duplicate receipt identity {identity} ({path.name})")
        seen.add(identity)

    missing = WAVE3_IDENTITIES - seen
    extra = seen - WAVE3_IDENTITIES
    if missing or extra:
        raise ArtifactReceiptError(
            f"Wave 3 completeness violated: missing={sorted(missing)} extra={sorted(extra)}"
        )
    return {"receipts": len(files)}


def mutation_self_test(receipts_dir: Path) -> list[str]:
    """Every corruption class must be rejected; runs only on throwaway copies."""
    source = [_load(path) for path in sorted(receipts_dir.glob("*.json"))]
    if len(source) != len(WAVE3_IDENTITIES):
        raise ArtifactReceiptError("cannot self-test against an incomplete receipt set")

    def mutate_first(field: str, value: Any):
        def mutate(bodies: list[dict]) -> None:
            bodies[0][field] = value

        return mutate

    def drop_last(bodies: list[dict]) -> None:
        bodies.pop()

    def append_unknown(bodies: list[dict]) -> None:
        extra = copy.deepcopy(bodies[0])
        extra["test_id"] = "DAL-T-UNKNOWN-999"
        extra["variant_id"] = "not_in_wave3"
        bodies.append(extra)

    def add_field(bodies: list[dict]) -> None:
        bodies[0]["x-unapproved"] = True

    def drop_field(bodies: list[dict]) -> None:
        del bodies[0]["environment_digest"]

    def tamper_coverage(bodies: list[dict]) -> None:
        bodies[0]["authority_coverage"]["transition_oracle_authority"] = True

    def tamper_coverage_complete(bodies: list[dict]) -> None:
        bodies[0]["authority_coverage"]["fixture_catalog_entry_present"] = False

    def tamper_nested_rebuild(bodies: list[dict]) -> None:
        bodies[0]["rebuild"]["drift"] = "hand-edited"

    mutations = (
        ("run_gate", mutate_first("run_gate", "G5")),
        ("fixture_sha256", mutate_first("fixture_sha256", "0" * 64)),
        ("oracle_sha256", mutate_first("oracle_sha256", "0" * 64)),
        ("manifest_row_sha256", mutate_first("manifest_row_sha256", "0" * 64)),
        ("owner_tasks_sha256", mutate_first("owner_tasks_sha256", "0" * 64)),
        ("manifest_sha256", mutate_first("manifest_sha256", "0" * 64)),
        ("implementation_sha", mutate_first("implementation_sha", "0" * 40)),
        ("status", mutate_first("status", "PASS")),
        ("schema_version", mutate_first("schema_version", "dal.test-receipt/1.0")),
        ("environment_digest", mutate_first("environment_digest", "0" * 64)),
        ("extra_field", add_field),
        ("missing_field", drop_field),
        ("coverage_xor", tamper_coverage),
        ("coverage_incomplete", tamper_coverage_complete),
        ("rebuild_drift", tamper_nested_rebuild),
        ("dropped_receipt", drop_last),
        ("unknown_receipt", append_unknown),
    )

    rejected: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dal-artifact-receipt-mutations-") as tmp:
        root = Path(tmp)
        for label, mutate in mutations:
            bodies = copy.deepcopy(source)
            mutate(bodies)
            mutated_dir = root / label
            mutated_dir.mkdir()
            for index, body in enumerate(bodies):
                name = f"{body['test_id']}__{body['variant_id']}__{body['run_gate']}.json"
                (mutated_dir / f"{index:03d}__{name}").write_text(
                    json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
            try:
                verify_dir(mutated_dir, skip_implementation_binding=False)
            except ArtifactReceiptError:
                rejected.append(label)
            else:
                raise ArtifactReceiptError(f"mutation escaped the verifier: {label}")
    return rejected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receipts-dir", type=Path, default=RECEIPTS_DIR,
        help="directory holding dal.artifact-rebuild-receipt/1.0 JSON files",
    )
    parser.add_argument("--mutation-self-test", action="store_true")
    args = parser.parse_args(argv)

    try:
        counts = verify_dir(args.receipts_dir)
        mutations = (
            mutation_self_test(args.receipts_dir) if args.mutation_self_test else []
        )
    except (ArtifactReceiptError, OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "receipts": counts["receipts"],
                "status": "PASS",
                "mutations_rejected": mutations,
                "schema_version": RECEIPT_SCHEMA,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
