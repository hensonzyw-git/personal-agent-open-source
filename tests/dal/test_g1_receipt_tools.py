"""Regression tests for the aggregate DAL-G1 receipt proof tooling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import generate_dal_g1_receipts as generator
import verify_all_dal_receipts as batch
from tests.dal.contract_loader import FrozenContracts


def test_generator_dispatch_covers_exact_authorized_g1_denominator() -> None:
    variants = generator._g1_variants(FrozenContracts(), None, None)

    assert len(variants) == 1077
    assert {variant.test_id for variant in variants} == set(generator.DISPATCH)
    assert all(
        generator.G1_OWNER_TASKS.intersection(variant.owner_tasks)
        for variant in variants
    )
    assert not any(variant.test_id == "DAL-T-DELIVERY-OBS-001" for variant in variants)


def test_batch_completeness_rejects_a_missing_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_dir = tmp_path / "manifests"
    receipt_dir = tmp_path / "receipts"
    manifest_dir.mkdir()
    receipt_dir.mkdir()
    manifest = {
        "test_variants": [
            {
                "test_id": "DAL-T-X",
                "variant_id": "one",
                "run_gate": "G1",
                "owner_tasks": ["DAL-007"],
            },
            {
                "test_id": "DAL-T-LATER",
                "variant_id": "not-in-scope",
                "run_gate": "G1",
                "owner_tasks": ["DAL-045"],
            },
        ]
    }
    (manifest_dir / "test-manifest_v1.2.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(batch.verifier, "MANIFESTS", manifest_dir)

    with pytest.raises(ValueError, match="G1 receipt denominator mismatch"):
        batch._verify_g1_completeness([])

    receipt = receipt_dir / "one.json"
    receipt.write_text(
        json.dumps(
            {"test_id": "DAL-T-X", "variant_id": "one", "run_gate": "G1"}
        )
    )
    assert batch._verify_g1_completeness([receipt]) == 1


def test_generator_rejects_non_g1_filter_without_writing() -> None:
    assert generator.main(["--run-gate", "G2", "--dry-run"]) == 2
