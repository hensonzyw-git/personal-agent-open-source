"""DAL-007: `DAL-T-CONFIG-ISOLATION-001`, replayed from the frozen contracts.

These four adversarial variants are not written from the implementation's own
assumptions. Each is loaded from `docs/dal/manifests/` and bound by its
manifest-declared content hash, executed against the real
`personal_agent_dal.config` loader through the harness, and judged against the
frozen oracle. The four attack shapes:

- `finance_import`: the config resolves to the Finance module namespace;
- `insecure_secret_file`: a secret file with a non-owner-only OS mode;
- `production_credential`: a request naming a production/provider credential;
- `unknown_config`: a request for a name outside the declared allowlist.

Each must be refused with `POLICY_DENIED`, leave the `service_config` entity
`not_loaded`, write nothing, and cross no forbidden external boundary.
"""

from __future__ import annotations

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.operation_executor import execute_fixture
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe


TEST_ID = "DAL-T-CONFIG-ISOLATION-001"
EXPECTED_VARIANTS = {
    "finance_import",
    "insecure_secret_file",
    "production_credential",
    "unknown_config",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_manifest_covers_exactly_the_four_expected_variants(
    contracts: FrozenContracts,
) -> None:
    """The frozen set is exactly the four expected variants: none added, none dropped."""
    variants = contracts.variants(TEST_ID)
    assert {v.variant_id for v in variants} == EXPECTED_VARIANTS
    for variant in variants:
        assert variant.run_gate == "G1"
        assert variant.owner_tasks == ("DAL-007",)


@pytest.mark.parametrize("variant_id", sorted(EXPECTED_VARIANTS))
def test_config_isolation_variant(
    contracts: FrozenContracts, variant_id: str
) -> None:
    """Replay one frozen variant against the real loader and judge by its oracle."""
    variant = next(
        v for v in contracts.variants(TEST_ID) if v.variant_id == variant_id
    )
    probe = fresh_probe()
    trace = execute_fixture(variant.fixture.body, probe=probe)
    result = compare(trace, variant.oracle.body)
    assert result.passed, (
        f"{variant_id} diverged from its frozen oracle:\n"
        + "\n".join(f"  - {m}" for m in result.mismatches)
    )
