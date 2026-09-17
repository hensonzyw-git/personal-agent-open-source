"""DAL-024: post-fix verdict two-layer boundary — `DAL-T-FIXDIFF-001`.

Thirteen frozen G3 variants, all offline replayable. The controller re-derives a
structural violation set (anchor entry, path continuity, surviving-set /
increment algebra, evidence roles, new-finding anchor, acceptance) and only
adopts a provider `verified` when that set is empty. A clean `verified` moves
the feature `reviewing → verified` (`review.completed`, four-write base set);
a clean `changes_requested` moves `reviewing → fixing` (`fix.requested`,
four-write base set) — whether it carries a remaining resolution or only new
findings (refrozen 2026-08-29, evidence
`DAL_R09-A2_review-fix-loop_2026-08-29.md` §2c D2/D3: new findings anchor to
the verdict's `result_sha`, and a non-empty `new_findings[]` alone forces
`changes_requested`); any structural violation moves `reviewing → needs_human`
(`feature.blocked`, `PROVIDER_CONTRACT_FAILURE`, seven-write block set).

No `dal.test-receipt/1.0` PASS is claimed here — that is §9 item 3's separate,
gated deliverable.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import post_fix_verdict as post_fix_verdict_policy

from tests.dal import post_fix_verdict_executor
from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.post_fix_verdict_executor import execute_post_fix_verdict_fixture
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-FIXDIFF-001"

FROZEN_VARIANTS: set[str] = {
    "verified_clean",
    "gap_closed_by_test_receipts",
    "changes_requested_declared",
    "changes_requested_new_findings_only",
    "evidence_role_violation",
    "anchor_entry_not_blob",
    "path_died_between_rounds",
    "surviving_set_empty",
    "increment_missed_surviving_lines",
    "no_deletion_in_increment",
    "gap_closed_by_fix_diff_only",
    "new_finding_anchor_mismatch",
    "verified_with_unverified_acceptance",
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
        trace = execute_post_fix_verdict_fixture(variant.fixture.body, probe=probe)
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

    command = _command(contracts, "verified_clean")
    command["input"]["action_sequence"] = [
        {"command": "record_review"},
        {"command": "validate_post_fix_verdict"},
    ]
    cases.append(("reordered action group", command))

    command = _command(contracts, "verified_clean")
    command["input"]["action_sequence"].pop()
    cases.append(("truncated action group", command))

    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"] = None
    cases.append(("non-list injected results", command))

    command = _command(contracts, "verified_clean")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "verified_clean")
    command["input"]["authoritative_facts"]["round_anchors"]["anchor_sha"] = "zz"
    cases.append(("malformed round anchor sha", command))

    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"][1]["verdict"]["acceptance_verified"] = "yes"
    cases.append(("non-boolean acceptance_verified", command))

    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"][1]["verdict"]["verdict"] = "bogus"
    cases.append(("unknown verdict value", command))

    command = _command(contracts, "verified_clean")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    command = _command(contracts, "verified_clean")
    command["input"]["target"]["state"] = "coding"
    cases.append(("target outside reviewing", command))

    # Round-5 review F-2: _structural dereferences every verdict member
    # directly, so a non-list container or a member outside its closed field
    # set must be rejected as trusted envelope drift — never a
    # TypeError/KeyError/IndexError out of the structural pass on the direct
    # OP-FIXDIFF-001 dispatch path.
    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"][1]["verdict"]["finding_resolutions"] = "abc"
    cases.append(("verdict resolutions not a list", command))

    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"][1]["verdict"][
        "acceptance_gap_resolutions"
    ] = 7
    cases.append(("verdict gap resolutions not a list", command))

    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"][1]["verdict"]["new_findings"] = None
    cases.append(("verdict new findings not a list", command))

    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"][1]["verdict"]["finding_resolutions"][0] = None
    cases.append(("verdict resolution None member", command))

    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"][1]["verdict"]["finding_resolutions"][0][
        "extra"
    ] = "x"
    cases.append(("verdict resolution extra field", command))

    command = _command(contracts, "verified_clean")
    command["input"]["injected_results"][1]["verdict"]["new_findings"] = [
        {"finding_id": "F-9"}
    ]
    cases.append(("verdict new finding shape not closed", command))

    # Round-6 review F4: the structural pass dereferences the trusted facts'
    # deep members directly (``test_receipts[digest].get(...)``,
    # ``plan_verification_ids[acceptance_id]`` membership, the original
    # review's id lists feeding set builds), so a drifted member is trusted
    # controller state and must fail closed as INVALID_ARGUMENT — never an
    # AttributeError/TypeError, never a silently accepted garbage id set.
    command = _command(contracts, "gap_closed_by_test_receipts")
    gap = command["input"]["injected_results"][1]["verdict"][
        "acceptance_gap_resolutions"
    ][0]
    command["input"]["authoritative_facts"]["test_receipts"][
        gap["evidence_sha256"][0]
    ] = []
    cases.append(("test receipt member not an object", command))

    command = _command(contracts, "gap_closed_by_test_receipts")
    command["input"]["authoritative_facts"]["test_receipts"]["not-a-digest"] = {
        "verification_id": "VR-1"
    }
    cases.append(("test receipt key not a digest", command))

    command = _command(contracts, "gap_closed_by_test_receipts")
    gap = command["input"]["injected_results"][1]["verdict"][
        "acceptance_gap_resolutions"
    ][0]
    command["input"]["authoritative_facts"]["plan_verification_ids"][
        gap["acceptance_id"]
    ] = None
    cases.append(("plan verification ids value not a list", command))

    command = _command(contracts, "gap_closed_by_test_receipts")
    command["input"]["authoritative_facts"]["plan_verification_ids"][7] = ["VR-1"]
    cases.append(("plan verification ids key not a string", command))

    command = _command(contracts, "verified_clean")
    command["input"]["authoritative_facts"]["original_review"]["finding_ids"] = [
        ["F-1"]
    ]
    cases.append(("original finding_ids list element", command))

    command = _command(contracts, "verified_clean")
    command["input"]["authoritative_facts"]["original_review"][
        "acceptance_gap_ids"
    ] = [["AC-1"]]
    cases.append(("original gap ids list element", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            post_fix_verdict_policy.validate_post_fix_verdict(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


@pytest.mark.parametrize("value", [None, [], {}, True, b"VR-1", "", 1])
def test_round7_receipt_verification_id_is_trusted(
    contracts: FrozenContracts, value: object,
) -> None:
    command = _command(contracts, "gap_closed_by_test_receipts")
    payload = command["input"]
    gap = payload["injected_results"][1]["verdict"]["acceptance_gap_resolutions"][0]
    payload["authoritative_facts"]["test_receipts"][gap["evidence_sha256"][0]]["verification_id"] = value
    with pytest.raises(DalError) as raised:
        post_fix_verdict_policy.validate_post_fix_verdict(command)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_round7_missing_receipt_id_differs_from_wrong_verification(
    contracts: FrozenContracts,
) -> None:
    command = _command(contracts, "gap_closed_by_test_receipts")
    payload = command["input"]
    gap = payload["injected_results"][1]["verdict"]["acceptance_gap_resolutions"][0]
    receipt = payload["authoritative_facts"]["test_receipts"][gap["evidence_sha256"][0]]
    receipt["verification_id"] = "unrelated-verification"
    result = post_fix_verdict_policy.validate_post_fix_verdict(command)
    assert result.final_reason_code == "PROVIDER_CONTRACT_FAILURE"
    assert result.reasons == ("gap_evidence",)
    del receipt["verification_id"]
    with pytest.raises(DalError) as raised:
        post_fix_verdict_policy.validate_post_fix_verdict(command)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_wrong_actor_and_evidence_are_their_own_refusals(
    contracts: FrozenContracts,
) -> None:
    """Actor and evidence mismatches carry their named refusal codes."""
    command = _command(contracts, "verified_clean")
    command["actor_type"] = "worker"
    with pytest.raises(DalError) as raised:
        post_fix_verdict_policy.validate_post_fix_verdict(command)
    assert raised.value.code is DalErrorCode.ACTOR_NOT_ALLOWED

    command = _command(contracts, "verified_clean")
    command["evidence_source_type"] = "policy-engine"
    with pytest.raises(DalError) as raised:
        post_fix_verdict_policy.validate_post_fix_verdict(command)
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
        post_fix_verdict_executor, "validate_post_fix_verdict", impure_handler
    )
    fixture = next(
        v.fixture.body
        for v in contracts.variants(TEST_ID)
        if v.variant_id == "verified_clean"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_post_fix_verdict_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_post_fix_verdict_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(post_fix_verdict_policy.__file__)
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
