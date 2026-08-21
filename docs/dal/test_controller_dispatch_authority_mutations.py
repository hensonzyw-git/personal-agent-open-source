#!/usr/bin/env python3
"""Prove coherent generator-source controller-dispatch mutations fail closed.

Mutates ``build_controller_dispatch_manifests.py`` at three semantic anchors
(node classification, a resulting command_type, a block_feature route target),
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

NODE_SOURCE = '    ("verified", "deterministic", "merge_candidate_sha_binding",'
COMMAND_SOURCE = '     [("record_plan", "awaiting_plan_review", None)]),'
ROUTE_SOURCE = '    ("AUTH_REQUIRED", ["planning", "coding", "reviewing", "fixing"], "blocked_auth"),'

MUTATIONS = {
    "node_type": (NODE_SOURCE, NODE_SOURCE.replace('"deterministic"', '"gate"')),
    "command_type": (COMMAND_SOURCE, COMMAND_SOURCE.replace('"record_plan"', '"record_review/pass"')),
    "block_route": (ROUTE_SOURCE, ROUTE_SOURCE.replace('"blocked_auth"', '"blocked_usage"')),
}


def main() -> None:
    source = (ROOT / "build_controller_dispatch_manifests.py").read_text(encoding="utf-8")
    for label, (anchor, _) in MUTATIONS.items():
        if source.count(anchor) != 1:
            raise RuntimeError(f"{label} source is no longer an exact mutation anchor")

    rejected: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dal-dispatch-mutation-") as temp_name:
        temp_root = Path(temp_name)
        for label, (anchor, replacement) in MUTATIONS.items():
            case_root = temp_root / label
            manifests = case_root / "manifests"
            manifests.mkdir(parents=True)
            (case_root / "build_controller_dispatch_manifests.py").write_text(
                source.replace(anchor, replacement), encoding="utf-8")
            for filename in (
                "dal_jcs.py", "verify_controller_dispatch_authority.py",
                "transition-spec-registry_v1.0.json",
                "controller-dispatch-authority_v1.0.json",
            ):
                src = ROOT / filename if filename.endswith(".py") else ROOT / "manifests" / filename
                dst = case_root / filename if filename.endswith(".py") else manifests / filename
                shutil.copy2(src, dst)

            build = subprocess.run(
                [sys.executable, str(case_root / "build_controller_dispatch_manifests.py")],
                cwd=case_root, text=True, capture_output=True, check=False,
            )
            if build.returncode != 0:
                raise RuntimeError(f"{label} mutated generator failed to build:\n{build.stderr[-2000:]}")

            verify = subprocess.run(
                [sys.executable, str(case_root / "verify_controller_dispatch_authority.py"),
                 "--directory", str(manifests),
                 "--authority", str(manifests / "controller-dispatch-authority_v1.0.json")],
                cwd=case_root, text=True, capture_output=True, check=False,
            )
            combined = verify.stdout + verify.stderr
            if verify.returncode == 0:
                raise RuntimeError(f"{label} mutation incorrectly rebuilt green")
            if "dispatch authority" not in combined and "AuthorityVerificationError" not in combined:
                raise RuntimeError(f"{label} mutation did not reach dispatch authority:\n{combined[-3000:]}")
            rejected.append(label)

    print(json.dumps({"coherent_dispatch_generator_mutations_rejected": rejected}, sort_keys=True))


if __name__ == "__main__":
    main()
