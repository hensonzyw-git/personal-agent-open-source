#!/usr/bin/env python3
"""Prove coherent generator-source verification mutations fail closed.

Mutates ``build_verification_manifests.py`` at four semantic anchors (the
command-swap detection, a reason-to-state target, a diff-integrity branch, and
the report-body base_sha binding), rebuilds the manifests in an isolated
directory, and proves the frozen authority rejects each single-sided change.
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
COMMAND_SWAP_SOURCE = '        if list(observed_command) != list(declared):'
REASON_TARGET_SOURCE = '    "TEST_BLOCKED": "blocked_test",'
DIFF_INTEGRITY_SOURCE = '    if injected["diff_sha"] != _sha256_text(injected["diff"]):'
REPORT_BODY_SOURCE = '        "base_sha": facts["base_sha"],'

MUTATIONS = {
    "command_swap": (
        COMMAND_SWAP_SOURCE, COMMAND_SWAP_SOURCE.replace('if list(', 'if False and list('),
    ),
    "reason_target": (
        REASON_TARGET_SOURCE, REASON_TARGET_SOURCE.replace('"blocked_test"', '"needs_human"'),
    ),
    "diff_integrity": (
        DIFF_INTEGRITY_SOURCE, DIFF_INTEGRITY_SOURCE.replace('if injected', 'if False and injected'),
    ),
    "report_body": (
        REPORT_BODY_SOURCE, REPORT_BODY_SOURCE.replace('facts["base_sha"]', '"tampered"'),
    ),
}


def main() -> None:
    source = (ROOT / "build_verification_manifests.py").read_text(encoding="utf-8")
    for label, (anchor, _) in MUTATIONS.items():
        if source.count(anchor) != 1:
            raise RuntimeError(f"{label} source is no longer an exact mutation anchor")

    rejected: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dal-verification-mutation-") as temp_name:
        temp_root = Path(temp_name)
        for label, (anchor, replacement) in MUTATIONS.items():
            case_root = temp_root / label
            manifests = case_root / "manifests"
            manifests.mkdir(parents=True)
            (case_root / "build_verification_manifests.py").write_text(
                source.replace(anchor, replacement), encoding="utf-8")
            for filename in (
                "dal_jcs.py", "verify_verification_authority.py",
                "verification-authority_v1.0.json",
            ):
                src = ROOT / filename if filename.endswith(".py") else ROOT / "manifests" / filename
                dst = case_root / filename if filename.endswith(".py") else manifests / filename
                shutil.copy2(src, dst)

            build = subprocess.run(
                [sys.executable, str(case_root / "build_verification_manifests.py")],
                cwd=case_root, text=True, capture_output=True, check=False,
            )
            if build.returncode != 0:
                raise RuntimeError(f"{label} mutated generator failed to build:\n{build.stderr[-2000:]}")

            verify = subprocess.run(
                [sys.executable, str(case_root / "verify_verification_authority.py"),
                 "--directory", str(manifests),
                 "--authority", str(manifests / "verification-authority_v1.0.json")],
                cwd=case_root, text=True, capture_output=True, check=False,
            )
            combined = verify.stdout + verify.stderr
            if verify.returncode == 0:
                raise RuntimeError(f"{label} mutation incorrectly rebuilt green")
            if "verification authority" not in combined and "AuthorityVerificationError" not in combined:
                raise RuntimeError(f"{label} mutation did not reach verification authority:\n{combined[-3000:]}")
            rejected.append(label)

    print(json.dumps({"coherent_verification_generator_mutations_rejected": rejected}, sort_keys=True))


if __name__ == "__main__":
    main()
