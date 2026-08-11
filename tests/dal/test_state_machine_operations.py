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

The G1 scenario sets are asserted as *closed enumerations* (B3's remediation):
a new scenario variant that nobody consumes must fail loudly instead of being
skipped. The G2 variants of these families — `worker_*` and `mac_*` — belong to
the Wave 2 Home Mac Worker slice and are asserted to exist at G2, not G1, so
this slice cannot silently claim them.
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

#: G2 variants that exist in the frozen contracts but belong to the Wave 2 Home
#: Mac Worker slice (DAL-016/017), outside this authorisation.
G2_VARIANTS: dict[str, set[str]] = {
    REC_TEST_ID: {"worker_disconnect", "worker_kill"},
    RESTART_TEST_ID: {"mac_retry_limit", "mac_review_limit"},
}

ALL_TEST_IDS = [REC_TEST_ID, RESTART_TEST_ID, CMD_IDEMPOTENCY_TEST_ID, EVENT_ORDER_TEST_ID]


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


def test_wave2_g2_variants_are_not_claimed(contracts: FrozenContracts) -> None:
    """The `worker_*`/`mac_*` variants stay G2, owned by the Home Mac Worker.

    This slice is authorised for the synthetic DAL-007-013 work only. If a
    worker variant drifted to G1, the closed-enumeration test above would still
    pass while this slice silently claimed it — so the G2 membership is pinned
    here as a separate assertion.
    """
    for test_id, expected_g2 in G2_VARIANTS.items():
        variants = contracts.variants(test_id)
        by_id = {v.variant_id: v for v in variants}
        assert set(by_id) >= expected_g2, (
            f"{test_id} lost a frozen variant: {sorted(expected_g2 - set(by_id))}"
        )
        for variant_id in expected_g2:
            assert by_id[variant_id].run_gate == "G2", (
                f"{test_id}/{variant_id} drifted out of G2"
            )


@pytest.mark.parametrize("test_id", ALL_TEST_IDS)
def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every G1 scenario variant and report all divergences at once.

    Parametrising pytest cases would print hundreds of divergence lines on a
    regression; the loop collects them so a failure reports *which* variants
    diverged and how, in one place.
    """
    variants = [
        v for v in contracts.variants(test_id) if v.run_gate == "G1"
    ]
    assert variants, f"no G1 variants for {test_id}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"op-{test_id}-{index}.db"
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
        f"{len(failures)} of {len(variants)} {test_id} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
