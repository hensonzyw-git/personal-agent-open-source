"""DAL-021: provider-response contract boundary — `DAL-T-PROVIDER-CONTRACT-001`.

All eight frozen variants are adversarial: `empty`, `multi_tool`, `prose_tool`,
`malformed_args`, `half_stream`, `multi_final`, `multi_turn`, `context_drift`.
Each replays an untrusted provider stream that violates the §3.4 single-shot
consumption rule in a different way, and each must land on the same frozen
outcome — `coding → needs_human` via `feature.blocked`
(`PROVIDER_CONTRACT_FAILURE`) with the seven-write block set and an `APPLIED`
receipt. Blocking is the operation succeeding at its job.

The fixtures are labelled G4 (live-environment variants) because earning the
G4 *receipt* requires the real Codex adapter — blocked until DAL-006 §6.1 is
revised (File-mode No-Go). The pure decision itself is offline replayable,
which is what this file exercises. No `dal.test-receipt/1.0` PASS is claimed
here; that is §9 item 3's separate, gated deliverable.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import provider_contract as provider_contract_policy

from tests.dal import provider_contract_executor
from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.provider_contract_executor import execute_provider_contract_fixture
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-PROVIDER-CONTRACT-001"

FROZEN_VARIANTS: set[str] = {
    "empty",
    "multi_tool",
    "prose_tool",
    "malformed_args",
    "half_stream",
    "multi_final",
    "multi_turn",
    "context_drift",
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
        trace = execute_provider_contract_fixture(variant.fixture.body, probe=probe)
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

    command = _command(contracts, "empty")
    command["input"]["action_sequence"] = []
    cases.append(("empty action sequence", command))

    command = _command(contracts, "empty")
    command["input"]["action_sequence"].insert(0, "malformed-step")
    cases.append(("non-object action step", command))

    command = _command(contracts, "empty")
    command["input"]["injected_results"] = None
    cases.append(("non-list injected results", command))

    command = _command(contracts, "empty")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "empty")
    command["input"]["authoritative_facts"]["maximum_tool_calls"] = True
    cases.append(("boolean maximum_tool_calls", command))

    command = _command(contracts, "empty")
    command["input"]["authoritative_facts"]["require_single_final"] = "yes"
    cases.append(("non-boolean require_single_final", command))

    command = _command(contracts, "empty")
    command["input"]["authoritative_facts"]["requested_context_envelope_sha256"] = "zz"
    cases.append(("malformed context digest", command))

    command = _command(contracts, "empty")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            provider_contract_policy.consume_provider_stream(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_wrong_actor_and_evidence_are_their_own_refusals(
    contracts: FrozenContracts,
) -> None:
    """Actor and evidence mismatches carry their named refusal codes."""
    command = _command(contracts, "empty")
    command["operation_spec_id"] = "OP-PROVIDER-CONTRACT-001"
    command["actor_type"] = "worker"
    with pytest.raises(DalError) as raised:
        provider_contract_policy.consume_provider_stream(command)
    assert raised.value.code is DalErrorCode.ACTOR_NOT_ALLOWED

    command = _command(contracts, "empty")
    command["evidence_source_type"] = "policy-engine"
    with pytest.raises(DalError) as raised:
        provider_contract_policy.consume_provider_stream(command)
    assert raised.value.code is DalErrorCode.SCOPE_DENIED


def test_untrusted_stream_never_crashes_the_boundary(
    contracts: FrozenContracts,
) -> None:
    """Hostile stream shapes are contract failures, never Python exceptions."""
    cases: list[tuple[str, list]] = []

    cases.append(("non-object event", ["not-an-event"]))
    cases.append(("unknown event type", [{"type": "surprise", "content": "x"}]))
    cases.append(
        ("final with unknown field", [{"type": "final", "content": "x", "extra": 1}])
    )
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
    cases.append(("integer content", [{"type": "final", "content": 42}]))
    cases.append(
        ("context digest not hex", [{"type": "final", "content": "x", "context_envelope_sha256": "g" * 64}])
    )

    for label, hostile in cases:
        command = _command(contracts, "empty")
        command["input"]["injected_results"] = hostile
        outcome = provider_contract_policy.consume_provider_stream(command)
        assert outcome.final_state == "needs_human", label
        assert outcome.final_reason_code == "PROVIDER_CONTRACT_FAILURE", label
        assert outcome.receipt.code.value == "APPLIED", label


def test_conforming_single_shot_stream_does_not_block(
    contracts: FrozenContracts,
) -> None:
    """The one shape that must NOT block: single non-empty final, bound context."""
    command = _command(contracts, "empty")
    requested = command["input"]["authoritative_facts"][
        "requested_context_envelope_sha256"
    ]
    command["input"]["injected_results"] = [
        {"type": "final", "content": "done", "context_envelope_sha256": requested}
    ]
    outcome = provider_contract_policy.consume_provider_stream(command)
    assert outcome.final_state == "coding"
    assert outcome.declared_write_set == ()
    assert outcome.event_trace == ()
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
        provider_contract_executor, "consume_provider_stream", impure_handler
    )
    fixture = next(
        v.fixture.body for v in contracts.variants(TEST_ID) if v.variant_id == "empty"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_provider_contract_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_provider_contract_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(provider_contract_policy.__file__)
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
