"""DAL-025 (offline slice): controller-dispatch graph — `DAL-T-GRAPH-001`.

Fifteen frozen G3 variants, all offline replayable. The dispatch graph is the
controller-side composition: given a feature state and the controller's facts,
it decides the node, orchestration action, handler sequence and resulting
command, then the transition the feature takes. The variants cover the two
failure vocabularies (a fabricated command → `ILLEGAL_TRANSITION`; a real
command misrouted to the wrong state → `CONTRACT_SCHEMA_INVALID`), the
provider-node threat vectors (cancel / empty / seam gate / drift / independence
reuse / loop limit), the round-1 and round-2 review compositions, and the
deterministic-node provider rejection.

The fixtures are labelled G3 because the pure dispatch decision is offline
replayable; the G4 gate is not used by this test id. No `dal.test-receipt/1.0`
PASS is claimed here — that is a separate, gated deliverable, exactly as for
the Task #12 decision functions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import dispatch_graph as dispatch_graph_policy

from tests.dal import dispatch_graph_executor
from tests.dal.dispatch_contract_loader import DispatchContracts
from tests.dal.dispatch_graph_executor import execute_dispatch_graph_fixture
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe

TEST_ID = "DAL-T-GRAPH-001"

FROZEN_VARIANTS: set[str] = {
    "provider_node_empty_response",
    "provider_node_cancelled",
    "provider_node_cancelled_no_receipt",
    "provider_node_wrong_route",
    "handler_pass_fail_composition",
    "deterministic_node_no_provider",
    "transition_legality",
    "round_dispatch_first",
    "round_dispatch_post_fix",
    "round_dispatch_gaps_only",
    "round_dispatch_after_replan",
    "loop_limit_in_fixing",
    "seam_injection_no_subprocess",
    "seam_subprocess_gated",
    "drift_fail_closed",
}

_VALID_FACTS: dict[str, object] = {
    "cancel_receipt_present": False,
    "has_findings_receipt_since_plan": False,
    "preflight_receipt_present": True,
    "prior_findings_receipts": 0,
    "record_plan_receipt_present": True,
    "review_fix_cycle_count": 0,
}


@pytest.fixture(scope="module")
def contracts() -> DispatchContracts:
    return DispatchContracts()


def test_variant_set_is_closed(contracts: DispatchContracts) -> None:
    """Every frozen variant is consumed; nothing frozen is silently skipped."""
    frozen = {v.variant_id for v in contracts.variants(TEST_ID)}
    assert frozen == FROZEN_VARIANTS, (
        f"{TEST_ID} variant set drifted: {sorted(frozen ^ FROZEN_VARIANTS)}"
    )


def test_every_scenario_variant_matches_its_oracle(
    contracts: DispatchContracts,
) -> None:
    """Replay every frozen variant and report all divergences at once."""
    variants = list(contracts.variants(TEST_ID))
    assert variants, f"no variants for {TEST_ID}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        execution = execute_dispatch_graph_fixture(variant.fixture, probe=probe)
        oracle = variant.oracle
        result = compare(execution.trace, oracle)
        divergences = list(result.mismatches)

        if execution.dispatch != oracle.get("expected_dispatch"):
            divergences.append(
                "dispatch: expected "
                f"{oracle.get('expected_dispatch')!r}, got {execution.dispatch!r}"
            )
        if execution.coverage_ref != oracle.get("coverage_ref"):
            divergences.append(
                "coverage_ref: expected "
                f"{oracle.get('coverage_ref')!r}, got {execution.coverage_ref!r}"
            )
        if set(execution.trace.declared_write_set) != set(execution.trace.write_set):
            divergences.append(
                "declared write set differs from observed write set: "
                f"{execution.trace.declared_write_set!r} != {execution.trace.write_set!r}"
            )
        if probe.observed:
            divergences.append(f"unexpected side effects: {sorted(probe.observed)!r}")
        if divergences:
            failures.append(f"{variant.variant_id}: " + "; ".join(divergences))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "state": "planning",
        "entity_type": "feature",
        "facts": dict(_VALID_FACTS),
        "seam": "injected",
        "stream": [{"type": "final"}],
        "attempted_resulting_command": None,
        "provider_attempted": False,
    }
    base.update(overrides)
    return base


def test_malformed_trusted_shapes_are_stable_invalid_arguments() -> None:
    """Malformed trusted shapes fail as DalError, never Python exceptions."""
    facts_missing_key = {k: v for k, v in _VALID_FACTS.items() if k != "preflight_receipt_present"}
    facts_bad_bool = dict(_VALID_FACTS, cancel_receipt_present=1)

    cases: list[tuple[str, dict[str, object]]] = [
        ("unknown state", _kwargs(state="not_a_state")),
        ("non-feature entity", _kwargs(entity_type="recovery_case")),
        ("non-object facts", _kwargs(facts=None)),
        ("facts missing a key", _kwargs(facts=facts_missing_key)),
        ("non-boolean fact", _kwargs(facts=facts_bad_bool)),
        ("unknown seam", _kwargs(seam="remote")),
        ("non-list stream", _kwargs(stream="cancelled")),
        ("non-string attempted command", _kwargs(attempted_resulting_command=3)),
        ("non-boolean provider_attempted", _kwargs(provider_attempted="yes")),
    ]

    for label, kwargs in cases:
        with pytest.raises(DalError) as raised:
            dispatch_graph_policy.dispatch_decision(**kwargs)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_executor_observes_a_forbidden_boundary_crossing(
    contracts: DispatchContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must fail if the handler performs even a file read."""

    def impure_handler(**_kwargs: object):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        dispatch_graph_executor, "dispatch_decision", impure_handler
    )
    fixture = next(
        v.fixture
        for v in contracts.variants(TEST_ID)
        if v.variant_id == "seam_injection_no_subprocess"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_dispatch_graph_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_dispatch_graph_dependency_surface_is_closed() -> None:
    """The pure decision cannot acquire an unguarded I/O dependency."""
    source_path = Path(dispatch_graph_policy.__file__)
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
