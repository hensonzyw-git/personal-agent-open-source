"""DAL-024: carry-forward open finding set — `DAL-T-OPENSET-001`.

Eight frozen G3 variants, all offline replayable. The controller re-derives
the open finding set per round (the original review's findings, minus the
findings each prior verdict resolved ``closed``, plus each prior verdict's new
findings — refrozen 2026-08-29, evidence
`DAL_R09-A2_review-fix-loop_2026-08-29.md` §2c D1) and judges the injected
verdict against it. A legal `verified` verifies the feature
(`reviewing → verified`, `review.completed`, four-write base set); a legal
`changes_requested` requests a fix (`reviewing → fixing`, `fix.requested`,
four-write base set); any violation of the carry-forward invariants —
omitted/renamed carried findings, an original finding silently dropped behind
the chain (`original_remaining_omitted`), an ID reuse, or a `verified`
verdict that still carries new findings — blocks the feature
(`reviewing → needs_human`, `feature.blocked`, `PROVIDER_CONTRACT_FAILURE`,
seven-write block set).

No `dal.test-receipt/1.0` PASS is claimed here — that is §9 item 3's separate,
gated deliverable.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import open_finding_set as open_finding_set_policy

from tests.dal import open_finding_set_executor
from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.open_finding_set_executor import execute_open_finding_set_fixture
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-OPENSET-001"

FROZEN_VARIANTS: set[str] = {
    "init_from_review",
    "carry_forward_exact",
    "remaining_declared",
    "carried_finding_omitted",
    "carried_finding_renamed",
    "new_finding_id_reused",
    "verified_with_new_findings",
    "original_remaining_omitted",
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
        trace = execute_open_finding_set_fixture(variant.fixture.body, probe=probe)
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

    command = _command(contracts, "init_from_review")
    command["input"]["action_sequence"] = [
        {"command": "record_review"},
        {"command": "derive_open_finding_set"},
    ]
    cases.append(("reordered action group", command))

    command = _command(contracts, "init_from_review")
    command["input"]["action_sequence"].pop()
    cases.append(("truncated action group", command))

    command = _command(contracts, "init_from_review")
    command["input"]["injected_results"] = None
    cases.append(("non-list injected results", command))

    command = _command(contracts, "init_from_review")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "init_from_review")
    command["input"]["authoritative_facts"]["recomputed_result_sha"] = "zz"
    cases.append(("malformed recomputed result sha", command))

    command = _command(contracts, "init_from_review")
    command["input"]["authoritative_facts"]["round_anchors"]["anchor_sha"] = "zz"
    cases.append(("malformed round anchor sha", command))

    command = _command(contracts, "init_from_review")
    command["input"]["injected_results"][1]["verdict"]["verdict"] = "bogus"
    cases.append(("unknown verdict value", command))

    command = _command(contracts, "init_from_review")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    command = _command(contracts, "init_from_review")
    command["input"]["target"]["state"] = "coding"
    cases.append(("target outside reviewing", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            open_finding_set_policy.derive_open_finding_set(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_wrong_actor_and_evidence_are_their_own_refusals(
    contracts: FrozenContracts,
) -> None:
    """Actor and evidence mismatches carry their named refusal codes."""
    command = _command(contracts, "init_from_review")
    command["actor_type"] = "worker"
    with pytest.raises(DalError) as raised:
        open_finding_set_policy.derive_open_finding_set(command)
    assert raised.value.code is DalErrorCode.ACTOR_NOT_ALLOWED

    command = _command(contracts, "init_from_review")
    command["evidence_source_type"] = "policy-engine"
    with pytest.raises(DalError) as raised:
        open_finding_set_policy.derive_open_finding_set(command)
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
        open_finding_set_executor, "derive_open_finding_set", impure_handler
    )
    fixture = next(
        v.fixture.body
        for v in contracts.variants(TEST_ID)
        if v.variant_id == "init_from_review"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_open_finding_set_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_open_finding_set_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(open_finding_set_policy.__file__)
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
