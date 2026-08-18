"""Wave 3 machine-artifact rebuild receipts (§9 item 1).

The 14 ``dal.artifact-rebuild-receipt/1.0`` files under
``docs/evidence/artifact-receipts/`` are the §9 item 1 deliverable: proof that
a fresh deterministic rebuild reproduces the frozen oracle/authority/manifest
bytes, bound per variant. These tests drive the generator and verifier as a
closed pair, including the adversarial mutations the verifier must reject.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "generate_dal_wave3_artifact_receipts.py"
VERIFIER = REPO_ROOT / "scripts" / "verify_dal_artifact_receipts.py"
RECEIPTS_DIR = REPO_ROOT / "docs" / "evidence" / "artifact-receipts"


def _load_script(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verifier():
    return _load_script(VERIFIER)


@pytest.fixture(scope="module")
def committed_receipts():
    """The receipts committed under docs/evidence/artifact-receipts/."""
    paths = sorted(RECEIPTS_DIR.glob("*.json"))
    assert paths, "no artifact receipts committed"
    return [_load_json(path) for path in paths]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_committed_receipts_verify(verifier, committed_receipts):
    counts = verifier.verify_dir(RECEIPTS_DIR)
    assert counts["receipts"] == 14 == len(committed_receipts)


def test_committed_receipts_verify_with_mutations(verifier):
    rejected = verifier.mutation_self_test(RECEIPTS_DIR)
    assert set(rejected) == {
        "run_gate",
        "fixture_sha256",
        "oracle_sha256",
        "manifest_row_sha256",
        "owner_tasks_sha256",
        "manifest_sha256",
        "implementation_sha",
        "status",
        "schema_version",
        "environment_digest",
        "extra_field",
        "missing_field",
        "coverage_xor",
        "coverage_incomplete",
        "rebuild_drift",
        "dropped_receipt",
        "unknown_receipt",
    }


def test_rebuild_is_reproducible_from_clean_tree(tmp_path):
    """A fresh generator run must reproduce the committed receipts' hashes."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["receipts"] == 14
    assert payload["status"] == "REBUILT"
    assert payload["written"] is False


def test_generator_dry_run_leaves_no_drift():
    before = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--short"],
        capture_output=True,
        text=True,
    ).stdout
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    after = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--short"],
        capture_output=True,
        text=True,
    ).stdout
    assert result.returncode == 0, result.stderr
    assert before == after, "dry-run must not touch the worktree"


def test_receipt_schema_is_not_a_test_receipt(committed_receipts):
    """The artifact schema must never be conflated with dal.test-receipt/1.0."""
    for receipt in committed_receipts:
        assert receipt["schema_version"] == "dal.artifact-rebuild-receipt/1.0"
        assert receipt["status"] == "REBUILT"
        assert "expected_receipt_code" not in receipt


def test_receipts_bind_the_manifest_and_catalogs(verifier, committed_receipts):
    """manifest_row_sha256 must recompute from the frozen row bytes."""
    manifest = _load_json(REPO_ROOT / "docs" / "dal" / "manifests" / "test-manifest_v1.2.json")
    rows = {
        (row["test_id"], row["variant_id"]): row
        for row in manifest["test_variants"]
    }
    for receipt in committed_receipts:
        row = rows[(receipt["test_id"], receipt["variant_id"])]
        assert receipt["manifest_sha256"] == manifest["manifest_sha256"]
        assert receipt["manifest_row_sha256"] == verifier_mod_content_hash(row)


def verifier_mod_content_hash(value) -> str:
    from tests.dal.contract_loader import content_hash

    return content_hash(value)


def test_verifier_rejects_out_of_wave3_identity(verifier, tmp_path, committed_receipts):
    """A receipt for an identity outside the §7 set must fail closed."""
    import copy

    bodies = [copy.deepcopy(r) for r in committed_receipts]
    bodies[0]["test_id"] = "DAL-T-UNKNOWN-999"
    (tmp_path / "receipt.json").write_text(
        json.dumps(bodies[0], ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    with pytest.raises(verifier.ArtifactReceiptError, match="outside the Wave 3"):
        verifier.verify_dir(tmp_path)


def test_verifier_detects_duplicate_identity(verifier, tmp_path, committed_receipts):
    """Two byte-identical receipts under different filenames must be rejected."""
    import copy

    duplicate = copy.deepcopy(committed_receipts[0])
    body = json.dumps(duplicate, sort_keys=True, indent=2)
    name = f"{duplicate['test_id']}__{duplicate['variant_id']}__{duplicate['run_gate']}.json"
    (tmp_path / name).write_text(body, encoding="utf-8")
    (tmp_path / f"dup__{name}").write_text(body, encoding="utf-8")
    with pytest.raises(verifier.ArtifactReceiptError, match="duplicate"):
        verifier.verify_dir(tmp_path)
