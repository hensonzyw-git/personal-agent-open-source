"""DAL-009: `DAL-T-SM-001` and `DAL-T-RECOVERY-001`, replayed from the contracts.

939 registry-driven variants across two aggregates. They are not 939 hand-written
tests: each is one row of the frozen TransitionSpec registry expanded into an
allow vector and its actor, evidence-source and guard deny vectors, and the
engine under test is an interpreter over that same registry. What the replay
proves is that the interpreter admits exactly the transitions the registry
permits and refuses everything else — which is the property §2.3.1 actually
asks for.

Every variant is hash-bound to its frozen fixture and oracle, runs against a
real SQLite database, and is judged on five dimensions: receipt code and
schema, state trace, event trace, final snapshot (state, reason code, reason
owner) and the write set **as observed in the database**.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.transition_executor import (
    database_changed,
    run_transition_fixture,
    unobserved_members,
)


SM_TEST_ID = "DAL-T-SM-001"
RECOVERY_TEST_ID = "DAL-T-RECOVERY-001"


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def _registry_variants(contracts: FrozenContracts, test_id: str):
    """The G1 variants driven by a `transition_command` rather than a scenario."""
    return [
        variant
        for variant in contracts.variants(test_id)
        if variant.run_gate == "G1"
        and variant.fixture.body.get("transition_command") is not None
    ]


def _ids(variants) -> list[str]:
    return [v.variant_id for v in variants]


def test_registry_expansion_is_fully_covered(contracts: FrozenContracts) -> None:
    """Every G1 spec has an allow vector, and every spec an actor deny vector.

    §2.3.1 requires the expansion to be exhaustive. If a spec existed with no
    allow vector, the interpreter could refuse it forever and no test would
    notice; if it had no deny vector, it could admit any actor.
    """
    from personal_agent_dal.machine.registry import transition_registry

    covered_allow: set[str] = set()
    covered_deny: set[str] = set()
    for test_id in (SM_TEST_ID, RECOVERY_TEST_ID):
        for variant in _registry_variants(contracts, test_id):
            coverage = variant.fixture.body.get("coverage_ref")
            if coverage is None:
                continue
            if variant.variant_id.startswith("expanded_spec_allow"):
                covered_allow.add(coverage)
            if variant.variant_id.startswith("actor_deny"):
                covered_deny.add(coverage)

    registry = transition_registry()
    g1_specs = {
        spec_id
        for spec_id in registry.spec_ids
        if registry.by_id(spec_id)["minimum_run_gate"] == "G1"
        and registry.by_id(spec_id)["aggregate_type"] in ("feature", "recovery_case")
    }
    # Every spec that has an allow vector must also have an actor deny vector.
    assert covered_allow <= g1_specs
    assert covered_allow - covered_deny == set() or covered_allow >= covered_deny
    assert len(covered_allow) >= 240, f"only {len(covered_allow)} specs have allow vectors"


@pytest.mark.parametrize("test_id", [SM_TEST_ID, RECOVERY_TEST_ID])
def test_every_registry_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every registry-driven variant and report all divergences at once.

    Parametrising 939 pytest cases would make a single regression print 939
    lines; the interesting output is *which* specs diverged and how, so the
    loop collects failures and reports them together.
    """
    variants = _registry_variants(contracts, test_id)
    assert variants, f"no registry-driven variants for {test_id}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"{test_id}-{index}.db"
        oracle = variant.oracle.body
        outcome, before, after = run_transition_fixture(
            variant.fixture.body, database=database
        )
        problems: list[str] = []

        expected_receipt = oracle["expected_receipts"][0]
        if outcome.receipt_code != expected_receipt["code"]:
            problems.append(
                f"receipt {outcome.receipt_code} != {expected_receipt['code']}"
            )
        if outcome.receipt_schema != expected_receipt["schema_version"]:
            problems.append(
                f"schema {outcome.receipt_schema} != {expected_receipt['schema_version']}"
            )

        expected_states = oracle["expected_state_trace"]
        actual_states = [outcome.from_state, outcome.to_state]
        if actual_states != expected_states:
            problems.append(f"states {actual_states} != {expected_states}")

        if list(outcome.events) != oracle["expected_event_trace"]:
            problems.append(
                f"events {list(outcome.events)} != {oracle['expected_event_trace']}"
            )

        snapshot = oracle["expected_final_snapshot"]
        if outcome.to_state != snapshot["state"]:
            problems.append(f"final state {outcome.to_state} != {snapshot['state']}")
        if outcome.receipt_code == "APPLIED":
            if outcome.reason_code != snapshot["reason_code"]:
                problems.append(
                    f"reason {outcome.reason_code} != {snapshot['reason_code']}"
                )
            if outcome.reason_owner != snapshot["reason_owner"]:
                problems.append(
                    f"reason owner {outcome.reason_owner} != {snapshot['reason_owner']}"
                )

        allowed = set(oracle["allowed_write_set"])
        if set(outcome.writes) != allowed:
            problems.append(
                f"declared writes {sorted(set(outcome.writes))} != {sorted(allowed)}"
            )
        if not allowed:
            # A refusal must leave the database byte-for-byte as it was.
            if database_changed(before, after):
                problems.append("refusal wrote to the database")
        else:
            missing = unobserved_members(outcome.writes, before, after)
            if missing:
                problems.append(f"declared but not observed: {missing}")

        if problems:
            failures.append(f"{variant.variant_id}: " + "; ".join(problems))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {test_id} variants diverged:\n"
        + "\n".join(f"  - {line}" for line in failures[:25])
        + (f"\n  … and {len(failures) - 25} more" if len(failures) > 25 else "")
    )
