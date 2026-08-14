"""DAL-013: notification batch — `DAL-T-BATCH-001`.

`DAL-T-BATCH-001` proves the §3.5.2 fixed-window batch semantics:

- ``continuous`` — two normal decisions, window deadline passed → flush.
- ``service_restart`` — a persisted window whose deadline has passed → flush.
- ``fifth_item`` — five normal decisions reach `maximum_items` → early flush.
- ``high_risk_interrupt`` — an `immediate` decision closes and flushes the batch.
- ``all_invalid`` — every member expired/resolved → `NOOP`, `cancelled`, zero writes.

Each variant is hash-bound to its frozen fixture and oracle, replayed against
the real `apply_notification_batch`, and judged by `oracle_comparator.compare`
plus the database-derived write set and event trace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.batch_executor import execute_batch_fixture


BATCH_TEST_ID = "DAL-T-BATCH-001"

G1_SCENARIOS: set[str] = {
    "all_invalid",
    "continuous",
    "service_restart",
    "fifth_item",
    "high_risk_interrupt",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_set_is_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(BATCH_TEST_ID) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS, (
        f"{BATCH_TEST_ID} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS)}"
    )


def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [
        v for v in contracts.variants(BATCH_TEST_ID) if v.run_gate == "G1"
    ]
    assert variants, f"no G1 variants for {BATCH_TEST_ID}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"batch-{variant.variant_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_batch_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        oracle = variant.oracle.body
        result = compare(trace, oracle)
        divergences = list(result.mismatches)
        if divergences:
            failures.append(
                f"{variant.variant_id}: " + "; ".join(divergences)
            )

    assert not failures, (
        f"{len(failures)} of {len(variants)} {BATCH_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
