"""DAL-026: coder-response contract boundary — `DAL-T-CODER-CONTRACT-001`.

All sixteen frozen variants replay an untrusted coder stream through the pure
`consume_coder_stream` decision.  Unlike the single-shot provider contract, the
coder's happy path is multi-turn and multi-tool, so `tool_call` and free prose
are legal here; what fails closed is the *run integrity* (classifier digest,
endpoint, redaction, scope), the *budget*, the *transport*, and the *stream
shape* (empty, malformed, half-stream, multi-final, drifted context, empty
diff).  Each block lands on its frozen `(result_status, failure_class)` pair and
the feature transition it implies — `succeeded`/`cancelled` leave the feature in
`coding` with zero writes; every failure moves it to its block state with the
seven-write block set and an `APPLIED` receipt.  Blocking is the operation
succeeding at its job.

The pure decision itself is offline replayable, which is what this file
exercises.  The real `claude -p` subprocess launch is DAL-006 §9 P2/P3 work and
is deliberately out of scope here.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import coder_contract as coder_contract_policy

from tests.dal import coder_adapter_executor
from tests.dal.coder_adapter_contract_loader import CoderContracts
from tests.dal.coder_adapter_executor import execute_coder_adapter_fixture
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-CODER-CONTRACT-001"

FROZEN_VARIANTS: set[str] = {
    "happy_multi_tool",
    "empty",
    "malformed_args",
    "half_stream",
    "multi_final",
    "turn_ceiling",
    "context_drift",
    "cancel",
    "quota",
    "transient",
    "recovery",
    "real_auth",
    "tampered_host",
    "classifier_drift",
    "out_of_scope_patch",
    "done_diff_empty",
}


@pytest.fixture(scope="module")
def contracts() -> CoderContracts:
    return CoderContracts()


def test_variant_set_is_closed(contracts: CoderContracts) -> None:
    """Every frozen variant is consumed; nothing frozen is silently skipped."""
    frozen = {v.variant_id for v in contracts.variants(TEST_ID)}
    assert frozen == FROZEN_VARIANTS, (
        f"{TEST_ID} variant set drifted: {sorted(frozen ^ FROZEN_VARIANTS)}"
    )


def test_every_scenario_variant_matches_its_oracle(contracts: CoderContracts) -> None:
    """Replay every frozen variant and report all divergences at once."""
    variants = list(contracts.variants(TEST_ID))
    assert variants, f"no variants for {TEST_ID}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        execution = execute_coder_adapter_fixture(variant.fixture, probe=probe)
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
        if probe.observed:
            divergences.append(f"unexpected side effects: {sorted(probe.observed)!r}")
        if divergences:
            failures.append(f"{variant.variant_id}: " + "; ".join(divergences))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def _command(contracts: CoderContracts, variant_id: str) -> dict:
    variant = next(
        v for v in contracts.variants(TEST_ID) if v.variant_id == variant_id
    )
    return deepcopy(variant.fixture["operation_sequence"][0])


def test_malformed_envelopes_are_stable_invalid_arguments(
    contracts: CoderContracts,
) -> None:
    """Malformed trusted shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _command(contracts, "empty")
    command["input"]["action_sequence"] = []
    cases.append(("empty action sequence", command))

    command = _command(contracts, "empty")
    command["input"]["action_sequence"].insert(0, "malformed-step")
    cases.append(("non-object action step", command))

    command = _command(contracts, "empty")
    command["input"]["injected_results"] = None
    cases.append(("non-object injected results", command))

    command = _command(contracts, "empty")
    command["input"]["injected_results"]["stream"] = None
    cases.append(("non-list stream", command))

    command = _command(contracts, "empty")
    command["input"]["injected_results"]["transport"] = None
    cases.append(("non-object transport", command))

    command = _command(contracts, "empty")
    command["input"]["injected_results"]["budget"] = None
    cases.append(("non-object budget", command))

    command = _command(contracts, "empty")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "empty")
    command["input"]["authoritative_facts"]["max_turns"] = True
    cases.append(("boolean max_turns", command))

    command = _command(contracts, "empty")
    command["input"]["authoritative_facts"]["base_sha"] = "zz"
    cases.append(("malformed base_sha", command))

    command = _command(contracts, "empty")
    command["input"]["authoritative_facts"]["allowed_tools"] = []
    cases.append(("empty allowed_tools", command))

    command = _command(contracts, "empty")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            coder_contract_policy.consume_coder_stream(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_wrong_actor_and_evidence_are_their_own_refusals(
    contracts: CoderContracts,
) -> None:
    """Actor and evidence mismatches carry their named refusal codes."""
    command = _command(contracts, "empty")
    command["operation_spec_id"] = "OP-CODER-CONTRACT-001"
    command["actor_type"] = "worker"
    with pytest.raises(DalError) as raised:
        coder_contract_policy.consume_coder_stream(command)
    assert raised.value.code is DalErrorCode.ACTOR_NOT_ALLOWED

    command = _command(contracts, "empty")
    command["evidence_source_type"] = "policy-engine"
    with pytest.raises(DalError) as raised:
        coder_contract_policy.consume_coder_stream(command)
    assert raised.value.code is DalErrorCode.SCOPE_DENIED


def test_untrusted_stream_never_crashes_the_boundary(
    contracts: CoderContracts,
) -> None:
    """Hostile stream shapes are contract failures, never Python exceptions."""
    cases: list[tuple[str, list]] = []

    cases.append(("non-object event", ["not-an-event"]))
    cases.append(("unknown event type", [{"type": "surprise"}]))
    cases.append(("final missing mandatory fields", [{"type": "final", "content": "x"}]))
    cases.append(
        (
            "tool call with both argument forms",
            [
                {
                    "type": "tool_call",
                    "name": "read",
                    "turn": 1,
                    "arguments": {"path": "a"},
                    "arguments_json": '{"path": "a"}',
                }
            ],
        )
    )
    cases.append(
        (
            "tool call with no argument form",
            [{"type": "tool_call", "name": "read", "turn": 1}],
        )
    )
    cases.append(("integer text content", [{"type": "text", "content": 42}]))
    cases.append(
        (
            "final with malformed base_sha",
            [
                {
                    "type": "final",
                    "content": "x",
                    "base_sha": "zz",
                    "context_envelope_sha256": "a" * 64,
                    "changed_files": ["src/x.py"],
                }
            ],
        )
    )

    for label, hostile in cases:
        command = _command(contracts, "empty")
        command["input"]["injected_results"]["stream"] = hostile
        outcome = coder_contract_policy.consume_coder_stream(command)
        assert outcome.final_state == "needs_human", label
        assert outcome.final_reason_code == "PROVIDER_CONTRACT_FAILURE", label
        assert outcome.receipt.code.value == "APPLIED", label


def test_conforming_stream_does_not_block(contracts: CoderContracts) -> None:
    """The one shape that must NOT block: a bound, non-empty final diff."""
    command = _command(contracts, "empty")
    facts = command["input"]["authoritative_facts"]
    command["input"]["injected_results"]["stream"] = [
        {
            "type": "final",
            "content": "dal.patch-artifact/1.0:ref",
            "base_sha": facts["base_sha"],
            "context_envelope_sha256": facts["requested_context_envelope_sha256"],
            "changed_files": ["src/x.py"],
        }
    ]
    outcome = coder_contract_policy.consume_coder_stream(command)
    assert outcome.result_status == "succeeded"
    assert outcome.failure_class is None
    assert outcome.final_state == "coding"
    assert outcome.declared_write_set == ()
    assert outcome.event_trace == ()
    assert outcome.receipt.code.value == "APPLIED"


def test_cancelled_stream_closes_without_a_write(contracts: CoderContracts) -> None:
    """A lone cancel marker is a clean zero-write close, not a failure."""
    command = _command(contracts, "empty")
    command["input"]["injected_results"]["stream"] = [{"type": "cancelled"}]
    outcome = coder_contract_policy.consume_coder_stream(command)
    assert outcome.result_status == "cancelled"
    assert outcome.failure_class is None
    assert outcome.final_state == "coding"
    assert outcome.declared_write_set == ()
    assert outcome.event_trace == ()


def test_executor_observes_a_forbidden_boundary_crossing(
    contracts: CoderContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must fail if the handler performs even a file read."""

    def impure_handler(_command: dict):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        coder_adapter_executor, "consume_coder_stream", impure_handler
    )
    fixture = next(
        v.fixture for v in contracts.variants(TEST_ID) if v.variant_id == "empty"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_coder_adapter_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_coder_contract_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(coder_contract_policy.__file__)
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
