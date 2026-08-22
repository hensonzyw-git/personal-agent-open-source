"""DAL-027: routing handoff boundary — `DAL-T-ROUTING-CONTRACT-001`.

All thirteen frozen variants replay an untrusted primary-failure report through
the pure `consume_handoff` decision.  The handoff is a decision layer only: a
classifier drift or an unknown slot request fails closed (`blocked`), a
fallback-eligible provider/model-level failure is allowed (`fallback_allowed`)
with the four-field work state handed on verbatim, an ineligible failure is
denied (`fallback_denied`), and an unconfigured fallback is refused without
inventing a route (`no_fallback_route`).  Each non-blocked outcome leaves the
feature in `coding` with zero writes; each block moves it to `needs_human` with
the seven-write block set and an `APPLIED` receipt.  Blocking is the operation
succeeding at its job.

The pure decision itself is offline replayable, which is what this file
exercises.  The actual fallback re-invocation of the coder is a controller-side
composition and is deliberately out of scope here.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import routing_contract as routing_contract_policy

from tests.dal import routing_contract_executor
from tests.dal.routing_contract_executor import execute_routing_fixture
from tests.dal.routing_contract_loader import RoutingContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-ROUTING-CONTRACT-001"

FROZEN_VARIANTS: set[str] = {
    "primary_transient",
    "primary_usage_limit",
    "primary_auth",
    "primary_contract_failure",
    "primary_budget_limit",
    "primary_policy_failure",
    "primary_task_failure",
    "primary_cancelled",
    "classifier_modified",
    "classifier_replaced",
    "classifier_digest_drift",
    "unknown_slot_request",
    "fallback_unconfigured",
}


@pytest.fixture(scope="module")
def contracts() -> RoutingContracts:
    return RoutingContracts()


def test_variant_set_is_closed(contracts: RoutingContracts) -> None:
    """Every frozen variant is consumed; nothing frozen is silently skipped."""
    frozen = {v.variant_id for v in contracts.variants(TEST_ID)}
    assert frozen == FROZEN_VARIANTS, (
        f"{TEST_ID} variant set drifted: {sorted(frozen ^ FROZEN_VARIANTS)}"
    )


def test_every_scenario_variant_matches_its_oracle(contracts: RoutingContracts) -> None:
    """Replay every frozen variant and report all divergences at once."""
    variants = list(contracts.variants(TEST_ID))
    assert variants, f"no variants for {TEST_ID}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        execution = execute_routing_fixture(variant.fixture, probe=probe)
        oracle = variant.oracle
        result = compare(execution.trace, oracle)
        divergences = list(result.mismatches)
        if set(execution.trace.declared_write_set) != set(execution.trace.write_set):
            divergences.append(
                "declared write set differs from observed write set: "
                f"{execution.trace.declared_write_set!r} != {execution.trace.write_set!r}"
            )
        if execution.result_status != oracle.get("expected_result_status"):
            divergences.append(
                "result_status: expected "
                f"{oracle.get('expected_result_status')!r}, got {execution.result_status!r}"
            )
        if execution.failure_class != oracle.get("expected_failure_class"):
            divergences.append(
                "failure_class: expected "
                f"{oracle.get('expected_failure_class')!r}, got {execution.failure_class!r}"
            )
        if execution.handoff_state != oracle.get("expected_handoff_state"):
            divergences.append(
                "handoff_state: expected "
                f"{oracle.get('expected_handoff_state')!r}, got {execution.handoff_state!r}"
            )
        if probe.observed:
            divergences.append(f"unexpected side effects: {sorted(probe.observed)!r}")
        if divergences:
            failures.append(f"{variant.variant_id}: " + "; ".join(divergences))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def _command(contracts: RoutingContracts, variant_id: str) -> dict:
    variant = next(
        v for v in contracts.variants(TEST_ID) if v.variant_id == variant_id
    )
    return deepcopy(variant.fixture["operation_sequence"][0])


def test_malformed_envelopes_are_stable_invalid_arguments(
    contracts: RoutingContracts,
) -> None:
    """Malformed trusted shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _command(contracts, "primary_transient")
    command["input"]["action_sequence"] = []
    cases.append(("empty action sequence", command))

    command = _command(contracts, "primary_transient")
    command["input"]["action_sequence"].insert(0, "malformed-step")
    cases.append(("non-object action step", command))

    command = _command(contracts, "primary_transient")
    command["input"]["injected_results"] = None
    cases.append(("non-object injected results", command))

    command = _command(contracts, "primary_transient")
    command["input"]["injected_results"]["observed_classifier"] = None
    cases.append(("non-object observed classifier", command))

    command = _command(contracts, "primary_transient")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "primary_transient")
    command["input"]["authoritative_facts"]["requested_slot"] = 42
    cases.append(("non-string requested slot", command))

    command = _command(contracts, "primary_transient")
    command["input"]["authoritative_facts"]["classifier_digest"] = "zz"
    cases.append(("malformed classifier digest", command))

    command = _command(contracts, "primary_transient")
    del command["input"]["authoritative_facts"]["routing_snapshot"]["classifier"]
    cases.append(("snapshot missing classifier", command))

    command = _command(contracts, "primary_transient")
    command["input"]["authoritative_facts"]["routing_snapshot"]["primary"]["extra"] = "x"
    cases.append(("snapshot slot carries unknown field", command))

    command = _command(contracts, "primary_transient")
    command["input"]["authoritative_facts"]["work_state"]["base_sha"] = "zz"
    cases.append(("malformed work_state base_sha", command))

    command = _command(contracts, "primary_transient")
    del command["input"]["authoritative_facts"]["work_state"]["tests_receipt"]
    cases.append(("work_state missing a field", command))

    command = _command(contracts, "primary_transient")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            routing_contract_policy.consume_handoff(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_wrong_actor_and_evidence_are_their_own_refusals(
    contracts: RoutingContracts,
) -> None:
    """Actor and evidence mismatches carry their named refusal codes."""
    command = _command(contracts, "primary_transient")
    command["operation_spec_id"] = "OP-ROUTING-CONTRACT-001"
    command["actor_type"] = "worker"
    with pytest.raises(DalError) as raised:
        routing_contract_policy.consume_handoff(command)
    assert raised.value.code is DalErrorCode.ACTOR_NOT_ALLOWED

    command = _command(contracts, "primary_transient")
    command["evidence_source_type"] = "policy-engine"
    with pytest.raises(DalError) as raised:
        routing_contract_policy.consume_handoff(command)
    assert raised.value.code is DalErrorCode.SCOPE_DENIED


def test_untrusted_values_never_crash_the_boundary(
    contracts: RoutingContracts,
) -> None:
    """Hostile injected values classify, never raise a Python exception."""
    cases: list[tuple[str, dict]] = []

    cases.append(
        ("non-string observed provider", {"observed_classifier": {
            "provider": 42, "model": "GLM/glm-4.5-air",
            "digest_pre": "b" * 64, "digest_post": "b" * 64}})
    )
    cases.append(
        ("null observed model", {"observed_classifier": {
            "provider": "glm", "model": None,
            "digest_pre": "b" * 64, "digest_post": "b" * 64}})
    )
    cases.append(
        ("integer digest pre", {"observed_classifier": {
            "provider": "glm", "model": "GLM/glm-4.5-air",
            "digest_pre": 123, "digest_post": "b" * 64}})
    )
    cases.append(
        ("non-string primary failure class", {"primary_failure_class": {"x": 1}})
    )

    for label, overrides in cases:
        command = _command(contracts, "primary_transient")
        for key, value in overrides.items():
            command["input"]["injected_results"][key] = value
        outcome = routing_contract_policy.consume_handoff(command)
        assert outcome.final_state in ("coding", "needs_human"), label
        assert outcome.receipt.code.value == "APPLIED", label


def test_allowed_handoff_preserves_work_state(contracts: RoutingContracts) -> None:
    """A fallback-eligible failure hands the four-field work state on verbatim."""
    command = _command(contracts, "primary_transient")
    work_state = command["input"]["authoritative_facts"]["work_state"]
    outcome = routing_contract_policy.consume_handoff(command)
    assert outcome.result_status == "fallback_allowed"
    assert outcome.failure_class is None
    assert outcome.final_state == "coding"
    assert outcome.declared_write_set == ()
    assert outcome.event_trace == ()
    assert outcome.handoff_state == work_state
    assert outcome.handoff_state is not work_state  # a copy, not an alias


def test_denied_and_unconfigured_handoffs_write_nothing(
    contracts: RoutingContracts,
) -> None:
    """An ineligible failure or an unconfigured fallback closes with zero writes."""
    for variant_id in ("primary_task_failure", "primary_budget_limit", "fallback_unconfigured"):
        command = _command(contracts, variant_id)
        outcome = routing_contract_policy.consume_handoff(command)
        assert outcome.failure_class is None, variant_id
        assert outcome.final_state == "coding", variant_id
        assert outcome.declared_write_set == (), variant_id
        assert outcome.event_trace == (), variant_id
        assert outcome.handoff_state is None, variant_id


def test_executor_observes_a_forbidden_boundary_crossing(
    contracts: RoutingContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must fail if the handler performs even a file read."""

    def impure_handler(_command: dict):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        routing_contract_executor, "consume_handoff", impure_handler
    )
    fixture = next(
        v.fixture for v in contracts.variants(TEST_ID) if v.variant_id == "primary_transient"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_routing_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_routing_contract_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(routing_contract_policy.__file__)
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
