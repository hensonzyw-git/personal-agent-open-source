#!/usr/bin/env python3
"""Generate per-variant G3 ``dal.test-receipt/1.0`` for the Wave 3 G3 variants.

Wave 3 (DAL-021+) freezes five test ids. Their gates split:

- **G3** (offline replayable pure decisions): the four
  ``DAL-T-REVIEW-INDEP-001`` independence variants, the nine
  ``DAL-T-PLAN-XFIELD-001`` cross-field variants, the six
  ``DAL-T-DISPOSITION-001`` disposition variants, the seven
  ``DAL-T-OPENSET-001`` open-set variants and the twelve
  ``DAL-T-FIXDIFF-001`` post-fix verdict variants. These — and only these —
  may earn an offline PASS receipt, which is what this generator issues.
- **G4** (the eight PROVIDER-CONTRACT streams and REVIEW-INDEP's
  `live_fresh`): earning those receipts requires the real Codex adapter
  subprocess, blocked until DAL-006 §6.1 is revised (File-mode No-Go). This
  generator refuses to emit receipts for them — writing one now would claim a
  live run that never happened.

For each eligible variant this replays the **same** executor, oracle
comparator and write-set/side-effect checks the owning test file uses. A
variant that diverges is a hard failure: no PASS receipt is ever written for a
failing variant, and the whole run exits non-zero.

The receipts bind an existing implementation commit (``--implementation-sha``):
they are generated *after* that commit exists so the SHA is not
self-referential. ``--test-id``/``--variant-id`` narrow the run to a single
variant, which is the command each result artifact records for reproduction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.dal.contract_loader import (  # noqa: E402
    FrozenContracts,
    canonical_bytes,
    content_hash,
)
from tests.dal.side_effects import fresh_probe  # noqa: E402
from tests.dal.oracle_comparator import compare  # noqa: E402
from tests.dal.review_independence_executor import (  # noqa: E402
    execute_review_independence_fixture,
)
from tests.dal.plan_cross_fields_executor import (  # noqa: E402
    execute_plan_cross_fields_fixture,
)
from tests.dal.review_disposition_executor import (  # noqa: E402
    execute_review_disposition_fixture,
)
from tests.dal.open_finding_set_executor import (  # noqa: E402
    execute_open_finding_set_fixture,
)
from tests.dal.post_fix_verdict_executor import (  # noqa: E402
    execute_post_fix_verdict_fixture,
)

RESULT_SCHEMA = "dal.test-result/1.0"
RECEIPT_SCHEMA = "dal.test-receipt/1.0"

RESULTS_DIR = REPO_ROOT / "docs" / "evidence" / "results"
RECEIPTS_DIR = REPO_ROOT / "docs" / "evidence" / "receipts"

#: The test ids whose G3 variants exist in Wave 3 today. The closed-set check
#: below fails the run if the frozen manifest grows or shrinks this surface,
#: so a manifest change can never silently widen the receipt set.
WAVE3_G3_TEST_IDS: frozenset[str] = frozenset(
    {
        "DAL-T-REVIEW-INDEP-001",
        "DAL-T-PLAN-XFIELD-001",
        "DAL-T-DISPOSITION-001",
        "DAL-T-OPENSET-001",
        "DAL-T-FIXDIFF-001",
    }
)

#: Per-test-id G3 variant names. The variant surface is not a cross product:
#: each test id owns its own named subset, and the closed-set check below must
#: reproduce that exact map so a manifest drift fails instead of widening.
EXPECTED_G3_VARIANTS: dict[str, frozenset[str]] = {
    "DAL-T-REVIEW-INDEP-001": frozenset(
        {"same_session", "same_context", "same_independence_key", "synthetic_fresh"}
    ),
    "DAL-T-PLAN-XFIELD-001": frozenset(
        {
            "plan_complete", "identity_mismatch", "paths_overlap_file_in_dir",
            "paths_overlap_dir_in_dir", "paths_overlap_equal", "order_gap",
            "dependency_not_earlier", "unknown_verification", "digest_drift",
        }
    ),
    "DAL-T-DISPOSITION-001": frozenset(
        {
            "approve_clean", "request_changes_findings", "request_changes_gaps",
            "coverage_incomplete", "provider_approve_with_findings",
            "provider_request_changes_clean",
        }
    ),
    "DAL-T-OPENSET-001": frozenset(
        {
            "init_from_review", "carry_forward_exact", "remaining_declared",
            "carried_finding_omitted", "carried_finding_renamed",
            "new_finding_id_reused", "verified_with_new_findings",
            "original_remaining_omitted", "prior_closed_carried_not_touched",
        }
    ),
    "DAL-T-FIXDIFF-001": frozenset(
        {
            "verified_clean", "gap_closed_by_test_receipts",
            "changes_requested_declared", "changes_requested_new_findings_only",
            "evidence_role_violation",
            "anchor_entry_not_blob", "path_died_between_rounds",
            "surviving_set_empty", "increment_missed_surviving_lines",
            "no_deletion_in_increment", "gap_closed_by_fix_diff_only",
            "new_finding_anchor_mismatch", "verified_with_unverified_acceptance",
        }
    ),
}


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# ---------------------------------------------------------------------------
# Replay. A transcription of each owning test file's replay loop, calling the
# same executor + comparator + write-set and side-effect checks.
# ---------------------------------------------------------------------------

def _replay(executor: Callable[[Any, Any], Any], variant) -> list[str]:
    probe = fresh_probe()
    trace = executor(variant.fixture.body, probe=probe)
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    if set(trace.declared_write_set) != set(trace.write_set):
        divergences.append(
            "declared write set differs from observed write set: "
            f"{trace.declared_write_set!r} != {trace.write_set!r}"
        )
    if probe.observed:
        divergences.append(f"unexpected side effects: {sorted(probe.observed)!r}")
    return divergences


DISPATCH: dict[str, Callable[[Any], list[str]]] = {
    "DAL-T-REVIEW-INDEP-001": lambda v: _replay(
        execute_review_independence_fixture, v
    ),
    "DAL-T-PLAN-XFIELD-001": lambda v: _replay(
        execute_plan_cross_fields_fixture, v
    ),
    "DAL-T-DISPOSITION-001": lambda v: _replay(
        execute_review_disposition_fixture, v
    ),
    "DAL-T-OPENSET-001": lambda v: _replay(
        execute_open_finding_set_fixture, v
    ),
    "DAL-T-FIXDIFF-001": lambda v: _replay(
        execute_post_fix_verdict_fixture, v
    ),
}


# ---------------------------------------------------------------------------
# Artifact construction
# ---------------------------------------------------------------------------

def _implementation_paths() -> list[str]:
    """The DAL implementation surface the receipts bind, repo-relative."""
    roots = [
        REPO_ROOT / "src" / "personal_agent_dal",
        REPO_ROOT / "tests" / "dal",
        REPO_ROOT / "docs" / "dal" / "dal_jcs.py",
        REPO_ROOT / "scripts" / "verify_dal_test_receipt.py",
        REPO_ROOT / "scripts" / "verify_all_dal_receipts.py",
        REPO_ROOT / "scripts" / "generate_dal_g1_receipts.py",
        REPO_ROOT / "scripts" / "generate_dal_g2_receipts.py",
        REPO_ROOT / "scripts" / "generate_dal_wave3_g3_receipts.py",
    ]
    paths: list[str] = []
    for root in roots:
        if root.is_dir():
            paths.extend(
                str(p.relative_to(REPO_ROOT)) for p in sorted(root.rglob("*.py"))
            )
        elif root.is_file():
            paths.append(str(root.relative_to(REPO_ROOT)))
    return sorted(set(paths))


def _environment() -> dict[str, Any]:
    return {
        "architecture": platform.machine(),
        "os": sys.platform,
        "python_version": platform.python_version(),
        "runner": "scripts/generate_dal_wave3_g3_receipts.py",
    }


def _build_artifacts(
    variant,
    implementation_sha: str,
    implementation_paths: list[str],
    recorded_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (result, receipt) dicts for one passing variant."""
    command = [
        "uv", "run", "python", "scripts/generate_dal_wave3_g3_receipts.py",
        "--test-id", variant.test_id,
        "--variant-id", variant.variant_id,
        "--run-gate", variant.run_gate,
    ]
    result = {
        "schema_version": RESULT_SCHEMA,
        "test_id": variant.test_id,
        "variant_id": variant.variant_id,
        "run_gate": variant.run_gate,
        "command": command,
        "exit_code": 0,
        "status": "PASS",
        "passed": 1,
        "skipped": 0,
    }

    environment = _environment()
    owner_tasks = list(variant.owner_tasks)

    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "manifest_sha256": _manifest_sha256(),
        "test_id": variant.test_id,
        "variant_id": variant.variant_id,
        "run_gate": variant.run_gate,
        "owner_tasks_sha256": _sha256(owner_tasks),
        "fixture_sha256": content_hash(variant.fixture.body),
        "oracle_id": variant.oracle.oracle_id,
        "oracle_sha256": content_hash(variant.oracle.body),
        "implementation_sha": implementation_sha,
        "implementation_paths": implementation_paths,
        "result_path": str(
            (RESULTS_DIR / _stem(variant)).relative_to(REPO_ROOT)
        ),
        "result_sha": _sha256(result),
        "environment": environment,
        "environment_digest": _sha256(environment),
        "status": "PASS",
        "recorded_at": recorded_at,
    }
    return result, receipt


