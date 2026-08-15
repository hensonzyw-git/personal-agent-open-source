"""DAL-011: approval service — APP-001 + APP-EXP-001 G1 variants.

`DAL-T-APP-001` proves the consume-once approval contract:
- ``concurrent_consume``: two operations race; first APPLIED, second APPROVAL_INVALID.
- ``double_tap``: approval already consumed → APPROVAL_INVALID.
- ``revoke_race``: approval revoked → APPROVAL_INVALID.
- ``stale_decision``: submitted version != current → DECISION_STALE.

`DAL-T-APP-EXP-001` proves the expiry contract:
- ``approval_expired``: server_now past approval expiry → APPROVAL_INVALID.
- ``decision_expired``: server_now past decision expiry → DECISION_STALE.

Each variant is hash-bound to its frozen fixture and oracle, replayed against
the real engine through ``approval_executor``, and judged by
``oracle_comparator.compare`` + persisted-content check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.approval_executor import (
    execute_approval_fixture,
    approval_persisted_divergences,
)


APP_TEST_ID = "DAL-T-APP-001"
APP_EXP_TEST_ID = "DAL-T-APP-EXP-001"

G1_SCENARIOS: dict[str, set[str]] = {
    APP_TEST_ID: {
        "concurrent_consume",
        "double_tap",
        "revoke_race",
        "stale_decision",
    },
    APP_EXP_TEST_ID: {
        "approval_expired",
        "decision_expired",
    },
}

#: Variants that are known-blocked. The set is closed against the manifest, so
#: any variant listed here still has to exist in the frozen contracts — drift
#: surfaces in the closed-set test, not by silently skipping. The only blocked
#: variant (``concurrent_consume``) was unblocked by the approval token gate
#: (engine runs the approval CAS before the aggregate version CAS); the reason
#: is recorded in the DAL-011 evidence doc.
KNOWN_BLOCKED: dict[str, dict[str, str]] = {
    APP_TEST_ID: {},
    APP_EXP_TEST_ID: {},
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


@pytest.mark.parametrize("test_id", [APP_TEST_ID, APP_EXP_TEST_ID])
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


@pytest.mark.parametrize("test_id", [APP_TEST_ID, APP_EXP_TEST_ID])
def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [v for v in contracts.variants(test_id) if v.run_gate == "G1"]
    assert variants, f"no G1 variants for {test_id}"

    failures: list[str] = []
    blocked: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"app-{test_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_approval_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        oracle = variant.oracle.body
        result = compare(trace, oracle)
        divergences = list(result.mismatches)
        receipt_codes = [r.code for r in trace.receipts]
        divergences.extend(
            approval_persisted_divergences(database, variant.fixture.body, receipt_codes)
        )
        if divergences:
            if variant.variant_id in KNOWN_BLOCKED.get(test_id, {}):
                blocked.append(
                    f"{variant.variant_id} ({KNOWN_BLOCKED[test_id][variant.variant_id]}): "
                    + "; ".join(divergences)
                )
            else:
                failures.append(
                    f"{variant.variant_id}: " + "; ".join(divergences)
                )

    assert not failures, (
        f"{len(failures)} of {len(variants)} {test_id} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
    if blocked:
        # Blocked variants fail by definition; surface them as a skip so the
        # suite reports them loudly rather than letting them look like passes.
        pytest.skip(
            f"{len(blocked)} known-blocked {test_id} variant(s):\n"
            + "\n".join(f"  - {line}" for line in blocked)
        )
