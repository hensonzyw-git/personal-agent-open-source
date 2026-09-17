"""DAL-022: plan cross-field validation — `DAL-T-PLAN-XFIELD-001`.

Nine frozen G3 variants, all offline replayable. One happy path
(`plan_complete`) isolates no rule and moves the feature
`planning → awaiting_plan_review` (`plan.ready`, four-write accept set,
`APPLIED`); the other eight each isolate exactly one §4 cross-field rule and
move the feature `planning → needs_human` (`feature.blocked`,
`PROVIDER_CONTRACT_FAILURE`, reason owner `feature`, seven-write block set).
Blocking is the operation succeeding at its job, not a refusal.

The fixtures are labelled G3 because the pure cross-field decision is offline
replayable; the G4 gate is not used by this test id. No
`dal.test-receipt/1.0` PASS is claimed here — that is §9 item 3's separate,
gated deliverable.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import plan_cross_fields as plan_cross_fields_policy

from tests.dal import plan_cross_fields_executor
from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.plan_cross_fields_executor import execute_plan_cross_fields_fixture
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-PLAN-XFIELD-001"

FROZEN_VARIANTS: set[str] = {
    "plan_complete",
    "identity_mismatch",
    "paths_overlap_file_in_dir",
    "paths_overlap_dir_in_dir",
    "paths_overlap_equal",
    "order_gap",
    "dependency_not_earlier",
    "unknown_verification",
    "digest_drift",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_variant_set_is_closed(contracts: FrozenContracts) -> None:
    """Every frozen variant is consumed; nothing frozen is silently skipped."""
    frozen = {v.variant_id for v in contracts.variants(TEST_ID)}
    assert frozen == FROZEN_VARIANTS, (
        f"{TEST_ID} variant set drifted: {sorted(frozen ^ FROZEN_VARIANTS)}"
    )


def test_every_scenario_variant_matches_its_oracle(contracts: FrozenContracts) -> None:
    """Replay every frozen variant and report all divergences at once."""
    variants = list(contracts.variants(TEST_ID))
    assert variants, f"no variants for {TEST_ID}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        trace = execute_plan_cross_fields_fixture(variant.fixture.body, probe=probe)
        oracle = variant.oracle.body
        result = compare(trace, oracle)
        divergences = list(result.mismatches)
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
        f"{len(failures)} of {len(variants)} {TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def _command(contracts: FrozenContracts, variant_id: str) -> dict:
    variant = next(
        v for v in contracts.variants(TEST_ID) if v.variant_id == variant_id
    )
    return deepcopy(variant.fixture.body["operation_sequence"][0])


def test_malformed_envelopes_are_stable_invalid_arguments(
    contracts: FrozenContracts,
) -> None:
    """Malformed trusted shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _command(contracts, "plan_complete")
    command["input"]["action_sequence"] = []
    cases.append(("empty action sequence", command))

    command = _command(contracts, "plan_complete")
    command["input"]["action_sequence"].pop()
    cases.append(("truncated action group", command))

    command = _command(contracts, "plan_complete")
    command["input"]["injected_results"] = None
    cases.append(("non-list injected results", command))

    command = _command(contracts, "plan_complete")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "plan_complete")
    command["input"]["authoritative_facts"]["input_manifest"]["base_sha"] = "zz"
    cases.append(("malformed input manifest base sha", command))

    command = _command(contracts, "plan_complete")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    command = _command(contracts, "plan_complete")
    command["input"]["target"]["state"] = "coding"
    cases.append(("target outside planning", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            plan_cross_fields_policy.validate_plan_cross_fields(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_wrong_actor_and_evidence_are_their_own_refusals(
    contracts: FrozenContracts,
) -> None:
    """Actor and evidence mismatches carry their named refusal codes."""
    command = _command(contracts, "plan_complete")
    command["actor_type"] = "worker"
    with pytest.raises(DalError) as raised:
        plan_cross_fields_policy.validate_plan_cross_fields(command)
    assert raised.value.code is DalErrorCode.ACTOR_NOT_ALLOWED

    command = _command(contracts, "plan_complete")
    command["evidence_source_type"] = "policy-engine"
    with pytest.raises(DalError) as raised:
        plan_cross_fields_policy.validate_plan_cross_fields(command)
    assert raised.value.code is DalErrorCode.SCOPE_DENIED


def test_planner_evidence_status_never_authorizes_a_plan(
    contracts: FrozenContracts,
) -> None:
    """A plan payload cannot pass on a missing/unfinished planner result."""
    unfinished = _command(contracts, "plan_complete")
    unfinished["input"]["injected_results"][0]["status"] = "timed_out"
    outcome = plan_cross_fields_policy.validate_plan_cross_fields(unfinished)
    assert outcome.final_state == "needs_human"
    assert outcome.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert outcome.receipt.code.value == "APPLIED"


def test_executor_observes_a_forbidden_boundary_crossing(
    contracts: FrozenContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must fail if the handler performs even a file read."""

    def impure_handler(_command: dict):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        plan_cross_fields_executor, "validate_plan_cross_fields", impure_handler
    )
    fixture = next(
        v.fixture.body
        for v in contracts.variants(TEST_ID)
        if v.variant_id == "plan_complete"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_plan_cross_fields_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_plan_cross_fields_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(plan_cross_fields_policy.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == {
        "__future__",
        "dataclasses",
        "itertools",
        "typing",
        "personal_agent_dal.errors",
        "personal_agent_dal.machine.registry",
        "personal_agent_dal.receipt",
    }
    forbidden_calls = {"open", "exec", "eval", "compile", "__import__"}
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called_names & forbidden_calls)
