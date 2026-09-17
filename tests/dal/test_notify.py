"""DAL-013: notification delivery — `DAL-T-NOTIFY-001`.

`DAL-T-NOTIFY-001` proves the §3.7 delivery state machine:

- ``ack_loss`` — first attempt loses its ack, the re-read finds it → delivered.
- ``concurrent_claim`` — one claim wins, a conflicting claim is a no-op → delivered.
- ``restart`` — an already-started attempt is idempotent, then the ack → delivered.
- ``permanent_failure`` — five permanent failures → dead_letter.

Each variant is hash-bound to its frozen fixture and oracle, replayed against
the real `deliver_notification`, and judged by `oracle_comparator.compare`
plus the database-derived write set and event trace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.notify_executor import execute_notify_fixture


NOTIFY_TEST_ID = "DAL-T-NOTIFY-001"

G1_SCENARIOS: set[str] = {
    "ack_loss",
    "concurrent_claim",
    "restart",
    "permanent_failure",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_set_is_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(NOTIFY_TEST_ID) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS, (
        f"{NOTIFY_TEST_ID} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS)}"
    )


def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [
        v for v in contracts.variants(NOTIFY_TEST_ID) if v.run_gate == "G1"
    ]
    assert variants, f"no G1 variants for {NOTIFY_TEST_ID}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"notify-{variant.variant_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_notify_fixture(
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
        f"{len(failures)} of {len(variants)} {NOTIFY_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
