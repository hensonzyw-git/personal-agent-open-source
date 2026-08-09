#!/usr/bin/env python3
"""Prove coherent generator-source operation/evidence mutations fail closed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
COMMAND_SOURCE = '    "DAL-T-APP-001": "approve_plan",'
EVIDENCE_SOURCE = '        "auth-probe": (),'
MUTATIONS = {
    "operation_command": (COMMAND_SOURCE, COMMAND_SOURCE.replace('"approve_plan"', '"approve_plan_mutated"')),
    "evidence_fields": (EVIDENCE_SOURCE, EVIDENCE_SOURCE.replace("()", '("approval",)')),
}


def main() -> None:
    source = (ROOT / "build_contract_manifests.py").read_text(encoding="utf-8")
    for label, (anchor, _) in MUTATIONS.items():
        if source.count(anchor) != 1:
            raise RuntimeError(f"{label} source is no longer an exact mutation anchor")
    rejected: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dal-operation-mutation-") as temp_name:
        temp_root = Path(temp_name)
        for label, (anchor, replacement) in MUTATIONS.items():
            case_root = temp_root / label
            manifests = case_root / "manifests"
            manifests.mkdir(parents=True)
            (case_root / "build_contract_manifests.py").write_text(source.replace(anchor, replacement), encoding="utf-8")
            for filename in (
                "dal_jcs.py", "verify_transition_oracle_authority.py", "verify_operation_oracle_authority.py",
            ):
                shutil.copy2(ROOT / filename, case_root)
            for filename in (
                "transition-oracle-authority_v1.0.json", "operation-oracle-authority_v1.0.json",
            ):
                shutil.copy2(ROOT / "manifests" / filename, manifests)
            result = subprocess.run(
                [sys.executable, str(case_root / "build_contract_manifests.py")],
                cwd=case_root, text=True, capture_output=True, check=False,
            )
            combined = result.stdout + result.stderr
            if result.returncode == 0:
                raise RuntimeError(f"{label} mutation incorrectly rebuilt green")
            if "operation authority" not in combined:
                raise RuntimeError(f"{label} mutation did not reach operation authority:\n{combined[-3000:]}")
            rejected.append(label)
    print(json.dumps({"coherent_generator_mutations_rejected": rejected}, sort_keys=True))


if __name__ == "__main__":
    main()
