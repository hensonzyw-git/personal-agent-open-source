"""DAL-023: reviewer independence — `DAL-T-REVIEW-INDEP-001`.

Five frozen variants split across gates: `same_session`, `same_context`,
`same_independence_key` and `synthetic_fresh` are G3 (offline replayable);
`live_fresh` is G4 (needs the real reviewer subprocess — see the DAL-006
File-mode block). This file replays the pure independence decision for all
five, because the decision itself is offline; the G3/G4 *receipt* split is
§9 item 3's separate, gated deliverable and no `dal.test-receipt/1.0` PASS is
claimed here.

The frozen oracle pins the asymmetry that matters: a fresh reviewer moves the
feature `reviewing → verified` (`review.completed`, four-write accept set,
`APPLIED`), while any of the three reuse equalities is a clean `POLICY_DENIED`
with **zero** writes and no state change.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import review_independence as review_independence_policy

from tests.dal import review_independence_executor
from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.review_independence_executor import (
    execute_review_independence_fixture,
)
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-REVIEW-INDEP-001"

G3_VARIANTS: set[str] = {
    "same_session",
    "same_context",
    "same_independence_key",
    "synthetic_fresh",
}

FROZEN_VARIANTS: set[str] = G3_VARIANTS | {"live_fresh"}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_variant_set_is_closed(contracts: FrozenContracts) -> None:
    """Every frozen variant is consumed; nothing frozen is silently skipped."""
    frozen = {v.variant_id for v in contracts.variants(TEST_ID)}
    assert frozen == FROZEN_VARIANTS, (
        f"{TEST_ID} variant set drifted: {sorted(frozen ^ FROZEN_VARIANTS)}"
    )


def test_g3_assignment_matches_the_machine_manifest(contracts: FrozenContracts) -> None:
    """G3 set follows the frozen manifest, not the freeze-package transcript."""
    g3 = {v.variant_id for v in contracts.variants(TEST_ID) if v.run_gate == "G3"}
    assert g3 == G3_VARIANTS, f"G3 set drifted: {sorted(g3 ^ G3_VARIANTS)}"


def test_every_scenario_variant_matches_its_oracle(contracts: FrozenContracts) -> None:
    """Replay every frozen variant and report all divergences at once."""
    variants = list(contracts.variants(TEST_ID))
    assert variants, f"no variants for {TEST_ID}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        trace = execute_review_independence_fixture(variant.fixture.body, probe=probe)
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

    command = _command(contracts, "synthetic_fresh")
    command["input"]["action_sequence"] = [
        {"command": "accept_review_receipt"},
        {"command": "verify_reviewer_independence"},
    ]
    cases.append(("reordered action group", command))

    command = _command(contracts, "synthetic_fresh")
    command["input"]["action_sequence"].pop()
    cases.append(("truncated action group", command))

    command = _command(contracts, "synthetic_fresh")
    command["input"]["injected_results"] = None
    cases.append(("non-list injected results", command))

    command = _command(contracts, "synthetic_fresh")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "synthetic_fresh")
    command["input"]["authoritative_facts"]["evidence_kind"] = "guessed"
    cases.append(("unknown evidence kind", command))

    command = _command(contracts, "synthetic_fresh")
    command["input"]["authoritative_facts"]["coder_context_sha256"] = "zz"
    cases.append(("malformed coder context digest", command))

    command = _command(contracts, "synthetic_fresh")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    command = _command(contracts, "synthetic_fresh")
    command["input"]["target"]["state"] = "coding"
    cases.append(("target outside reviewing", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            review_independence_policy.accept_independent_review(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_incomplete_reviewer_evidence_remains_fail_closed(
    contracts: FrozenContracts,
) -> None:
    """Missing or unfinished reviewer evidence can never permit an accept."""
    missing = _command(contracts, "synthetic_fresh")
    missing["input"]["injected_results"] = []
    outcome = review_independence_policy.accept_independent_review(missing)
    assert outcome.receipt.code.value == "POLICY_DENIED"
    assert outcome.declared_write_set == ()
    assert outcome.final_state == "reviewing"

    unfinished = _command(contracts, "synthetic_fresh")
    unfinished["input"]["injected_results"] = [
        {"source": "reviewer", "status": "timed_out", "finding_count": 0}
    ]
    outcome = review_independence_policy.accept_independent_review(unfinished)
    assert outcome.receipt.code.value == "POLICY_DENIED"
    assert outcome.declared_write_set == ()

    wrong_source = _command(contracts, "synthetic_fresh")
    wrong_source["input"]["injected_results"] = [
        {"source": "coder", "status": "completed", "finding_count": 0}
    ]
    outcome = review_independence_policy.accept_independent_review(wrong_source)
    assert outcome.receipt.code.value == "POLICY_DENIED"
    assert outcome.declared_write_set == ()


def test_each_reuse_equality_alone_voids_independence(
    contracts: FrozenContracts,
) -> None:
    """Each §5.1 equality voids on its own; combined violations refuse once."""
    base = _command(contracts, "synthetic_fresh")

    session_only = deepcopy(base)
    session_only["input"]["authoritative_facts"]["reviewer_session_id"] = "session-a"
    outcome = review_independence_policy.accept_independent_review(session_only)
    assert outcome.independence_violations == (
        "reviewer reuses the coder session",
    )

    key_only = deepcopy(base)
    key_only["input"]["authoritative_facts"]["reviewer_independence_key"] = (
        "independent-coder"
    )
    outcome = review_independence_policy.accept_independent_review(key_only)
    assert outcome.independence_violations == (
        "reviewer reuses the coder independence key",
    )

    all_three = deepcopy(base)
    facts = all_three["input"]["authoritative_facts"]
    facts["reviewer_session_id"] = "session-a"
    facts["reviewer_context_sha256"] = facts["coder_context_sha256"]
    facts["reviewer_independence_key"] = facts["coder_independence_key"]
    outcome = review_independence_policy.accept_independent_review(all_three)
    assert len(outcome.independence_violations) == 3
    assert outcome.receipt.code.value == "POLICY_DENIED"
    assert outcome.declared_write_set == ()


def test_executor_observes_a_forbidden_boundary_crossing(
    contracts: FrozenContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must fail if the handler performs even a file read."""

    def impure_handler(_command: dict):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        review_independence_executor, "accept_independent_review", impure_handler
    )
    fixture = next(
        v.fixture.body
        for v in contracts.variants(TEST_ID)
        if v.variant_id == "synthetic_fresh"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_review_independence_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_review_independence_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(review_independence_policy.__file__)
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
