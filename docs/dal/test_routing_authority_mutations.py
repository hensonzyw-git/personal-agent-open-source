#!/usr/bin/env python3
"""Prove coherent generator-source routing mutations fail closed.

Mutates ``build_routing_manifests.py`` at three semantic anchors (the fallback
eligibility set, a reason-to-state target, a classifier-integrity branch),
rebuilds the manifests in an isolated directory, and proves the frozen
authority rejects each single-sided change.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent

#: Each anchor is a unique source line whose single-sided edit changes at least
#: one frozen variant's classifier outcome while keeping the generator runnable.
ELIGIBILITY_SOURCE = 'FALLBACK_ALLOWED_CLASSES = ["auth", "contract_failure", "transient", "usage_limit"]'
REASON_TARGET_SOURCE = '    "POLICY_FAILURE": "needs_human",'
INTEGRITY_BRANCH_SOURCE = '    if observed.get("digest_pre") != digest_value:'

MUTATIONS = {
    "eligibility_set": (
        ELIGIBILITY_SOURCE,
        ELIGIBILITY_SOURCE.replace('"usage_limit"]', '"usage_limit", "budget_limit"]'),
    ),
    "reason_target": (
        REASON_TARGET_SOURCE, REASON_TARGET_SOURCE.replace('"needs_human"', '"blocked_usage"'),
    ),
    "integrity_branch": (
        INTEGRITY_BRANCH_SOURCE, INTEGRITY_BRANCH_SOURCE.replace('if observed', 'if False and observed'),
    ),
}


def main() -> None:
    source = (ROOT / "build_routing_manifests.py").read_text(encoding="utf-8")
    for label, (anchor, _) in MUTATIONS.items():
        if source.count(anchor) != 1:
            raise RuntimeError(f"{label} source is no longer an exact mutation anchor")

    rejected: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dal-routing-mutation-") as temp_name:
        temp_root = Path(temp_name)
        for label, (anchor, replacement) in MUTATIONS.items():
            case_root = temp_root / label
            manifests = case_root / "manifests"
            manifests.mkdir(parents=True)
            (case_root / "build_routing_manifests.py").write_text(
                source.replace(anchor, replacement), encoding="utf-8")
            for filename in (
                "dal_jcs.py", "verify_routing_authority.py",
                "routing-authority_v1.0.json",
            ):
                src = ROOT / filename if filename.endswith(".py") else ROOT / "manifests" / filename
                dst = case_root / filename if filename.endswith(".py") else manifests / filename
                shutil.copy2(src, dst)

            build = subprocess.run(
                [sys.executable, str(case_root / "build_routing_manifests.py")],
                cwd=case_root, text=True, capture_output=True, check=False,
            )
            if build.returncode != 0:
                raise RuntimeError(f"{label} mutated generator failed to build:\n{build.stderr[-2000:]}")

            verify = subprocess.run(
                [sys.executable, str(case_root / "verify_routing_authority.py"),
                 "--directory", str(manifests),
                 "--authority", str(manifests / "routing-authority_v1.0.json")],
                cwd=case_root, text=True, capture_output=True, check=False,
            )
            combined = verify.stdout + verify.stderr
            if verify.returncode == 0:
                raise RuntimeError(f"{label} mutation incorrectly rebuilt green")
            if "routing authority" not in combined and "AuthorityVerificationError" not in combined:
                raise RuntimeError(f"{label} mutation did not reach routing authority:\n{combined[-3000:]}")
            rejected.append(label)

    print(json.dumps({"coherent_routing_generator_mutations_rejected": rejected}, sort_keys=True))


if __name__ == "__main__":
    main()
