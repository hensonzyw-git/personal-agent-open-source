#!/usr/bin/env python3
"""Verify every committed ``dal.test-receipt/1.0`` under ``docs/evidence/receipts/``.

A thin batch wrapper over ``scripts/verify_dal_test_receipt.py``. That verifier
is itself bound by the DAL-012 receipt's ``implementation_paths`` and must stay
byte-identical, so batch mode lives in its own script rather than modifying it.

The per-receipt frozen-contract loads are memoised across the batch: the
manifest, fixture and oracle catalogs are identical for every receipt, so they
are parsed once instead of once per receipt.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from pathlib import Path

import verify_dal_test_receipt as verifier

_original_load = verifier._load

G1_OWNER_TASKS = frozenset(
    {"DAL-007", "DAL-008", "DAL-009", "DAL-010", "DAL-011", "DAL-012", "DAL-013"}
)


@functools.lru_cache(maxsize=None)
def _load_cached(path: Path) -> dict:
    """Memoised frozen-artifact load, shared across the whole batch."""
    return _original_load(path)


def _install_batch_caches() -> None:
    """Cache immutable JCS code and the repeated full-manifest digest."""
    module_path = verifier.MANIFESTS.parent / "dal_jcs.py"
    namespace: dict = {}
    exec(compile(module_path.read_bytes(), str(module_path), "exec"), namespace)
    verifier._canonical_bytes = namespace["canonical_bytes"]  # noqa: SLF001

    manifest = verifier._load(verifier.MANIFESTS / "test-manifest_v1.2.json")
    manifest_body = dict(manifest)
    manifest_body.pop("manifest_sha256", None)
    manifest_digest = verifier._sha256(manifest_body)  # noqa: SLF001
    manifest_rows = manifest["test_variants"]
    original_sha256 = verifier._sha256

    def _sha256_cached(value) -> str:
        if (
            isinstance(value, dict)
            and value.get("test_variants") is manifest_rows
            and "manifest_sha256" not in value
        ):
            return manifest_digest
        return original_sha256(value)

    verifier._sha256 = _sha256_cached  # noqa: SLF001


def _identity(body: dict) -> tuple[str, str, str]:
    return body["test_id"], body["variant_id"], body["run_gate"]


def _verify_g1_completeness(receipts: list[Path]) -> int:
    """Prove the target G1 denominator is exact, not merely that files present pass."""
    manifest = verifier._load(verifier.MANIFESTS / "test-manifest_v1.2.json")
    expected = {
        (row["test_id"], row["variant_id"], row["run_gate"])
        for row in manifest["test_variants"]
        if row["run_gate"] == "G1"
        and G1_OWNER_TASKS.intersection(row["owner_tasks"])
    }
    seen: dict[tuple[str, str, str], list[str]] = {}
    for receipt in receipts:
        body = verifier._load(receipt)
        identity = _identity(body)
        if identity in expected:
            seen.setdefault(identity, []).append(receipt.name)

    missing = sorted(expected - set(seen))
    duplicates = {key: names for key, names in seen.items() if len(names) != 1}
    if missing or duplicates:
        detail = {
            "missing": ["/".join(key) for key in missing],
            "duplicates": {"/".join(key): names for key, names in duplicates.items()},
        }
        raise ValueError("G1 receipt denominator mismatch: " + json.dumps(detail, sort_keys=True))
    return len(expected)


def main() -> int:
    # Batch-only cache: route the verifier's `_load` through a memoised wrapper.
    # `_original_load` is captured *before* the patch so the wrapper does not
    # recurse into itself.
    verifier._load = _load_cached  # noqa: SLF001
    _install_batch_caches()

    receipts = sorted((verifier.REPO_ROOT / "docs" / "evidence" / "receipts").glob("*.json"))
    if not receipts:
        print("no receipts found", file=sys.stderr)
        return 2

    try:
        g1_count = _verify_g1_completeness(receipts)
    except (KeyError, OSError, ValueError) as error:
        print(f"FAIL G1 completeness: {error}", file=sys.stderr)
        return 1

    failed = 0
    for receipt in receipts:
        try:
            verifier.verify(receipt)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(f"FAIL {receipt.name}: {error}", file=sys.stderr)
            failed += 1

    if failed:
        print(f"{failed} of {len(receipts)} receipts FAILED", file=sys.stderr)
        return 1
    print(
        f"PASS: {len(receipts)} receipts verified; "
        f"G1 completeness {g1_count}/{g1_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
