"""DAL-010: transaction atomicity — the four failure-injection variants.

`DAL-T-TX-001` proves the transaction boundary is real. An operation writes
five members (aggregate, business_event, transition_receipt, audit,
notification_outbox) in one serializable unit; one member fails. The oracle
for every variant is zero-write, ``planning -> planning``, no receipt — the
partial writes before the failure are not durable.

Each variant is hash-bound to its frozen fixture and oracle by
`FrozenContracts`, executed against a real SQLite database through
`transaction_executor`, and judged by `oracle_comparator.compare` on every
dimension the oracle freezes. The persisted-content check
(`transaction_persisted_divergences`) is the independent second opinion: it
confirms no row survived in any member table and the feature is unchanged,
so a transaction that rolled back the data rows but left a receipt or bumped
the version is caught even if the write-set labels missed it.

The G1 scenario set is asserted as a closed enumeration, matching the pattern
of the DAL-009 operation tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.transaction_executor import (
    execute_transaction_fixture,
    transaction_persisted_divergences,
)


TX_TEST_ID = "DAL-T-TX-001"

#: The exact G1 scenario set. B3's closure rule: a frozen variant outside
#: this set is a harness gap, not a skipped test.
G1_SCENARIOS: set[str] = {
    "audit_fail",
    "event_fail",
    "outbox_fail",
    "receipt_fail",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_set_is_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(TX_TEST_ID) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS, (
        f"{TX_TEST_ID} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS)}"
    )


def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    """Replay every G1 variant and report all divergences at once.

    Each variant injects a failure at one transaction member. The oracle
    expects atomic rollback: zero-write, ``planning -> planning``, no receipt.
    The comparator judges the trace; the persisted-content check judges the
    database itself — so a transaction that silently kept a partial write is
    caught by both.
    """
    variants = [v for v in contracts.variants(TX_TEST_ID) if v.run_gate == "G1"]
    assert variants, f"no G1 variants for {TX_TEST_ID}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"tx-{variant.variant_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_transaction_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        result = compare(trace, variant.oracle.body)
        divergences = list(result.mismatches)
        divergences.extend(
            transaction_persisted_divergences(database, variant.fixture.body)
        )
        if divergences:
            failures.append(
                f"{variant.variant_id}: " + "; ".join(divergences)
            )

    assert not failures, (
        f"{len(failures)} of {len(variants)} {TX_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
