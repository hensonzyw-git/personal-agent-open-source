#!/usr/bin/env python3
"""Prove four coherent generator-source mutations cannot refresh the oracle."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_ROW = (
    '        ("SM-CREATE", None, "intake", "create_feature", "feature.created", '
    '["service"], ["workflow-service"], "dal.evidence.feature/1.0", None, "A"),'
)
MUTATIONS = {
    "command": SOURCE_ROW.replace('"create_feature"', '"create_feature_mutated"'),
    "event": SOURCE_ROW.replace('"feature.created"', '"feature.created_mutated"'),
    "to_state": SOURCE_ROW.replace('None, "intake",', 'None, "planning",'),
    "write_set": SOURCE_ROW[:-5] + '"D"),',
}


def main() -> None:
    source = (ROOT / "build_contract_manifests.py").read_text(encoding="utf-8")
    if source.count(SOURCE_ROW) != 1:
        raise RuntimeError("SM-CREATE source row is no longer an exact mutation anchor")
    rejected: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dal-oracle-mutation-") as temp_name:
        temp_root = Path(temp_name)
        for label, replacement in MUTATIONS.items():
            case_root = temp_root / label
            manifests = case_root / "manifests"
            manifests.mkdir(parents=True)
            (case_root / "build_contract_manifests.py").write_text(
                source.replace(SOURCE_ROW, replacement),
                encoding="utf-8",
            )
            shutil.copy2(ROOT / "verify_transition_oracle_authority.py", case_root)
            shutil.copy2(ROOT / "manifests" / "transition-oracle-authority_v1.0.json", manifests)
            result = subprocess.run(
                [sys.executable, str(case_root / "build_contract_manifests.py")],
                cwd=case_root,
                text=True,
                capture_output=True,
                check=False,
            )
            combined = result.stdout + result.stderr
            if result.returncode == 0:
                raise RuntimeError(f"{label} mutation incorrectly rebuilt green")
            if "independent authority" not in combined:
                raise RuntimeError(f"{label} mutation did not reach the independent authority check:\n{combined[-2000:]}")
            rejected.append(label)
    print(json.dumps({"coherent_generator_mutations_rejected": rejected}, sort_keys=True))


if __name__ == "__main__":
    main()
