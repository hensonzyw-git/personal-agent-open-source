"""DAL-014/015: GitHub intake + facts adapter — `GH-EVENT`, `GIT-BASE`, INJECTION G2.

The three G2 families of the GitHub slice:

- `DAL-T-GH-EVENT-001` (5) — the webhook/poll intake matrix. A fork, unknown
  repository, unknown sender, edited event or replayed delivery is refused
  `POLICY_DENIED` with zero writes. Pure: executed against `accept_github_intake`
  under `guard_pure_policy`.
- `DAL-T-GIT-BASE-001` (4) — the git mutation preconditions. Base/PR-head drift
  or an index/content conflict blocks the coding feature with `GIT_CONFLICT`
  (`BLK-GIT--coding`). DB-backed: executed against `apply_transition`.
- `DAL-T-INJECTION-001` G2 (4) — the coding-state injection carriers (diff,
  issue, readme, test_failure). A tainted escalation blocks the coding feature
  with `POLICY_FAILURE` (`BLK-POLICY--coding`). DB-backed.

Each G2 variant is hash-bound to its frozen fixture and oracle, replayed against
the real policy + engine, and judged by `oracle_comparator.compare`. The G1
`api_intake` injection variant stays in `test_injection.py`.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.github import git_base as git_base_policy
from personal_agent_dal.github import webhook as webhook_policy

from tests.dal import gh_event_executor
from tests.dal.contract_loader import FrozenContracts
from tests.dal.gh_event_executor import execute_gh_event_fixture
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import ForbiddenSideEffectError, fresh_probe
from tests.dal.state_machine_executor import execute_state_machine_fixture


GH_EVENT_TEST_ID = "DAL-T-GH-EVENT-001"
GIT_BASE_TEST_ID = "DAL-T-GIT-BASE-001"
INJECTION_TEST_ID = "DAL-T-INJECTION-001"

#: The exact G2 scenario sets each family must expose. A frozen variant outside
#: its set is a harness gap, not a skipped test — the enumeration is closed.
G2_SCENARIOS: dict[str, set[str]] = {
    GH_EVENT_TEST_ID: {
        "edited_event",
        "fork",
        "replay_delivery",
        "unknown_repo",
        "unknown_sender",
    },
    GIT_BASE_TEST_ID: {
        "base_drift",
        "content_conflict",
        "index_conflict",
        "pr_head_drift",
    },
    INJECTION_TEST_ID: {"diff", "issue", "readme", "test_failure"},
}

#: Pure refusals run without a database; DB-backed blocks need a real database.
PURE_TEST_IDS = (GH_EVENT_TEST_ID,)
DB_TEST_IDS = (GIT_BASE_TEST_ID, INJECTION_TEST_ID)


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_g2_scenario_sets_are_closed(contracts: FrozenContracts) -> None:
    """Every G2 variant is consumed; nothing frozen is silently skipped."""
    for test_id, expected in G2_SCENARIOS.items():
        g2 = {
            v.variant_id for v in contracts.variants(test_id) if v.run_gate == "G2"
        }
        assert g2 == expected, (
            f"{test_id} G2 scenario set drifted: {sorted(g2 ^ expected)}"
        )


@pytest.mark.parametrize("test_id", PURE_TEST_IDS)
def test_pure_intake_variants_match_their_oracles(
    contracts: FrozenContracts, test_id: str
) -> None:
    """Replay every pure GH-EVENT refusal and report all divergences at once."""
    variants = [
        v for v in contracts.variants(test_id) if v.run_gate == "G2"
    ]
    assert variants, f"no G2 variants for {test_id}"

    failures: list[str] = []
    for variant in variants:
        probe = fresh_probe()
        trace = execute_gh_event_fixture(variant.fixture.body, probe=probe)
        divergences = list(compare(trace, variant.oracle.body).mismatches)
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
        f"{len(failures)} of {len(variants)} {test_id} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


@pytest.mark.parametrize("test_id", DB_TEST_IDS)
def test_db_backed_block_variants_match_their_oracles(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every DB-backed block and report all divergences at once."""
    variants = [
        v for v in contracts.variants(test_id) if v.run_gate == "G2"
    ]
    assert variants, f"no G2 variants for {test_id}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"{test_id}-{index}.db"
        probe = fresh_probe()
        trace = execute_state_machine_fixture(
            variant.fixture.body, database=database, probe=probe
        )
        divergences = list(compare(trace, variant.oracle.body).mismatches)
        if divergences:
            failures.append(f"{variant.variant_id}: " + "; ".join(divergences))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {test_id} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


