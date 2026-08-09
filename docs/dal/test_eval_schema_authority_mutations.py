#!/usr/bin/env python3
"""Prove a coherent eval-schema generator-source mutation fails closed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = '        "required": ["case_id", *digest_fields],'
MUTATED = '        "required": ["case_id", *digest_fields[:-1]],'


def main() -> None:
    source = (ROOT / "build_contract_manifests.py").read_text(encoding="utf-8")
    if source.count(SOURCE) != 1:
        raise RuntimeError("eval run-input required source is no longer an exact mutation anchor")
    with tempfile.TemporaryDirectory(prefix="dal-eval-schema-mutation-") as temp_name:
        case_root = Path(temp_name)
        manifests = case_root / "manifests"
        manifests.mkdir()
        (case_root / "build_contract_manifests.py").write_text(
            source.replace(SOURCE, MUTATED), encoding="utf-8",
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
            raise RuntimeError("eval-schema source mutation incorrectly rebuilt green")
        if "eval schema" not in combined or "independent authority" not in combined:
            raise RuntimeError(f"mutation did not reach eval authority:\n{combined[-3000:]}")
    print(json.dumps({"coherent_generator_mutations_rejected": ["eval_tool_policy_required"]}, sort_keys=True))


if __name__ == "__main__":
    main()
