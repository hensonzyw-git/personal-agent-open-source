"""DAL-013: Decision Dock projection — `DAL-T-DOCK-001`.

`DAL-T-DOCK-001` proves the §3.5.1 rank pipeline. Each of the five G1 variants
exercises one pipeline dimension and freezes its exact projection:

- ``mixed_rank`` — different first-match ranks sort highest-first.
- ``tie`` — same rank breaks by `decision_id`.
- ``dependency`` — a dependent whose dependency is still open is filtered.
- ``same_root`` — the rank-front wins within a root, the rest are evicted.
- ``bulk_high_risk`` — high-risk decisions project normally (no refusal),
  truncated by `maximum_items`.

Each variant is hash-bound to its frozen fixture and oracle, replayed against
the real `apply_decision_dock`, and judged by `oracle_comparator.compare`
(which asserts the frozen `ordered_decision_ids`/`ranks`/`evictions` scenario
metrics) plus a persisted-projection re-read.
"""

from __future__ import annotations

from pathlib import Path
from copy import deepcopy

import pytest
from sqlalchemy import text

from personal_agent_core.timeutil import parse_rfc3339
from personal_agent_dal.machine.dock import apply_decision_dock, project_decision_dock
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.dock_executor import (
    _seed,
    _snapshot,
    execute_dock_fixture,
    dock_persisted_divergences,
)


DOCK_TEST_ID = "DAL-T-DOCK-001"

G1_SCENARIOS: set[str] = {
    "mixed_rank",
    "tie",
    "dependency",
    "same_root",
    "bulk_high_risk",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_set_is_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(DOCK_TEST_ID) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS, (
        f"{DOCK_TEST_ID} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS)}"
    )


def test_invalid_candidate_shape_fails_closed() -> None:
    candidate = {
        "decision_id": "d1",
        "root_id": "r1",
        "status": "open",
        "safety_or_irreversible": True,
        "blocking_scope": "unknown",
        "depends_on": ["d2", "d2"],
        "expires_at": None,
        "created_at": "2026-08-14T12:00:00Z",
    }
    with pytest.raises(ValueError):
        project_decision_dock(
            [candidate], server_now=parse_rfc3339("2026-08-14T12:00:00Z")
        )


def test_dock_replay_and_frontier_refresh_are_idempotent(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    variant = next(
        item
        for item in contracts.variants(DOCK_TEST_ID)
        if item.variant_id == "mixed_rank" and item.run_gate == "G1"
    )
    database = tmp_path / "dock-replay-refresh.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    _seed(engine, variant.fixture.body)
    command = deepcopy(variant.fixture.body["operation_sequence"][0])
    now = parse_rfc3339(
        command["input"]["injected_results"][0]["server_now"]
    )

    first, _ = apply_decision_dock(engine, command, now=now)
    after_first = _snapshot(engine)
    replay, _ = apply_decision_dock(engine, command, now=now)
    assert replay == first
    assert _snapshot(engine) == after_first

    with engine.connect() as connection, connection.begin():
        connection.execute(
            text("UPDATE decisions SET status = 'consumed' WHERE decision_id = 'd1'")
        )
    refreshed = deepcopy(command)
    refreshed["operation_id"] = "op-dock-refresh"
    refreshed["idempotency_key"] = "idem-dock-refresh"
    projection, _ = apply_decision_dock(engine, refreshed, now=now)
    assert projection.ordered_decision_ids == ("d2",)
    with engine.connect() as connection:
        rows = {
            row[0]: (bool(row[1]), row[2])
            for row in connection.execute(
                text(
                    "SELECT decision_id, actionable, projection_version "
                    "FROM decision_card_projections"
                )
            )
        }
    assert rows["d1"][0] is False
    assert rows["d2"][0] is True
    assert rows["d1"][1] >= 2
    assert rows["d2"][1] >= 2


def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [
        v for v in contracts.variants(DOCK_TEST_ID) if v.run_gate == "G1"
    ]
    assert variants, f"no G1 variants for {DOCK_TEST_ID}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"dock-{variant.variant_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_dock_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        oracle = variant.oracle.body
        result = compare(trace, oracle)
        divergences = list(result.mismatches)
        divergences.extend(dock_persisted_divergences(database, trace))
        if divergences:
            failures.append(
                f"{variant.variant_id}: " + "; ".join(divergences)
            )

    assert not failures, (
        f"{len(failures)} of {len(variants)} {DOCK_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )
