"""DAL-010: effect ownership — the four G1 variants.

`DAL-T-EFFECT-OWNERSHIP-001` proves who may act on an external effect, and
what happens when the owning root aggregate closes it:

- ``feature_direct_completed_command_removed`` — a removed command on the
  effect → ILLEGAL_TRANSITION, zero-write.
- ``feature_direct_reconciliation_owner_denied`` — the effect's owner is a
  feature but the command names a recovery_case owner → POLICY_DENIED, zero-write.
- ``feature_reconciliation_root_closes_effect`` — a human resumes the feature
  from reconciliation and the companion closes the effect → APPLIED.
- ``recovery_owner_root_closes_effect`` — the recovery case records execution
  and the companion closes the effect → APPLIED.

Each variant is hash-bound to its frozen fixture and oracle, replayed against
the real engine through ``effect_ownership_executor``, and judged by
``oracle_comparator.compare`` on every dimension the oracle freezes. The
persisted-content check independently confirms the root and effect rows are
unchanged after a denial, and moved correctly after an apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.effect_ownership_executor import (
    execute_ownership_fixture,
    ownership_persisted_divergences,
)


EO_TEST_ID = "DAL-T-EFFECT-OWNERSHIP-001"

G1_SCENARIOS: set[str] = {
    "feature_direct_completed_command_removed",
    "feature_direct_reconciliation_owner_denied",
    "feature_reconciliation_root_closes_effect",
    "recovery_owner_root_closes_effect",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_set_is_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(EO_TEST_ID) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS, (
        f"{EO_TEST_ID} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS)}"
    )


def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [v for v in contracts.variants(EO_TEST_ID) if v.run_gate == "G1"]
    assert variants, f"no G1 variants for {EO_TEST_ID}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"eo-{variant.variant_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_ownership_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        oracle = variant.oracle.body
        result = compare(trace, oracle)
        divergences = list(result.mismatches)
        divergences.extend(
            ownership_persisted_divergences(
                database, variant.fixture.body, trace.receipts[0].code,
                expected_root_state=trace.final_state if trace.receipts[0].code == "APPLIED" else None,
            )
        )
        if divergences:
            failures.append(
                f"{variant.variant_id}: " + "; ".join(divergences)
            )

    assert not failures, (
        f"{len(failures)} of {len(variants)} {EO_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
