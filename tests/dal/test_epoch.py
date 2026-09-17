"""DAL-016: the epoch-bound result boundary — `EPOCH-001` G2 (6 variants).

A worker result is accepted only when the epochs it was issued under are still
current. The frozen G2 variants each leave at least one binding epoch behind the
current one and are refused with zero writes:

- `all_old`, `lease_capability`, `old_capability`, `old_lease` — a `coding`
  feature whose capability or lease epoch is stale → `CAPABILITY_STALE`;
- `capability_approval`, `old_approval` — an `approved` feature whose approval
  epoch is stale → `APPROVAL_INVALID`.

Pure: executed against `accept_epoch_bound_result` under `guard_pure_policy`,
replayed against the real policy and judged by `oracle_comparator.compare`.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import epoch as epoch_policy

from tests.dal.contract_loader import FrozenContracts
from tests.dal.epoch_executor import execute_epoch_fixture
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe


EPOCH_TEST_ID = "DAL-T-EPOCH-001"

#: The exact G2 scenario set the family must expose. A frozen variant outside
#: its set is a harness gap, not a skipped test — the enumeration is closed.
G2_SCENARIOS: set[str] = {
    "all_old",
    "capability_approval",
    "lease_capability",
    "old_approval",
    "old_capability",
    "old_lease",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g2_scenario_sets_are_closed(contracts: FrozenContracts) -> None:
    """Every G2 variant is consumed; nothing frozen is silently skipped."""
    g2 = {v.variant_id for v in contracts.variants(EPOCH_TEST_ID) if v.run_gate == "G2"}
    assert g2 == G2_SCENARIOS, (
        f"{EPOCH_TEST_ID} G2 scenario set drifted: {sorted(g2 ^ G2_SCENARIOS)}"
    )


def test_pure_epoch_variants_match_their_oracles(
    contracts: FrozenContracts,
) -> None:
    """Replay every pure epoch refusal and report all divergences at once."""
    variants = [
        v for v in contracts.variants(EPOCH_TEST_ID) if v.run_gate == "G2"
    ]
    assert variants, f"no G2 variants for {EPOCH_TEST_ID}"

    failures: list[str] = []
    for variant in variants:
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
        if divergences:
            failures.append(f"{variant.variant_id}: " + "; ".join(divergences))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {EPOCH_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def _epoch_command(contracts: FrozenContracts) -> dict:
    variant = next(
        v
        for v in contracts.variants(EPOCH_TEST_ID)
        if v.run_gate == "G2" and v.variant_id == "old_lease"
    )
    return deepcopy(variant.fixture.body["operation_sequence"][0])


def test_epoch_malformed_inputs_fail_closed(contracts: FrozenContracts) -> None:
    """Malformed epoch shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _epoch_command(contracts)
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _epoch_command(contracts)
    command["input"]["authoritative_facts"]["current_epochs"] = {
        "approval_epoch": 9,
        "capability_epoch": 7,
    }
    cases.append(("missing lease epoch", command))

    command = _epoch_command(contracts)
    command["input"]["authoritative_facts"]["submitted_epochs"]["lease_epoch"] = -1
    cases.append(("negative submitted epoch", command))

    command = _epoch_command(contracts)
    command["input"]["target"]["state"] = "intake"
    cases.append(("non-epoch-bound target state", command))

    command = _epoch_command(contracts)
    command["input"]["action_sequence"][0].pop("result_sha256")
    cases.append(("missing result_sha256", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            epoch_policy.accept_epoch_bound_result(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_pure_policy_dependency_surfaces_are_closed() -> None:
    """The pure epoch policy cannot acquire an unguarded I/O dependency."""
    expected_imports = {
        "__future__",
        "dataclasses",
        "typing",
        "personal_agent_dal.errors",
        "personal_agent_dal.receipt",
    }
    tree = ast.parse(Path(epoch_policy.__file__).read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == expected_imports
