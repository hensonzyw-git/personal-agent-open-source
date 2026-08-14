"""DAL-011: hash-bound state and artifact binding validation.

`DAL-T-STATEHASH-001` proves `state_sha256` is recomputed server-side (§3.2.1):
a decision whose submitted state binding diverges from the protected digest is
`DECISION_STALE` with zero writes. Its seven G1 variants each change one
dimension of the binding — a field, the aggregate version, member order, an
array member, a null where a value was frozen, or an injected `binding_sha256`
field.

`DAL-T-ARTIFACTHASH-001` proves the same for `artifact_sha256` (§3.2): a
divergent artifact binding is `APPROVAL_INVALID`. Its five variants change the
body hash, the canonicalizer field, a metadata value's type, a metadata value,
or inject a forged `binding_sha256` field.

Each variant is hash-bound to its frozen fixture and oracle, replayed against
the real validators through ``binding_executor``, and judged by
``oracle_comparator.compare`` plus an independent zero-write persisted check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.binding_executor import (
    execute_binding_fixture,
    binding_persisted_divergences,
)


STATEHASH_TEST_ID = "DAL-T-STATEHASH-001"
ARTIFACTHASH_TEST_ID = "DAL-T-ARTIFACTHASH-001"

G1_SCENARIOS: dict[str, set[str]] = {
    STATEHASH_TEST_ID: {
        "effect_inventory_membership",
        "effect_inventory_order",
        "field",
        "forged_digest",
        "null",
        "order",
        "version",
    },
    ARTIFACTHASH_TEST_ID: {
        "body",
        "canonicalizer",
        "field_boundary",
        "forged_digest",
        "metadata",
    },
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


@pytest.mark.parametrize("test_id", [STATEHASH_TEST_ID, ARTIFACTHASH_TEST_ID])
def test_g1_scenario_set_is_closed(
    contracts: FrozenContracts, test_id: str
) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(test_id) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS[test_id], (
        f"{test_id} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS[test_id])}"
    )


@pytest.mark.parametrize("test_id", [STATEHASH_TEST_ID, ARTIFACTHASH_TEST_ID])
def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [v for v in contracts.variants(test_id) if v.run_gate == "G1"]
    assert variants, f"no G1 variants for {test_id}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"binding-{test_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_binding_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        oracle = variant.oracle.body
        result = compare(trace, oracle)
        divergences = list(result.mismatches)
        divergences.extend(
            binding_persisted_divergences(database, variant.fixture.body)
        )
        if divergences:
            failures.append(
                f"{variant.variant_id}: " + "; ".join(divergences)
            )

    assert not failures, (
        f"{len(failures)} of {len(variants)} {test_id} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
