"""DAL-013: decision-card invalidation — `DAL-T-CARD-001`.

A device action against a decision card that is no longer current is refused
with `DECISION_STALE` and zero writes (§3.5). The six G1 variants each diverge
on one dimension:

- ``apns_loss`` — the device's card is a projection version behind because an
  APNs update was lost;
- ``expired`` — the decision's expiry has passed;
- ``old_click`` — a stale projection version;
- ``resolved`` — the decision is no longer open;
- ``stale`` — a stale projection version (same shape as ``apns_loss``, a
  distinct idempotency key);
- ``superseded`` — the decision was replaced by a newer one.

Each variant is hash-bound to its frozen fixture and oracle, replayed against
the real `apply_decision_action` through ``card_executor``, and judged by
``oracle_comparator.compare`` plus an independent zero-write persisted check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent_core.timeutil import parse_rfc3339
from personal_agent_dal.machine.card import apply_decision_action
from personal_agent_dal.machine.transition_types import ReceiptCodes, TransitionRefused
from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.card_executor import (
    execute_card_fixture,
    card_persisted_divergences,
)


CARD_TEST_ID = "DAL-T-CARD-001"

G1_SCENARIOS: set[str] = {
    "apns_loss",
    "expired",
    "old_click",
    "resolved",
    "stale",
    "superseded",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_set_is_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(CARD_TEST_ID) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS, (
        f"{CARD_TEST_ID} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS)}"
    )


def test_cross_decision_card_with_same_version_is_refused() -> None:
    card = {
        "decision_id": "decision-a",
        "projection_version": 5,
        "status": "open",
        "expires_at": "2026-08-14T12:05:00Z",
        "superseded_by": None,
    }
    latest = {**card, "decision_id": "decision-b"}
    with pytest.raises(TransitionRefused) as refusal:
        apply_decision_action(
            card=card,
            server_projection=latest,
            server_now=parse_rfc3339("2026-08-14T12:00:00Z"),
        )
    assert refusal.value.code == ReceiptCodes.DECISION_STALE
    assert refusal.value.latest_projection == latest


def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [
        v for v in contracts.variants(CARD_TEST_ID) if v.run_gate == "G1"
    ]
    assert variants, f"no G1 variants for {CARD_TEST_ID}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"card-{variant.variant_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_card_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        oracle = variant.oracle.body
        result = compare(trace, oracle)
        divergences = list(result.mismatches)
        expected_projection = variant.fixture.body["operation_sequence"][0][
            "input"
        ]["authoritative_facts"]["server_projection"]
        if trace.metrics.get("latest_projection") != expected_projection:
            divergences.append("DECISION_STALE did not return the latest projection")
        divergences.extend(
            card_persisted_divergences(database, variant.fixture.body)
        )
        if divergences:
            failures.append(
                f"{variant.variant_id}: " + "; ".join(divergences)
            )

    assert not failures, (
        f"{len(failures)} of {len(variants)} {CARD_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
