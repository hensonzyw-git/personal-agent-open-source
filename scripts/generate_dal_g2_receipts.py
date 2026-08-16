#!/usr/bin/env python3
"""Generate per-variant ``dal.test-receipt/1.0`` + ``dal.test-result/1.0`` for
every DAL-G2 variant owned by DAL-014..020.

This generator closes only the frozen **G2 policy-receipt sub-gate**: each
manifest variant must be bound by a receipt proving that exact pure-policy
scenario executed and satisfied its oracle (threat model §6.1). It replays the
exact 47-variant denominator owned by DAL-014..020 — GitHub intake judgement,
git facts, lease/epoch decisions, and worker-isolation guards. These receipts do
not prove the executable DAL-G2 acceptance gate, which separately requires a
credential-free real-project branch-only run, launchd composition and restart
recovery evidence.

For each variant it replays the variant through the **same** executor, oracle
comparator and persisted-divergence checks its owning test file uses — it does
not invent a second, weaker judge. A variant that diverges is a hard failure:
no ``PASS`` receipt is ever written for a failing variant, and the whole run
exits non-zero.

The receipts bind an existing implementation commit (``--implementation-sha``):
they are generated *after* that commit exists so the SHA is not
self-referential. ``--test-id``/``--variant-id`` narrow the run to a single
variant, which is the command each result artifact records for reproduction.

This script is separate from ``generate_dal_g1_receipts.py`` because the G1
generator is itself bound by the G1 receipts' ``implementation_paths`` and must
stay byte-identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
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

# Executors — the public, importable counterparts of the test files' replay loops.
from tests.dal.gh_event_executor import execute_gh_event_fixture  # noqa: E402
from tests.dal.epoch_executor import execute_epoch_fixture  # noqa: E402
from tests.dal.state_machine_executor import (  # noqa: E402
    execute_state_machine_fixture,
    operation_persisted_divergences,
)

RESULT_SCHEMA = "dal.test-result/1.0"
RECEIPT_SCHEMA = "dal.test-receipt/1.0"

RESULTS_DIR = REPO_ROOT / "docs" / "evidence" / "results"
RECEIPTS_DIR = REPO_ROOT / "docs" / "evidence" / "receipts"

G2_OWNER_TASKS: frozenset[str] = frozenset(
    {"DAL-014", "DAL-015", "DAL-016", "DAL-017", "DAL-018", "DAL-019", "DAL-020"}
)


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# ---------------------------------------------------------------------------
# Per-test-id replay. Each returns the list of divergences; empty == PASS.
# These are transcriptions of the owning test file's replay loop, calling the
# same executor + comparator + persisted-divergence functions.
# ---------------------------------------------------------------------------

def _replay_gh_event(variant, database: Path) -> list[str]:
    """Pure GitHub intake matrix (test_github.py)."""
    probe = fresh_probe()
    trace = execute_gh_event_fixture(variant.fixture.body, probe=probe)
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    if set(trace.declared_write_set) != set(trace.write_set):
        divergences.append(
            "declared write set differs from observed write set: "
            f"{trace.declared_write_set!r} != {trace.write_set!r}"
        )
    if probe.observed:
        divergences.append(f"unexpected side effects: {sorted(probe.observed)!r}")
    return divergences


def _replay_epoch(variant, database: Path) -> list[str]:
    """Pure epoch-bound result boundary (test_epoch.py)."""
    probe = fresh_probe()
    trace = execute_epoch_fixture(variant.fixture.body, probe=probe)
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    if set(trace.declared_write_set) != set(trace.write_set):
        divergences.append(
            "declared write set differs from observed write set: "
            f"{trace.declared_write_set!r} != {trace.write_set!r}"
        )
    if probe.observed:
        divergences.append(f"unexpected side effects: {sorted(probe.observed)!r}")
    return divergences


def _replay_state_machine_operation(variant, database: Path) -> list[str]:
    """DB-backed worker lifecycle (lease/REC/RESTART), with persisted-divergence checks."""
    db = database / "op.db"
    trace = execute_state_machine_fixture(variant.fixture.body, database=db, probe=fresh_probe())
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    divergences.extend(operation_persisted_divergences(db, variant.fixture.body))
    return divergences


def _replay_state_machine_block(variant, database: Path) -> list[str]:
    """DB-backed ``block_feature`` transitions (git-base/injection-G2/guards)."""
    db = database / "block.db"
    trace = execute_state_machine_fixture(variant.fixture.body, database=db, probe=fresh_probe())
    return list(compare(trace, variant.oracle.body).mismatches)


#: test_id -> replay function. The grouping mirrors the owning test file.
DISPATCH: dict[str, Callable[[Any, Path], list[str]]] = {
    "DAL-T-GH-EVENT-001": _replay_gh_event,
    "DAL-T-GIT-BASE-001": _replay_state_machine_block,
    "DAL-T-INJECTION-001": _replay_state_machine_block,
    "DAL-T-EPOCH-001": _replay_epoch,
    "DAL-T-LEASE-001": _replay_state_machine_operation,
    "DAL-T-REC-001": _replay_state_machine_operation,
    "DAL-T-RESTART-001": _replay_state_machine_operation,
    "DAL-T-PATH-001": _replay_state_machine_block,
    "DAL-T-NET-001": _replay_state_machine_block,
    "DAL-T-CRED-001": _replay_state_machine_block,
    "DAL-T-SECRET-OUTPUT-001": _replay_state_machine_block,
}


def _replay_for(test_id: str, variant, db_dir: Path) -> list[str]:
    return DISPATCH[test_id](variant, db_dir)


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
        REPO_ROOT / "scripts" / "verify_all_dal_g2_receipts.py",
        REPO_ROOT / "scripts" / "generate_dal_g2_receipts.py",
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
        "runner": "scripts/generate_dal_g2_receipts.py",
    }


def _build_artifacts(
    variant,
    implementation_sha: str,
    implementation_paths: list[str],
    recorded_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (result, receipt) dicts for one passing variant."""
    command = [
        "uv", "run", "python", "scripts/generate_dal_g2_receipts.py",
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
    """Require a real ancestor commit whose bound implementation is unchanged."""
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

def _g2_variants(frozen: FrozenContracts, filter_test_id: str | None,
                 filter_variant_id: str | None) -> list[Any]:
    eligible = [
        variant
        for variant in frozen.all_variants()
        if variant.run_gate == "G2"
        and G2_OWNER_TASKS.intersection(variant.owner_tasks)
    ]
    expected_dispatch = {variant.test_id for variant in eligible}
    if set(DISPATCH) != expected_dispatch:
        missing = sorted(expected_dispatch - set(DISPATCH))
        extra = sorted(set(DISPATCH) - expected_dispatch)
        raise ValueError(
            f"G2 replay dispatch is not closed: missing={missing}, extra={extra}"
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

    if args.run_gate not in (None, "G2"):
        print(f"ERROR: only --run-gate G2 is supported: {args.run_gate!r}", file=sys.stderr)
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

    frozen = FrozenContracts()
    variants = _g2_variants(frozen, args.test_id, args.variant_id)
    if not variants:
        print("no G2 variants to generate", file=sys.stderr)
        return 2

    if not args.dry_run:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    artifacts: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    with tempfile.TemporaryDirectory(prefix="dal-g2-") as tmp:
        tmp_root = Path(tmp)
        for variant in variants:
            # A fresh database directory per variant: the owning test file uses
            # a unique tmp_path per variant, and reusing a DB across variants
            # would leak the previous variant's rows into the next replay.
            db_dir = tmp_root / f"{variant.test_id}__{variant.variant_id}"
            db_dir.mkdir()
            divergences = _replay_for(variant.test_id, variant, db_dir)
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
