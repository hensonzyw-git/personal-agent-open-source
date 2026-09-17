#!/usr/bin/env python3
"""Generate per-variant ``dal.test-receipt/1.0`` + ``dal.test-result/1.0`` for
every remaining DAL-G1 variant owned by DAL-007..013.

The Development Agent Loop's G1 gate closes only when each frozen G1 variant is
bound by a receipt proving *that exact variant* was executed and its oracle
fully satisfied (threat model §6.1). This generator replays and signs the exact
1077-variant denominator owned by DAL-007..013, including DAL-012's injection
variant, so one closed dispatch and one naming convention cover the whole gate.

For each variant it replays the variant through the **same** executor, oracle
comparator and persisted-divergence checks its owning test file uses — it does
not invent a second, weaker judge. A variant that diverges is a hard failure:
no ``PASS`` receipt is ever written for a failing variant, and the whole run
exits non-zero.

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
from tests.dal.operation_executor import execute_fixture  # noqa: E402
from tests.dal.injection_executor import execute_injection_fixture  # noqa: E402
from tests.dal.binding_executor import (  # noqa: E402
    execute_binding_fixture,
    binding_persisted_divergences,
)
from tests.dal.effect_ownership_executor import (  # noqa: E402
    execute_ownership_fixture,
    ownership_persisted_divergences,
)
from tests.dal.transaction_executor import (  # noqa: E402
    execute_transaction_fixture,
    transaction_persisted_divergences,
)
from tests.dal.card_executor import (  # noqa: E402
    execute_card_fixture,
    card_persisted_divergences,
)
from tests.dal.dock_executor import (  # noqa: E402
    execute_dock_fixture,
    dock_persisted_divergences,
)
from tests.dal.batch_executor import execute_batch_fixture  # noqa: E402
from tests.dal.notify_executor import execute_notify_fixture  # noqa: E402
from tests.dal.approval_executor import (  # noqa: E402
    execute_approval_fixture,
    approval_persisted_divergences,
)
from tests.dal.state_machine_executor import (  # noqa: E402
    execute_state_machine_fixture,
    operation_persisted_divergences,
)
from tests.dal.transition_executor import (  # noqa: E402
    database_changed,
    persisted_content_divergences,
    run_scenario_fixture,
    run_transition_fixture,
    unobserved_members,
)
from tests.dal.test_evidence_binding import _build_trace  # noqa: E402

RESULT_SCHEMA = "dal.test-result/1.0"
RECEIPT_SCHEMA = "dal.test-receipt/1.0"

RESULTS_DIR = REPO_ROOT / "docs" / "evidence" / "results"
RECEIPTS_DIR = REPO_ROOT / "docs" / "evidence" / "receipts"

G1_OWNER_TASKS: frozenset[str] = frozenset(
    {"DAL-007", "DAL-008", "DAL-009", "DAL-010", "DAL-011", "DAL-012", "DAL-013"}
)


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# ---------------------------------------------------------------------------
# Per-test-id replay. Each returns the list of divergences; empty == PASS.
# These are transcriptions of the owning test file's replay loop, calling the
# same executor + comparator + persisted-divergence functions.
# ---------------------------------------------------------------------------

def _replay_config_isolation(variant, database: Path) -> list[str]:
    trace = execute_fixture(variant.fixture.body, probe=fresh_probe())
    return list(compare(trace, variant.oracle.body).mismatches)


def _replay_injection(variant, database: Path) -> list[str]:
    probe = fresh_probe()
    trace = execute_injection_fixture(variant.fixture.body, probe=probe)
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    if set(trace.declared_write_set) != set(trace.write_set):
        divergences.append(
            "declared write set differs from observed write set: "
            f"{trace.declared_write_set!r} != {trace.write_set!r}"
        )
    if probe.observed:
        divergences.append(f"unexpected side effects: {sorted(probe.observed)!r}")
    return divergences


def _replay_db_contract(variant, database: Path) -> list[str]:
    trace = execute_fixture(
        variant.fixture.body, probe=fresh_probe(), database=database / "dal.db"
    )
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    if set(trace.declared_write_set) != set(trace.write_set):
        divergences.append(
            f"declared {sorted(set(trace.declared_write_set))} "
            f"but the database shows {sorted(set(trace.write_set))}"
        )
    return divergences


def _replay_binding(variant, database: Path) -> list[str]:
    trace = execute_binding_fixture(
        variant.fixture.body, database=database / "binding.db", probe=fresh_probe()
    )
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    divergences.extend(binding_persisted_divergences(database / "binding.db", variant.fixture.body))
    return divergences


def _replay_effect_ownership(variant, database: Path) -> list[str]:
    db = database / "eo.db"
    trace = execute_ownership_fixture(variant.fixture.body, database=db, probe=fresh_probe())
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    divergences.extend(
        ownership_persisted_divergences(
            db,
            variant.fixture.body,
            trace.receipts[0].code,
            expected_root_state=(
                trace.final_state if trace.receipts[0].code == "APPLIED" else None
            ),
        )
    )
    return divergences


def _replay_transaction(variant, database: Path) -> list[str]:
    db = database / "tx.db"
    trace = execute_transaction_fixture(variant.fixture.body, database=db, probe=fresh_probe())
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    divergences.extend(transaction_persisted_divergences(db, variant.fixture.body))
    return divergences


def _replay_card(variant, database: Path) -> list[str]:
    db = database / "card.db"
    trace = execute_card_fixture(variant.fixture.body, database=db, probe=fresh_probe())
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    expected_projection = variant.fixture.body["operation_sequence"][0][
        "input"
    ]["authoritative_facts"]["server_projection"]
    if trace.metrics.get("latest_projection") != expected_projection:
        divergences.append("DECISION_STALE did not return the latest projection")
    divergences.extend(card_persisted_divergences(db, variant.fixture.body))
    return divergences


def _replay_dock(variant, database: Path) -> list[str]:
    db = database / "dock.db"
    trace = execute_dock_fixture(variant.fixture.body, database=db, probe=fresh_probe())
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    divergences.extend(dock_persisted_divergences(db, trace))
    return divergences


def _replay_batch(variant, database: Path) -> list[str]:
    trace = execute_batch_fixture(
        variant.fixture.body, database=database / "batch.db", probe=fresh_probe()
    )
    return list(compare(trace, variant.oracle.body).mismatches)


def _replay_notify(variant, database: Path) -> list[str]:
    trace = execute_notify_fixture(
        variant.fixture.body, database=database / "notify.db", probe=fresh_probe()
    )
    return list(compare(trace, variant.oracle.body).mismatches)


def _replay_approval(variant, database: Path) -> list[str]:
    db = database / "app.db"
    trace = execute_approval_fixture(variant.fixture.body, database=db, probe=fresh_probe())
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    receipt_codes = [r.code for r in trace.receipts]
    divergences.extend(
        approval_persisted_divergences(db, variant.fixture.body, receipt_codes)
    )
    return divergences


def _replay_state_machine_operation(variant, database: Path) -> list[str]:
    db = database / "op.db"
    trace = execute_state_machine_fixture(variant.fixture.body, database=db, probe=fresh_probe())
    divergences = list(compare(trace, variant.oracle.body).mismatches)
    divergences.extend(operation_persisted_divergences(db, variant.fixture.body))
    return divergences


def _replay_registry_transition(variant, database: Path) -> list[str]:
    """The registry-driven transition vectors (test_state_machine.py)."""
    oracle = variant.oracle.body
    outcome, before, after = run_transition_fixture(
        variant.fixture.body, database=database / "registry.db"
    )
    problems: list[str] = []

    expected_receipt = oracle["expected_receipts"][0]
    if outcome.receipt_code != expected_receipt["code"]:
        problems.append(f"receipt {outcome.receipt_code} != {expected_receipt['code']}")
    if outcome.receipt_schema != expected_receipt["schema_version"]:
        problems.append(
            f"schema {outcome.receipt_schema} != {expected_receipt['schema_version']}"
        )
    expected_states = oracle["expected_state_trace"]
    if [outcome.from_state, outcome.to_state] != expected_states:
        problems.append(
            f"states {[outcome.from_state, outcome.to_state]} != {expected_states}"
        )
    if list(outcome.events) != oracle["expected_event_trace"]:
        problems.append(f"events {list(outcome.events)} != {oracle['expected_event_trace']}")
    snapshot = oracle["expected_final_snapshot"]
    if outcome.to_state != snapshot["state"]:
        problems.append(f"final state {outcome.to_state} != {snapshot['state']}")
    if outcome.receipt_code == "APPLIED":
        if outcome.reason_code != snapshot["reason_code"]:
            problems.append(f"reason {outcome.reason_code} != {snapshot['reason_code']}")
        if outcome.reason_owner != snapshot["reason_owner"]:
            problems.append(f"reason owner {outcome.reason_owner} != {snapshot['reason_owner']}")

    allowed = set(oracle["allowed_write_set"])
    if set(outcome.writes) != allowed:
        problems.append(
            f"declared writes {sorted(set(outcome.writes))} != {sorted(allowed)}"
        )
    if not allowed:
        if database_changed(before, after):
            problems.append("refusal wrote to the database")
    else:
        missing = unobserved_members(outcome.writes, before, after)
        if missing:
            problems.append(f"declared but not observed: {missing}")
        divergences = persisted_content_divergences(
            database / "registry.db", variant.fixture.body, outcome
        )
        if divergences:
            problems.append("persisted content: " + "; ".join(divergences))

    return problems


def _replay_scenario_transition(variant, database: Path) -> list[str]:
    """The business-command scenario vectors (test_state_machine.py)."""
    oracle = variant.oracle.body
    outcome, before, after = run_scenario_fixture(
        variant.fixture.body, database=database / "scenario.db"
    )
    problems: list[str] = []

    expected_receipt = oracle["expected_receipts"][0]
    if outcome.receipt_code != expected_receipt["code"]:
        problems.append(f"receipt {outcome.receipt_code} != {expected_receipt['code']}")
    if outcome.receipt_schema != expected_receipt["schema_version"]:
        problems.append(
            f"schema {outcome.receipt_schema} != {expected_receipt['schema_version']}"
        )
    expected_states = oracle["expected_state_trace"]
    if [outcome.from_state, outcome.to_state] != expected_states:
        problems.append(
            f"states {[outcome.from_state, outcome.to_state]} != {expected_states}"
        )
    if list(outcome.events) != oracle["expected_event_trace"]:
        problems.append(f"events {list(outcome.events)} != {oracle['expected_event_trace']}")
    snapshot = oracle["expected_final_snapshot"]
    if outcome.to_state != snapshot["state"]:
        problems.append(f"final state {outcome.to_state} != {snapshot['state']}")
    if outcome.receipt_code == "APPLIED" and snapshot["entity_type"] == "feature":
        if outcome.reason_code != snapshot["reason_code"]:
            problems.append(f"reason {outcome.reason_code} != {snapshot['reason_code']}")
        if outcome.reason_owner != snapshot["reason_owner"]:
            problems.append(f"reason owner {outcome.reason_owner} != {snapshot['reason_owner']}")

    allowed = set(oracle["allowed_write_set"])
    if not allowed:
        if database_changed(before, after):
            problems.append("refusal wrote to the database")
    else:
        core = {
            "aggregate", "recovery_case", "external_effect", "business_event",
            "transition_receipt", "recovery_transition_receipt",
            "external_effect_transition_receipt", "audit",
        }
        business_core = allowed & core
        if not business_core <= set(outcome.writes):
            problems.append(
                f"business core {sorted(business_core)} not all in declared "
                f"writes {sorted(set(outcome.writes))}"
            )
        undeclared = unobserved_members(outcome.writes, before, after)
        if undeclared:
            problems.append(f"declared but not observed: {undeclared}")

    return problems


def _replay_evidence_binding(variant, database: Path) -> list[str]:
    """Evidence-binding validation vectors (test_evidence_binding.py)."""
    db = database / "eb.db"
    outcome, before, after = run_transition_fixture(variant.fixture.body, database=db)
    oracle = variant.oracle.body

    trace = _build_trace(outcome, before, after, variant.fixture.body, db)

    divergences = list(compare(trace, oracle).mismatches)
    allowed = set(oracle["allowed_write_set"])
    if not allowed:
        if database_changed(before, after):
            divergences.append("refusal wrote to the database")
    else:
        core = {
            "aggregate", "recovery_case", "external_effect", "business_event",
            "transition_receipt", "recovery_transition_receipt",
            "external_effect_transition_receipt", "audit",
        }
        business_core = allowed & core
        if not business_core <= set(outcome.writes):
            divergences.append(
                f"business core {sorted(business_core)} not all in declared "
                f"writes {sorted(set(outcome.writes))}"
            )
        undeclared = unobserved_members(outcome.writes, before, after)
        if undeclared:
            divergences.append(f"declared but not observed: {undeclared}")

    return divergences


#: test_id -> replay function. The grouping mirrors the owning test file.
DISPATCH: dict[str, Callable[[Any, Path], list[str]]] = {
    "DAL-T-CONFIG-ISOLATION-001": _replay_config_isolation,
    "DAL-T-INJECTION-001": _replay_injection,
    "DAL-T-DB-CONTRACT-001": _replay_db_contract,
    "DAL-T-STATEHASH-001": _replay_binding,
    "DAL-T-ARTIFACTHASH-001": _replay_binding,
    "DAL-T-EFFECT-OWNERSHIP-001": _replay_effect_ownership,
    "DAL-T-TX-001": _replay_transaction,
    "DAL-T-CARD-001": _replay_card,
    "DAL-T-DOCK-001": _replay_dock,
    "DAL-T-BATCH-001": _replay_batch,
    "DAL-T-NOTIFY-001": _replay_notify,
    "DAL-T-APP-001": _replay_approval,
    "DAL-T-APP-EXP-001": _replay_approval,
    "DAL-T-REC-001": _replay_state_machine_operation,
    "DAL-T-RESTART-001": _replay_state_machine_operation,
    "DAL-T-CMD-IDEMPOTENCY-001": _replay_state_machine_operation,
    "DAL-T-EVENT-ORDER-001": _replay_state_machine_operation,
    "DAL-T-SM-001": _replay_registry_transition,
    "DAL-T-RECOVERY-001": _replay_registry_transition,
    "DAL-T-EXTERNAL-EFFECT-001": _replay_registry_transition,
    "DAL-T-EVIDENCE-BINDING-001": _replay_evidence_binding,
}

#: test_ids whose scenario variants (no `transition_command`) use the scenario
#: path rather than the registry path.
SCENARIO_IDS: set[str] = {"DAL-T-SM-001", "DAL-T-RECOVERY-001"}


def _replay_for(test_id: str, variant, db_dir: Path) -> list[str]:
    """Choose the registry/scenario split for the transition-driven ids."""
    if test_id in SCENARIO_IDS and variant.fixture.body.get("transition_command") is None:
        return _replay_scenario_transition(variant, db_dir)
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
        REPO_ROOT / "scripts" / "verify_all_dal_receipts.py",
        REPO_ROOT / "scripts" / "generate_dal_g1_receipts.py",
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
        "runner": "scripts/generate_dal_g1_receipts.py",
    }


def _build_artifacts(
    variant,
    implementation_sha: str,
    implementation_paths: list[str],
    recorded_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (result, receipt) dicts for one passing variant."""
    command = [
        "uv", "run", "python", "scripts/generate_dal_g1_receipts.py",
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

def _g1_variants(frozen: FrozenContracts, filter_test_id: str | None,
                 filter_variant_id: str | None) -> list[Any]:
    eligible = [
        variant
        for variant in frozen.all_variants()
        if variant.run_gate == "G1"
        and G1_OWNER_TASKS.intersection(variant.owner_tasks)
    ]
    expected_dispatch = {variant.test_id for variant in eligible}
    if set(DISPATCH) != expected_dispatch:
        missing = sorted(expected_dispatch - set(DISPATCH))
        extra = sorted(set(DISPATCH) - expected_dispatch)
        raise ValueError(
            f"G1 replay dispatch is not closed: missing={missing}, extra={extra}"
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

    if args.run_gate not in (None, "G1"):
        print(f"ERROR: only --run-gate G1 is supported: {args.run_gate!r}", file=sys.stderr)
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
    variants = _g1_variants(frozen, args.test_id, args.variant_id)
    if not variants:
        print("no G1 variants to generate", file=sys.stderr)
        return 2

    if not args.dry_run:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    artifacts: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    with tempfile.TemporaryDirectory(prefix="dal-g1-") as tmp:
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