# ---------------------------------------------------------------------------
# Fail-closed: malformed inputs are stable DalError, never Python exceptions.
# ---------------------------------------------------------------------------


def _gh_event_command(contracts: FrozenContracts) -> dict:
    variant = next(
        v
        for v in contracts.variants(GH_EVENT_TEST_ID)
        if v.run_gate == "G2" and v.variant_id == "unknown_repo"
    )
    return deepcopy(variant.fixture.body["operation_sequence"][0])


def _git_base_command(contracts: FrozenContracts) -> dict:
    variant = next(
        v
        for v in contracts.variants(GIT_BASE_TEST_ID)
        if v.run_gate == "G2" and v.variant_id == "base_drift"
    )
    return deepcopy(variant.fixture.body["operation_sequence"][0])


def test_webhook_malformed_inputs_fail_closed(
    contracts: FrozenContracts,
) -> None:
    """Malformed intake shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _gh_event_command(contracts)
    command["input"]["authoritative_facts"] = None
    cases.append(("non-object facts", command))

    command = _gh_event_command(contracts)
    command["input"]["authoritative_facts"]["envelope"] = None
    cases.append(("non-object envelope", command))

    command = _gh_event_command(contracts)
    command["input"]["injected_results"] = None
    cases.append(("non-list signature results", command))

    command = _gh_event_command(contracts)
    command["input"]["target"]["state"] = "coding"
    cases.append(("non-intake target state", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            webhook_policy.accept_github_intake(malformed)
        assert raised.value.code in (
            DalErrorCode.INVALID_ARGUMENT,
            DalErrorCode.SCOPE_DENIED,
        ), label


def test_git_base_malformed_inputs_fail_closed(
    contracts: FrozenContracts,
) -> None:
    """Malformed git read-back shapes fail as DalError, never Python exceptions."""
    cases: list[tuple[str, dict]] = []

    command = _git_base_command(contracts)
    command["input"]["authoritative_facts"]["index_clean"] = "yes"
    cases.append(("non-boolean index_clean", command))

    command = _git_base_command(contracts)
    command["input"]["injected_results"] = []
    cases.append(("empty read-back", command))

    command = _git_base_command(contracts)
    command["input"]["target"]["state"] = "intake"
    cases.append(("non-coding target state", command))

    for label, malformed in cases:
        with pytest.raises(DalError) as raised:
            git_base_policy.verify_git_mutation_preconditions(malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


def test_fail_closed_read_back_still_reports_conflict(
    contracts: FrozenContracts,
) -> None:
    """An incomplete read-back can never be positively cleared."""
    command = _git_base_command(contracts)
    command["input"]["injected_results"] = [
        {"source": "git", "status": "timed_out"}
    ]
    assert git_base_policy.verify_git_mutation_preconditions(command).conflict is True


def test_pure_policy_dependency_surfaces_are_closed() -> None:
    """The pure GitHub policies cannot acquire an unguarded I/O dependency."""
    expected_imports: dict[str, set[str]] = {
        "webhook.py": {
            "__future__",
            "dataclasses",
            "typing",
            "personal_agent_dal.errors",
            "personal_agent_dal.receipt",
        },
        "git_base.py": {
            "__future__",
            "dataclasses",
            "typing",
            "personal_agent_dal.errors",
        },
    }
    modules = {
        "webhook.py": webhook_policy,
        "git_base.py": git_base_policy,
    }
    for name, module in modules.items():
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert imports == expected_imports[name], name


def test_intake_executor_observes_a_forbidden_boundary_crossing(
    contracts: FrozenContracts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pure executor must fail if the handler performs even a file read."""

    def impure_handler(_command: dict):
        with open(__file__, encoding="utf-8"):
            raise AssertionError("guard failed to block file access")

    monkeypatch.setattr(
        gh_event_executor, "accept_github_intake", impure_handler
    )
    fixture = next(
        v.fixture.body
        for v in contracts.variants(GH_EVENT_TEST_ID)
        if v.run_gate == "G2" and v.variant_id == "unknown_repo"
    )
    probe = fresh_probe()
    with pytest.raises(ForbiddenSideEffectError, match="filesystem_write"):
        execute_gh_event_fixture(fixture, probe=probe)
    assert probe.observed == frozenset({"filesystem_write"})
