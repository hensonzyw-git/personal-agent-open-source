#!/usr/bin/env python3
"""Prove a coherent generator-source manifest gate mutation fails closed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = '            "owner_tasks": owners,\n            "run_gate": gate,\n        })'
MUTATED = '            "owner_tasks": owners,\n            "run_gate": "G5",\n        })'


def main() -> None:
    source = (ROOT / "build_contract_manifests.py").read_text(encoding="utf-8")
    if source.count(SOURCE) != 1:
        raise RuntimeError("manifest run_gate source is no longer an exact mutation anchor")
    with tempfile.TemporaryDirectory(prefix="dal-manifest-mutation-") as temp_name:
        case_root = Path(temp_name)
        manifests = case_root / "manifests"
        manifests.mkdir()
        (case_root / "build_contract_manifests.py").write_text(source.replace(SOURCE, MUTATED), encoding="utf-8")
        for filename in (
            "dal_jcs.py", "verify_transition_oracle_authority.py",
            "verify_operation_oracle_authority.py", "verify_test_manifest_authority.py",
        ):
            shutil.copy2(ROOT / filename, case_root)
        for filename in (
            "transition-oracle-authority_v1.0.json", "operation-oracle-authority_v1.0.json",
            "test-manifest-authority_v1.0.json",
        ):
            shutil.copy2(ROOT / "manifests" / filename, manifests)
        result = subprocess.run(
            [sys.executable, str(case_root / "build_contract_manifests.py")],
            cwd=case_root, text=True, capture_output=True, check=False,
        )
        combined = result.stdout + result.stderr
        if result.returncode == 0:
            raise RuntimeError("manifest run_gate source mutation incorrectly rebuilt green")
        if "manifest" not in combined or "authority" not in combined:
            raise RuntimeError(f"mutation did not reach manifest authority:\n{combined[-3000:]}")
    print(json.dumps({"coherent_generator_mutations_rejected": ["manifest_run_gate"]}, sort_keys=True))


if __name__ == "__main__":
    main()
