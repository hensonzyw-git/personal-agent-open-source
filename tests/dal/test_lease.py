"""DAL-016: the worker-lease boundary — `LEASE-001` G2 (3 variants).

A paused feature resumes only through a lease whose epoch is current and whose
repository base has not drifted. The frozen G2 variants:

- `new_lease_after_drift` — a fresh lease whose base read-back no longer matches
  the approved base blocks the feature with `STATE_DRIFT` (`BLK-DRIFT--paused`);
- `old_worker_result` — a result for a revoked lease is refused `CAPABILITY_STALE`
  with zero writes;
- `pause_expire` — issuing a lease on an expired lease is refused
  `CAPABILITY_STALE` with zero writes.

The first is DB-backed (a `block_feature` transition); the other two are pure
zero-write refusals. All three run through the state-machine executor, which
seeds the paused feature and measures the write set from the database across
the whole sequence.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import lease as lease_policy

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.state_machine_executor import (
    execute_state_machine_fixture,
    operation_persisted_divergences,
)


LEASE_TEST_ID = "DAL-T-LEASE-001"

#: The exact G2 scenario set the family must expose. A frozen variant outside
#: its set is a harness gap, not a skipped test — the enumeration is closed.
G2_SCENARIOS: set[str] = {
    "new_lease_after_drift",
    "old_worker_result",
    "pause_expire",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g2_scenario_sets_are_closed(contracts: FrozenContracts) -> None:
    """Every G2 variant is consumed; nothing frozen is silently skipped."""
    g2 = {v.variant_id for v in contracts.variants(LEASE_TEST_ID) if v.run_gate == "G2"}
    assert g2 == G2_SCENARIOS, (
        f"{LEASE_TEST_ID} G2 scenario set drifted: {sorted(g2 ^ G2_SCENARIOS)}"
    )


def test_lease_variants_match_their_oracles(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    """Replay every lease variant and report all divergences at once."""
    variants = [
        v for v in contracts.variants(LEASE_TEST_ID) if v.run_gate == "G2"
    ]
    assert variants, f"no G2 variants for {LEASE_TEST_ID}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"lease-{index}.db"
        probe = fresh_probe()
        trace = execute_state_machine_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        divergences = list(compare(trace, variant.oracle.body).mismatches)
        divergences.extend(
            operation_persisted_divergences(database, variant.fixture.body)
        )
        if divergences:
            failures.append(f"{variant.variant_id}: " + "; ".join(divergences))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {LEASE_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def _lease_command(contracts: FrozenContracts, variant_id: str) -> dict:
    variant = next(
        v
        for v in contracts.variants(LEASE_TEST_ID)
        if v.run_gate == "G2" and v.variant_id == variant_id
    )
    return deepcopy(variant.fixture.body["operation_sequence"][0])


def test_lease_malformed_inputs_fail_closed(contracts: FrozenContracts) -> None:
    """Malformed lease shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _lease_command(contracts, "pause_expire")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _lease_command(contracts, "pause_expire")
    command["input"]["authoritative_facts"]["lease_status"] = "active"
    cases.append(("unknown lease status", command))

    command = _lease_command(contracts, "new_lease_after_drift")
    command["input"]["authoritative_facts"].pop("observed_base_sha")
    cases.append(("requested lease missing observed base", command))

    command = _lease_command(contracts, "pause_expire")
    command["input"]["authoritative_facts"].pop("current_epoch")
    cases.append(("dead lease missing current epoch", command))

    command = _lease_command(contracts, "pause_expire")
    command["input"]["target"]["state"] = "coding"
    cases.append(("non-paused target state", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            lease_policy.evaluate_lease(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label