def _stem(variant) -> str:
    return f"{variant.test_id}__{variant.variant_id}__{variant.run_gate}.json"


_manifest_sha_cache: str | None = None


def _manifest_sha256() -> str:
    global _manifest_sha_cache
    if _manifest_sha_cache is None:
        manifest = json.loads(
            (REPO_ROOT / "docs" / "dal" / "manifests" / "test-manifest_v1.2.json")
            .read_text()
        )
        _manifest_sha_cache = manifest["manifest_sha256"]
    return _manifest_sha_cache


def _head_sha() -> str:
    """The current HEAD commit, used as the default implementation binding."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _validate_implementation_sha(implementation_sha: str, paths: list[str]) -> None:
    """Require a real ancestor commit whose bound implementation is unchanged.

    The ``git diff`` below compares ``implementation_sha`` against the
    **worktree** (no ``HEAD``), not against a commit. That is deliberate:
    generation happens *before* the receipt files are committed, so the
    invariant is "the implementation on disk still matches the commit being
    bound". The verifier (`verify_dal_test_receipt.py`) runs after the receipt
    is committed and therefore compares ``implementation_sha`` against
    ``HEAD`` instead — same intent, different reference point.
    """
    subprocess.run(
        ["git", "cat-file", "-e", f"{implementation_sha}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", implementation_sha, "HEAD"],
        cwd=REPO_ROOT,
    )
    if ancestor.returncode != 0:
        raise ValueError(f"implementation SHA is not an ancestor of HEAD: {implementation_sha}")
    unchanged = subprocess.run(
        ["git", "diff", "--quiet", implementation_sha, "--", *paths],
        cwd=REPO_ROOT,
    )
    if unchanged.returncode != 0:
        raise ValueError(
            "implementation paths differ from --implementation-sha; commit the exact "
            "replayed implementation before generating receipts"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _g3_variants(frozen: FrozenContracts, filter_test_id: str | None,
                 filter_variant_id: str | None) -> list[Any]:
    eligible = [
        variant
        for variant in frozen.all_variants()
        if variant.run_gate == "G3" and variant.test_id in WAVE3_G3_TEST_IDS
    ]
    # Closed-set proof: the receipt denominator must be exactly the frozen
    # per-test-id variant subsets — a manifest drift (added, renamed or
    # regated variant) fails the run instead of silently widening it.
    seen = {(variant.test_id, variant.variant_id) for variant in eligible}
    expected = {
        (test_id, variant_id)
        for test_id, variant_ids in EXPECTED_G3_VARIANTS.items()
        for variant_id in variant_ids
    }
    if seen != expected:
        raise ValueError(
            "Wave 3 G3 variant set drifted: "
            f"missing={sorted(expected - seen)}, extra={sorted(seen - expected)}"
        )
    return [
        variant
        for variant in eligible
        if (filter_test_id is None or variant.test_id == filter_test_id)
        and (filter_variant_id is None or variant.variant_id == filter_variant_id)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation-sha", default=None,
                        help="existing commit SHA the receipts bind (40 hex chars)")
    parser.add_argument("--recorded-at", default=None,
                        help="ISO 8601 UTC timestamp for recorded_at (default: now)")
    parser.add_argument("--test-id", default=None)
    parser.add_argument("--variant-id", default=None)
    parser.add_argument("--run-gate", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="replay every variant and report, but write nothing")
    args = parser.parse_args(argv)

    if args.run_gate not in (None, "G3"):
        print(f"ERROR: only --run-gate G3 is supported: {args.run_gate!r}", file=sys.stderr)
        return 2

    if not args.dry_run:
        implementation_sha = args.implementation_sha or _head_sha()
        if (len(implementation_sha) != 40
                or any(c not in "0123456789abcdef" for c in implementation_sha)):
            print(f"ERROR: --implementation-sha must be 40 lowercase hex chars: {implementation_sha!r}",
                  file=sys.stderr)
            return 2
    else:
        implementation_sha = args.implementation_sha or "0" * 40

    recorded_at = args.recorded_at or (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    implementation_paths = _implementation_paths()
    if not args.dry_run:
        try:
            _validate_implementation_sha(implementation_sha, implementation_paths)
        except (subprocess.CalledProcessError, ValueError) as error:
            print(f"ERROR: invalid implementation binding: {error}", file=sys.stderr)
            return 2

    try:
        frozen = FrozenContracts()
        variants = _g3_variants(frozen, args.test_id, args.variant_id)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if not variants:
        print("no Wave 3 G3 variants to generate", file=sys.stderr)
        return 2

    if not args.dry_run:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    artifacts: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    for variant in variants:
        divergences = DISPATCH[variant.test_id](variant)
        if divergences:
            failures.append(
                f"{variant.test_id}/{variant.variant_id}: " + "; ".join(divergences)
            )
            continue
        if not args.dry_run:
            result, receipt = _build_artifacts(
                variant, implementation_sha, implementation_paths, recorded_at
            )
            artifacts.append((variant, result, receipt))

    if failures:
        print(f"{len(failures)} of {len(variants)} variants diverged — no receipts written:",
              file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"dry run: all {len(variants)} variants replayed cleanly")
        return 0

    for variant, result, receipt in artifacts:
        stem = _stem(variant)
        result_path = RESULTS_DIR / stem
        receipt_path = RECEIPTS_DIR / stem
        result_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        receipt_path.write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    print(
        f"wrote {len(artifacts)} result + {len(artifacts)} receipt "
        f"files (implementation_sha={implementation_sha})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
