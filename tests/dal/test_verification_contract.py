"""DAL-029: deterministic verification boundary — `DAL-T-VERIFICATION-CONTRACT-001`.

All eleven frozen variants replay an untrusted verification report through the
pure `consume_verification` decision.  The verifier is the deterministic test
stage: a conforming run whose four check stages all exit 0 reports `succeeded`,
moves the feature `verifying -> verified`, and advances `last_verified_sha` to
`base_sha`; a failed check stage reports `blocked` (`task_failure`) and moves it
to `blocked_test`, preserving `last_verified_sha`; a swapped command reports
`failed` (`policy_failure`); a drifted diff or a malformed stage set reports
`failed` (`contract_failure`).  Every outcome carries an `APPLIED` transition
receipt plus a check receipt whose `report_hash` binds `base_sha` + `diff_sha` +
the observed commands and exit codes.

The pure decision itself is offline replayable, which is what this file
exercises.  The actual `git diff` capture and `execute_toolchain` run belong to
`worker/verification.py` and are deliberately out of scope here.
"""

from __future__ import annotations

import ast
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import verification_contract as verification_contract_policy

from tests.dal import verification_contract_executor
from tests.dal.verification_contract_executor import execute_verification_fixture
from tests.dal.verification_contract_loader import VerificationContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe


TEST_ID = "DAL-T-VERIFICATION-CONTRACT-001"
CONTRACT_VERSION = "dal.verification-report/1.0"

FROZEN_VARIANTS: set[str] = {
    "all_pass",
    "diff_empty",
    "diff_hash_drift",
    "diff_base_mismatch",
    "format_fail",
    "lint_fail",
    "build_fail",
    "test_fail",
    "command_not_in_registry",
    "missing_stage",
    "fail_preserves_last_verified_sha",
}


@pytest.fixture(scope="module")
def contracts() -> VerificationContracts:
    return VerificationContracts()


def test_variant_set_is_closed(contracts: VerificationContracts) -> None:
    """Every frozen variant is consumed; nothing frozen is silently skipped."""
    frozen = {v.variant_id for v in contracts.variants(TEST_ID)}
    assert frozen == FROZEN_VARIANTS, (
        f"{TEST_ID} variant set drifted: {sorted(frozen ^ FROZEN_VARIANTS)}"
    )


def _report_hash_from_fixture(fixture: dict) -> str:
    """Independent re-derivation of the report hash, so the classifier's binding
    is proven against a third computation (builder + classifier + this)."""
    facts = fixture["operation_sequence"][0]["input"]["authoritative_facts"]
    injected = fixture["operation_sequence"][0]["input"]["injected_results"]
    stage_results = injected["stage_results"]
    commands: dict = {}
    exit_codes: dict = {}
    for stage in sorted(key for key in stage_results if isinstance(key, str)):
        observed = stage_results[stage]
        if isinstance(observed, dict):
            commands[stage] = observed.get("command")
            exit_codes[stage] = observed.get("exit_code")
    body = {
        "schema_version": CONTRACT_VERSION,
        "base_sha": facts["base_sha"],
        "diff_sha": injected["diff_sha"],
        "commands": commands,
        "exit_codes": exit_codes,
    }
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def test_every_scenario_variant_matches_its_oracle(
    contracts: VerificationContracts,
) -> None:
    """Replay every frozen variant and report all divergences at once."""
    variants = list(contracts.variants(TEST_ID))
    assert variants, f"no variants for {TEST_ID}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        execution = execute_verification_fixture(variant.fixture, probe=probe)
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
        if execution.last_verified_sha != oracle.get("expected_last_verified_sha"):
            divergences.append(
                "last_verified_sha: expected "
                f"{oracle.get('expected_last_verified_sha')!r}, got {execution.last_verified_sha!r}"
            )
        if execution.report_hash != oracle.get("expected_report_hash"):
            divergences.append(
                "report_hash: expected "
                f"{oracle.get('expected_report_hash')!r}, got {execution.report_hash!r}"
            )
        if probe.observed:
            divergences.append(f"unexpected side effects: {sorted(probe.observed)!r}")
        if divergences:
            failures.append(f"{variant.variant_id}: " + "; ".join(divergences))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {TEST_ID} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def test_report_hash_binds_commands_and_exit_codes(
    contracts: VerificationContracts,
) -> None:
    """The classifier's `report_hash` is a real JCS digest of the report body,
    recomputed independently per variant — not a shared frozen constant."""
    for variant in contracts.variants(TEST_ID):
        probe = fresh_probe()
        execution = execute_verification_fixture(variant.fixture, probe=probe)
        assert execution.report_hash == _report_hash_from_fixture(variant.fixture), (
            variant.variant_id
        )
        assert execution.check_receipt.code.value == "APPLIED", variant.variant_id
        assert execution.check_receipt.schema_version == CONTRACT_VERSION, variant.variant_id


def _command(contracts: VerificationContracts, variant_id: str) -> dict:
    variant = next(
        v for v in contracts.variants(TEST_ID) if v.variant_id == variant_id
    )
    return deepcopy(variant.fixture["operation_sequence"][0])


