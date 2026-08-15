"""DAL-012: injection policy — `DAL-T-INJECTION-001`.

`DAL-T-INJECTION-001/api_intake` proves the intake injection boundary: tainted
content requesting capabilities beyond the current set (production_credentials
+ unrestricted_repository_write over read_workspace) is refused `POLICY_DENIED`
with zero writes and the feature left in `intake`.

The G1 gate owns exactly one variant — `api_intake`. The `diff`/`issue`/
`readme`/`test_failure` (G2) and `provider_output` (G4) carriers are later-wave
work and are deliberately excluded here.

Each G1 variant is hash-bound to its frozen fixture and oracle, replayed against
the real `evaluate_untrusted_content`, and judged by `oracle_comparator.compare`.
"""

from __future__ import annotations

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.injection_executor import execute_injection_fixture


INJECTION_TEST_ID = "DAL-T-INJECTION-001"

G1_SCENARIOS: set[str] = {
    "api_intake",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_set_is_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(INJECTION_TEST_ID) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS, (
        f"{INJECTION_TEST_ID} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS)}"
    )


def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts,
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [
        v for v in contracts.variants(INJECTION_TEST_ID) if v.run_gate == "G1"
    ]
    assert variants, f"no G1 variants for {INJECTION_TEST_ID}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        trace = execute_injection_fixture(variant.fixture.body, probe=probe)
        oracle = variant.oracle.body
        result = compare(trace, oracle)
        divergences = list(result.mismatches)
        if divergences:
            failures.append(f"{variant.variant_id}: " + "; ".join(divergences))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {INJECTION_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
