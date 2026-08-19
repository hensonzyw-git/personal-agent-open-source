"""DAL-023: review-disposition recompute — `DAL-T-DISPOSITION-001`.

Six frozen G3 variants, all offline replayable. The controller re-derives the
disposition from coverage, pins and named work, then compares it to the
provider's declared `disposition` string. A legal `approve` verifies the
feature (`reviewing → verified`, `review.completed`, four-write accept set);
a legal `request_changes` requests a fix (`reviewing → fixing`,
`fix.requested`, four-write base set); any recompute disagreement — `approve`
with findings, `request_changes` with nothing named, or incomplete coverage —
blocks the feature (`reviewing → needs_human`, `feature.blocked`,
`PROVIDER_CONTRACT_FAILURE`, seven-write block set).

No `dal.test-receipt/1.0` PASS is claimed here — that is §9 item 3's separate,
gated deliverable.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import review_disposition as review_disposition_policy

from tests.dal import review_disposition_executor
from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.review_disposition_executor import execute_review_disposition_fixture
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-DISPOSITION-001"

FROZEN_VARIANTS: set[str] = {
    "approve_clean",
    "request_changes_findings",
    "request_changes_gaps",
    "coverage_incomplete",
    "provider_approve_with_findings",
    "provider_request_changes_clean",
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
        trace = execute_review_disposition_fixture(variant.fixture.body, probe=probe)
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

    command = _command(contracts, "approve_clean")
    command["input"]["action_sequence"] = [
        {"command": "record_review"},
        {"command": "recompute_review_disposition"},
    ]
    cases.append(("reordered action group", command))

    command = _command(contracts, "approve_clean")
    command["input"]["action_sequence"].pop()
    cases.append(("truncated action group", command))

    command = _command(contracts, "approve_clean")
    command["input"]["injected_results"] = None
    cases.append(("non-list injected results", command))

    command = _command(contracts, "approve_clean")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "approve_clean")
    command["input"]["authoritative_facts"]["recomputed_review_inputs"]["result_sha"] = "zz"
    cases.append(("malformed recomputed result sha", command))

    command = _command(contracts, "approve_clean")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    command = _command(contracts, "approve_clean")
    command["input"]["target"]["state"] = "coding"
    cases.append(("target outside reviewing", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            review_disposition_policy.recompute_review_disposition(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_wrong_actor_and_evidence_are_their_own_refusals(
    contracts: FrozenContracts,
) -> None:
    """Actor and evidence mismatches carry their named refusal codes."""
    command = _command(contracts, "approve_clean")
    command["actor_type"] = "worker"
    with pytest.raises(DalError) as raised:
        review_disposition_policy.recompute_review_disposition(command)
    assert raised.value.code is DalErrorCode.ACTOR_NOT_ALLOWED

    command = _command(contracts, "approve_clean")
    command["evidence_source_type"] = "policy-engine"
    with pytest.raises(DalError) as raised:
        review_disposition_policy.recompute_review_disposition(command)
    assert raised.value.code is DalErrorCode.SCOPE_DENIED


def test_executor_observes_a_forbidden_boundary_crossing(
    contracts: FrozenContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must fail if the handler performs even a file read."""

    def impure_handler(_command: dict):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        review_disposition_executor, "recompute_review_disposition", impure_handler
    )
    fixture = next(
        v.fixture.body
        for v in contracts.variants(TEST_ID)
        if v.variant_id == "approve_clean"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_review_disposition_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_review_disposition_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(review_disposition_policy.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == {
        "__future__",
        "dataclasses",
        "typing",
        "personal_agent_dal.errors",
        "personal_agent_dal.receipt",
    }
    forbidden_calls = {"open", "exec", "eval", "compile", "__import__"}
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called_names & forbidden_calls)