def test_malformed_envelopes_are_stable_invalid_arguments(
    contracts: VerificationContracts,
) -> None:
    """Malformed trusted shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _command(contracts, "all_pass")
    command["input"]["action_sequence"] = []
    cases.append(("empty action sequence", command))

    command = _command(contracts, "all_pass")
    command["input"]["action_sequence"].insert(0, "malformed-step")
    cases.append(("non-object action step", command))

    command = _command(contracts, "all_pass")
    command["input"]["injected_results"] = None
    cases.append(("non-object injected results", command))

    command = _command(contracts, "all_pass")
    command["input"]["injected_results"]["diff"] = None
    cases.append(("non-string diff", command))

    command = _command(contracts, "all_pass")
    command["input"]["injected_results"]["stage_results"] = None
    cases.append(("non-object stage results", command))

    command = _command(contracts, "all_pass")
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _command(contracts, "all_pass")
    command["input"]["authoritative_facts"]["base_sha"] = "zz"
    cases.append(("malformed base sha", command))

    command = _command(contracts, "all_pass")
    del command["input"]["authoritative_facts"]["registry_commands"]["test"]
    cases.append(("registry missing a stage", command))

    command = _command(contracts, "all_pass")
    command["input"]["authoritative_facts"]["registry_commands"]["format"] = []
    cases.append(("registry stage with empty argv", command))

    command = _command(contracts, "all_pass")
    command["input"]["target"]["version"] = True
    cases.append(("boolean target version", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            verification_contract_policy.consume_verification(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_wrong_actor_and_evidence_are_their_own_refusals(
    contracts: VerificationContracts,
) -> None:
    """Actor and evidence mismatches carry their named refusal codes."""
    command = _command(contracts, "all_pass")
    command["operation_spec_id"] = "OP-VERIFICATION-CONTRACT-001"
    command["actor_type"] = "worker"
    with pytest.raises(DalError) as raised:
        verification_contract_policy.consume_verification(command)
    assert raised.value.code is DalErrorCode.ACTOR_NOT_ALLOWED

    command = _command(contracts, "all_pass")
    command["evidence_source_type"] = "policy-engine"
    with pytest.raises(DalError) as raised:
        verification_contract_policy.consume_verification(command)
    assert raised.value.code is DalErrorCode.SCOPE_DENIED


def test_untrusted_values_never_crash_the_boundary(
    contracts: VerificationContracts,
) -> None:
    """Hostile stage-result values classify, never raise a Python exception."""
    cases: list[tuple[str, dict]] = [
        ("non-object stage value", {"format": "not-a-dict"}),
        ("non-list stage command", {"format": {"command": 42, "exit_code": 0}}),
        ("non-int stage exit code", {"format": {"command": ["make", "format"], "exit_code": "zero"}}),
        ("non-string stage key", {42: {"command": ["x"], "exit_code": 0}}),
    ]

    for label, overrides in cases:
        command = _command(contracts, "all_pass")
        for key, value in overrides.items():
            command["input"]["injected_results"]["stage_results"][key] = value
        outcome = verification_contract_policy.consume_verification(command)
        assert outcome.final_state in ("verified", "blocked_test", "needs_human"), label
        assert outcome.receipt.code.value == "APPLIED", label


def test_success_advances_last_verified_sha(
    contracts: VerificationContracts,
) -> None:
    """A conforming run moves `verifying -> verified` and advances the SHA."""
    command = _command(contracts, "all_pass")
    outcome = verification_contract_policy.consume_verification(command)
    assert outcome.result_status == "succeeded"
    assert outcome.failure_class is None
    assert outcome.final_state == "verified"
    assert outcome.last_verified_sha == command["input"]["authoritative_facts"]["base_sha"]
    assert outcome.declared_write_set == ("aggregate", "transition_receipt")
    assert outcome.event_trace == ()


def test_failure_preserves_last_verified_sha(
    contracts: VerificationContracts,
) -> None:
    """A failed check stage preserves the prior SHA instead of resetting it."""
    command = _command(contracts, "fail_preserves_last_verified_sha")
    prior = command["input"]["authoritative_facts"]["prior_last_verified_sha"]
    assert prior is not None
    outcome = verification_contract_policy.consume_verification(command)
    assert outcome.result_status == "blocked"
    assert outcome.failure_class == "task_failure"
    assert outcome.final_state == "blocked_test"
    assert outcome.last_verified_sha == prior


def test_executor_observes_a_forbidden_boundary_crossing(
    contracts: VerificationContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must fail if the handler performs even a file read."""

    def impure_handler(_command: dict):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        verification_contract_executor, "consume_verification", impure_handler
    )
    fixture = next(
        v.fixture for v in contracts.variants(TEST_ID) if v.variant_id == "all_pass"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_verification_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})


def test_verification_contract_dependency_surface_is_closed() -> None:
    """The pure policy cannot acquire an unguarded I/O dependency."""
    source_path = Path(verification_contract_policy.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
    assert imports == {
        "__future__",
        "dataclasses",
        "typing",
        "hashlib",
        "json",
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
