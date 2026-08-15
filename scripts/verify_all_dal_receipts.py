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
import subprocess
import sys
from pathlib import Path

import verify_dal_test_receipt as verifier

_original_load = verifier._load


@functools.lru_cache(maxsize=None)
def _load_cached(path: Path) -> dict:
    """Memoised frozen-artifact load, shared across the whole batch."""
    return _original_load(path)


def main() -> int:
    # Batch-only cache: route the verifier's `_load` through a memoised wrapper.
    # `_original_load` is captured *before* the patch so the wrapper does not
    # recurse into itself.
    verifier._load = _load_cached  # noqa: SLF001

    receipts = sorted((verifier.REPO_ROOT / "docs" / "evidence" / "receipts").glob("*.json"))
    if not receipts:
        print("no receipts found", file=sys.stderr)
        return 2

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
    print(f"PASS: {len(receipts)} receipts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
