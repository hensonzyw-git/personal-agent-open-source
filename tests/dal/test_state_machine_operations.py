"""DAL-009: the four operation-driven scenario families, replayed against the
real engine and judged by their frozen oracles.

`test_state_machine.py` replays the registry-driven variants and the
single-transition scenarios. The four families here cannot be consumed by that
harness because they are *operations*, not single transitions:

- `DAL-T-REC-001` drives the external-effect lifecycle across four recording
  steps (or refuses cancellation of an unknown effect);
- `DAL-T-RESTART-001` evaluates persisted loop counters after a restart and
  blocks the feature when a limit is hit;
- `DAL-T-CMD-IDEMPOTENCY-001` replays the same command (returning the original
  receipt) or refuses a reused key with different content;
- `DAL-T-EVENT-ORDER-001` refuses an event with a stale expected version.

Each variant is hash-bound to its frozen fixture and oracle by
`FrozenContracts`, executed against a real SQLite database through
`state_machine_executor`, and judged by `oracle_comparator.compare` on every
dimension the oracle freezes: state trace, receipts (with duplicate flags and
unique receipt ids), write set, event and external-effect traces, final
snapshot, scenario assertions and forbidden side effects.

Both gates are asserted as *closed enumerations* (B3's remediation): a new
scenario variant that nobody consumes must fail loudly instead of being
skipped. The G1 scenario sets belong to the DAL-007-013 slice; the G2 scenario
sets — REC-001 `worker_*` and RESTART-001 `mac_*` — are owned by the DAL-016
Home Mac Worker slice and replayed here against the same engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.state_machine_executor import (
    execute_state_machine_fixture,
    operation_persisted_divergences,
)


REC_TEST_ID = "DAL-T-REC-001"
RESTART_TEST_ID = "DAL-T-RESTART-001"
CMD_IDEMPOTENCY_TEST_ID = "DAL-T-CMD-IDEMPOTENCY-001"
EVENT_ORDER_TEST_ID = "DAL-T-EVENT-ORDER-001"

#: The exact G1 scenario sets each family must expose at this gate. B3 made the
#: enumeration closed: a frozen variant outside this set is a harness gap, not
#: a skipped test.
G1_SCENARIOS: dict[str, set[str]] = {
    REC_TEST_ID: {
        "synthetic_ack_loss",
        "synthetic_disconnect",
        "synthetic_kill",
        "unknown_merge_cancel",
        "unknown_deploy_cancel",
    },
    RESTART_TEST_ID: {"service_retry_limit", "service_review_limit"},
    CMD_IDEMPOTENCY_TEST_ID: {"idempotency_conflict", "idempotent_replay"},
    EVENT_ORDER_TEST_ID: {"out_of_order_event"},
}

#: The exact G2 scenario sets owned by the DAL-016 Home Mac Worker slice.
G2_SCENARIOS: dict[str, set[str]] = {
    REC_TEST_ID: {"worker_disconnect", "worker_kill"},
    RESTART_TEST_ID: {"mac_retry_limit", "mac_review_limit"},
}

ALL_TEST_IDS = [REC_TEST_ID, RESTART_TEST_ID, CMD_IDEMPOTENCY_TEST_ID, EVENT_ORDER_TEST_ID]
G2_TEST_IDS = [REC_TEST_ID, RESTART_TEST_ID]


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_sets_are_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped.

    A scenario the harness does not resolve would otherwise sit in the catalog
    forever, green because no test touches it. The enumeration is exact in both
    directions: an extra G1 variant fails, and a dropped one fails.
    """
    for test_id in ALL_TEST_IDS:
        g1 = {
            v.variant_id for v in contracts.variants(test_id) if v.run_gate == "G1"
        }
        assert g1 == G1_SCENARIOS[test_id], (
            f"{test_id} G1 scenario set drifted: "
            f"{sorted(g1 ^ G1_SCENARIOS[test_id])}"
        )


def test_g2_scenario_sets_are_closed(contracts: FrozenContracts) -> None:
    """Every G2 variant is consumed; nothing frozen is silently skipped.

    The `worker_*`/`mac_*` variants are owned by the DAL-016 Home Mac Worker
    slice and must be replayed here, not merely asserted to exist at G2. The
    enumeration is exact in both directions.
    """
    for test_id in G2_TEST_IDS:
        g2 = {
            v.variant_id for v in contracts.variants(test_id) if v.run_gate == "G2"
        }
        assert g2 == G2_SCENARIOS[test_id], (
            f"{test_id} G2 scenario set drifted: "
            f"{sorted(g2 ^ G2_SCENARIOS[test_id])}"
        )


def _replay(
    contracts: FrozenContracts,
    test_id: str,
    run_gate: str,
    tmp_path: Path,
) -> None:
    """Replay every variant of one gate and report all divergences at once."""
    variants = [
        v for v in contracts.variants(test_id) if v.run_gate == run_gate
    ]
    assert variants, f"no {run_gate} variants for {test_id}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"op-{test_id}-{run_gate}-{index}.db"
        probe = fresh_probe()
        trace = execute_state_machine_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        result = compare(trace, variant.oracle.body)
        divergences = list(result.mismatches)
        # The comparator judges the trace; the database itself is judged
        # separately, so a transition whose receipt misreports its own spec,
        # states or version is caught even when the trace recorded it
        # faithfully (the arrangement/judgement separation of §5.1 applied to
        # the persisted content, not only the write classes).
        divergences.extend(
            operation_persisted_divergences(database, variant.fixture.body)
        )
        if divergences:
            failures.append(
                f"{variant.variant_id}: " + "; ".join(divergences)
            )

    assert not failures, (
        f"{len(failures)} of {len(variants)} {test_id} {run_gate} scenarios "
        f"diverged:\n" + "\n".join(f"  - {line}" for line in failures)
    )


@pytest.mark.parametrize("test_id", ALL_TEST_IDS)
def test_every_g1_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every G1 scenario variant against its oracle."""
    _replay(contracts, test_id, "G1", tmp_path)


@pytest.mark.parametrize("test_id", G2_TEST_IDS)
def test_every_g2_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every G2 scenario variant against its oracle."""
    _replay(contracts, test_id, "G2", tmp_path)
