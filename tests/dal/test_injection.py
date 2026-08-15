"""DAL-012: injection policy — `DAL-T-INJECTION-001`.

`DAL-T-INJECTION-001/api_intake` proves the intake injection boundary: tainted
content requesting capabilities beyond the current set (production_credentials
+ unrestricted_repository_write over read_workspace) is refused `POLICY_DENIED`
with zero writes and the feature left in `intake`.

The G1 gate owns exactly one variant — `api_intake`. The `diff`/`issue`/
`readme`/`test_failure` (G2) and `provider_output` (G4) carriers are later-wave
work and are deliberately excluded here.

Each G1 variant is hash-bound to its frozen fixture and oracle, replayed against
the real `evaluate_untrusted_content`, and judged by `oracle_comparator.compare`.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import injection as injection_policy

from tests.dal import injection_executor
from tests.dal.contract_loader import FrozenContracts
from tests.dal.injection_executor import execute_injection_fixture
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


INJECTION_TEST_ID = "DAL-T-INJECTION-001"

G1_SCENARIOS: set[str] = {
    "api_intake",
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g1_scenario_set_is_closed(contracts: FrozenContracts) -> None:
    """Every G1 variant is consumed; nothing frozen is silently skipped."""
    g1 = {
        v.variant_id for v in contracts.variants(INJECTION_TEST_ID) if v.run_gate == "G1"
    }
    assert g1 == G1_SCENARIOS, (
        f"{INJECTION_TEST_ID} G1 scenario set drifted: "
        f"{sorted(g1 ^ G1_SCENARIOS)}"
    )


def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts,
) -> None:
    """Replay every G1 variant and report all divergences at once."""
    variants = [
        v for v in contracts.variants(INJECTION_TEST_ID) if v.run_gate == "G1"
    ]
    assert variants, f"no G1 variants for {INJECTION_TEST_ID}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        trace = execute_injection_fixture(variant.fixture.body, probe=probe)
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
        f"{len(failures)} of {len(variants)} {INJECTION_TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def _api_intake_command(contracts: FrozenContracts) -> dict:
    variant = next(
        v
        for v in contracts.variants(INJECTION_TEST_ID)
        if v.run_gate == "G1" and v.variant_id == "api_intake"
    )
    return deepcopy(variant.fixture.body["operation_sequence"][0])


def test_malformed_nested_inputs_are_stable_invalid_arguments(
    contracts: FrozenContracts,
) -> None:
    """Malformed untrusted shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _api_intake_command(contracts)
    command["input"]["action_sequence"].insert(1, "malformed-step")
    cases.append(("non-object action step", command))

    command = _api_intake_command(contracts)
    command["input"]["injected_results"] = None
    cases.append(("non-list parser results", command))

    command = _api_intake_command(contracts)
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _api_intake_command(contracts)
    command["input"]["authoritative_facts"]["current_capabilities"] = "read_workspace"
    cases.append(("non-list capabilities", command))

    command = _api_intake_command(contracts)
    command["input"]["injected_results"] = ["malformed-result"]
    cases.append(("non-object parser result", command))

    command = _api_intake_command(contracts)
    del command["input"]["injected_results"][0]["tainted"]
    cases.append(("completed result missing taint", command))

    command = _api_intake_command(contracts)
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    for label, malformed in cases:
        with pytest.raises(DalError, match="INVALID_ARGUMENT") as raised:
            injection_policy.evaluate_untrusted_content(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_uncleared_parser_results_remain_fail_closed(
    contracts: FrozenContracts,
) -> None:
    """Missing or incomplete parser evidence can never permit the request."""
    missing = _api_intake_command(contracts)
    missing["input"]["injected_results"] = []
    assert (
        injection_policy.evaluate_untrusted_content(missing).receipt.code.value
        == "POLICY_DENIED"
    )

    incomplete = _api_intake_command(contracts)
    incomplete["input"]["injected_results"] = [
        {"source": "content_parser", "status": "timed_out"}
    ]
    assert (
        injection_policy.evaluate_untrusted_content(incomplete).receipt.code.value
        == "POLICY_DENIED"
    )


def test_executor_observes_a_forbidden_boundary_crossing(
    contracts: FrozenContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must fail if the handler performs even a file read."""

    def impure_handler(_command: dict):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        injection_executor, "evaluate_untrusted_content", impure_handler
    )
    fixture = next(
        v.fixture.body
        for v in contracts.variants(INJECTION_TEST_ID)
        if v.run_gate == "G1" and v.variant_id == "api_intake"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_injection_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_injection_policy_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(injection_policy.__file__)
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
