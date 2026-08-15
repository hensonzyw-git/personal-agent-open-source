"""Verify one committed DAL test receipt against frozen contracts and Git.

The receipt is deliberately external to the implementation commit: its
``implementation_sha`` names an already-existing commit, avoiding a
self-referential hash.  Verification additionally requires every declared
implementation path to be byte-identical at the current checkout.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Final


REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
MANIFESTS: Final[Path] = REPO_ROOT / "docs" / "dal" / "manifests"
REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "manifest_sha256",
        "test_id",
        "variant_id",
        "run_gate",
        "owner_tasks_sha256",
        "fixture_sha256",
        "oracle_id",
        "oracle_sha256",
        "implementation_sha",
        "implementation_paths",
        "result_path",
        "result_sha",
        "environment",
        "environment_digest",
        "status",
        "recorded_at",
    }
)


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _canonical_bytes(value: Any) -> bytes:
    module_path = MANIFESTS.parent / "dal_jcs.py"
    namespace: dict[str, Any] = {}
    exec(compile(module_path.read_bytes(), str(module_path), "exec"), namespace)
    return namespace["canonical_bytes"](value)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_sha(value: Any, *, length: int, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} must be a lowercase {length}-character SHA")
    return value


def _manifest_row(receipt: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _load(MANIFESTS / "test-manifest_v1.2.json")
    declared_manifest_sha = manifest.get("manifest_sha256")
    manifest_body = dict(manifest)
    manifest_body.pop("manifest_sha256", None)
    if _sha256(manifest_body) != declared_manifest_sha:
        raise ValueError("frozen manifest content does not match manifest_sha256")
    matches = [
        row
        for row in manifest["test_variants"]
        if row["test_id"] == receipt["test_id"]
        and row["variant_id"] == receipt["variant_id"]
        and row["run_gate"] == receipt["run_gate"]
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one frozen manifest row, found {len(matches)}")
    row = matches[0]

    fixtures = _load(MANIFESTS / "test-fixtures_v1.0.json")["fixtures"]
    fixture = fixtures.get(row["fixture_ref"])
    if not isinstance(fixture, dict) or _sha256(fixture) != row["fixture_sha256"]:
        raise ValueError("fixture content does not match frozen fixture_sha256")
    oracles = _load(MANIFESTS / "test-oracles_v1.0.json")["oracles"]
    oracle = oracles.get(row["oracle_id"])
    if not isinstance(oracle, dict) or _sha256(oracle) != row["oracle_sha256"]:
        raise ValueError("oracle content does not match frozen oracle_sha256")
    return manifest, row


def verify(receipt_path: Path) -> None:
    receipt_path = receipt_path.resolve()
    if REPO_ROOT not in receipt_path.parents:
        raise ValueError("receipt path escapes repository")
    receipt = _load(receipt_path)
    if frozenset(receipt) != REQUIRED_FIELDS:
        raise ValueError(
            f"receipt field set drifted: {sorted(frozenset(receipt) ^ REQUIRED_FIELDS)}"
        )
    if receipt["schema_version"] != "dal.test-receipt/1.0":
        raise ValueError("wrong receipt schema")
    if receipt["status"] != "PASS":
        raise ValueError("only an actual PASS receipt can close a gate")
    datetime.fromisoformat(receipt["recorded_at"].replace("Z", "+00:00"))

    manifest, row = _manifest_row(receipt)
    expected = {
        "manifest_sha256": manifest["manifest_sha256"],
        "fixture_sha256": row["fixture_sha256"],
        "oracle_id": row["oracle_id"],
        "oracle_sha256": row["oracle_sha256"],
        "owner_tasks_sha256": _sha256(row["owner_tasks"]),
    }
    for field, value in expected.items():
        if receipt[field] != value:
            raise ValueError(f"{field} does not match the frozen manifest row")

    result_path = (REPO_ROOT / receipt["result_path"]).resolve()
    if REPO_ROOT not in result_path.parents:
        raise ValueError("result path escapes repository")
    result = _load(result_path)
    if _sha256(result) != _require_sha(
        receipt["result_sha"], length=64, field="result_sha"
    ):
        raise ValueError("result_sha does not bind the result artifact")
    if result.get("exit_code") != 0 or result.get("status") != "PASS":
        raise ValueError("result artifact is not a successful run")
    for field in ("test_id", "variant_id", "run_gate"):
        if result.get(field) != receipt[field]:
            raise ValueError(f"result {field} does not match receipt")

    environment = receipt["environment"]
    if not isinstance(environment, dict) or not environment:
        raise ValueError("environment must be a non-empty object")
    if _sha256(environment) != _require_sha(
        receipt["environment_digest"], length=64, field="environment_digest"
    ):
        raise ValueError("environment_digest does not bind environment")

    implementation_sha = _require_sha(
        receipt["implementation_sha"], length=40, field="implementation_sha"
    )
    paths = receipt["implementation_paths"]
    if (
        not isinstance(paths, list)
        or not paths
        or len(paths) != len(set(paths))
        or any(not isinstance(path, str) or not path for path in paths)
    ):
        raise ValueError("implementation_paths must be unique non-empty strings")
    subprocess.run(
        ["git", "cat-file", "-e", f"{implementation_sha}^{{commit}}"],
        cwd=REPO_ROOT,
        check=True,
    )
    unchanged = subprocess.run(
        ["git", "diff", "--quiet", implementation_sha, "HEAD", "--", *paths],
        cwd=REPO_ROOT,
        check=False,
    )
    if unchanged.returncode != 0:
        raise ValueError("implementation paths drifted after implementation_sha")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_dal_test_receipt.py RECEIPT.json", file=sys.stderr)
        return 2
    try:
        verify(Path(sys.argv[1]))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
