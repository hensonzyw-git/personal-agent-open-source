#!/usr/bin/env python3
"""Prove coherent manifest and catalog generator-source mutations fail closed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MUTATIONS = (
    (
        "manifest_run_gate",
        '            "owner_tasks": owners,\n            "run_gate": gate,\n        })',
        '            "owner_tasks": owners,\n            "run_gate": "G5",\n        })',
    ),
    (
        "fixture_catalog_root",
        'fixture_catalog = {"schema_version": "dal.test-fixture-catalog/1.0",',
        'fixture_catalog = {"x-unapproved": True, "schema_version": "dal.test-fixture-catalog/1.0",',
    ),
    (
        "oracle_catalog_root",
        'oracle_catalog = {"schema_version": "dal.test-oracle-catalog/1.0",',
        'oracle_catalog = {"x-unapproved": True, "schema_version": "dal.test-oracle-catalog/1.0",',
    ),
)


def main() -> None:
    source = (ROOT / "build_contract_manifests.py").read_text(encoding="utf-8")
    rejected: list[str] = []
    for label, anchor, replacement in MUTATIONS:
        if source.count(anchor) != 1:
            raise RuntimeError(f"{label} source is no longer an exact mutation anchor")
        with tempfile.TemporaryDirectory(prefix=f"dal-{label}-mutation-") as temp_name:
            case_root = Path(temp_name)
            manifests = case_root / "manifests"
            manifests.mkdir()
            (case_root / "build_contract_manifests.py").write_text(
                source.replace(anchor, replacement), encoding="utf-8",
            )
            for filename in (
                "dal_jcs.py", "verify_transition_oracle_authority.py",
                "verify_operation_oracle_authority.py", "verify_test_manifest_authority.py",
                "verify_eval_schema_authority.py",
            ):
                shutil.copy2(ROOT / filename, case_root)
            for filename in (
                "transition-oracle-authority_v1.0.json", "operation-oracle-authority_v1.0.json",
                "test-manifest-authority_v1.0.json", "eval-schema-authority_v1.0.json",
            ):
                shutil.copy2(ROOT / "manifests" / filename, manifests)
            result = subprocess.run(
                [sys.executable, str(case_root / "build_contract_manifests.py")],
                cwd=case_root, text=True, capture_output=True, check=False,
            )
            combined = result.stdout + result.stderr
            if result.returncode == 0:
                raise RuntimeError(f"{label} source mutation incorrectly rebuilt green")
            if "authority" not in combined:
                raise RuntimeError(f"{label} mutation did not reach an authority:\n{combined[-3000:]}")
            rejected.append(label)
    print(json.dumps({"coherent_generator_mutations_rejected": rejected}, sort_keys=True))


if __name__ == "__main__":
    main()
