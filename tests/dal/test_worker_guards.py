"""DAL-017/018: the worker-isolation guard families — G2 (21 variants).

Four pure guards protect the Home Mac Worker's boundary; each reports a
`POLICY_FAILURE` block reason that the trusted resolver carries into the
`BLK-POLICY--coding` transition (`block_feature` → `needs_human`):

- `DAL-T-PATH-001` (4) — the repository allowlist + worktree isolation. An
  absolute path, a `..` traversal, a repository mismatch, or a symlink swap is
  an escape;
- `DAL-T-NET-001` (4) — the network deny. An outbound host outside the empty
  allowlist, or an inbound bind while listeners are forbidden, is a denial;
- `DAL-T-CRED-001` (7) — the credential-read boundary. A child probing any
  closed surface (env/fd/keychain/log/parent_process/proxy/dependency_hook) and
  observing the canary is a breach;
- `DAL-T-SECRET-OUTPUT-001` (6) — the secret-output isolation. A canary matched
  on any output channel (stdout/stderr/patch/artifact/env/worker_exception) is a
  leak under the drop-and-revoke policy.

Each G2 variant is hash-bound to its frozen fixture and oracle, replayed against
the real pure policy + deterministic engine, and judged by
`oracle_comparator.compare`.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import cred as cred_policy
from personal_agent_dal.machine import net as net_policy
from personal_agent_dal.machine import path as path_policy
from personal_agent_dal.machine import secret_output as secret_output_policy

from tests.dal.contract_loader import FrozenContracts
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe
from tests.dal.state_machine_executor import execute_state_machine_fixture


PATH_TEST_ID = "DAL-T-PATH-001"
NET_TEST_ID = "DAL-T-NET-001"
CRED_TEST_ID = "DAL-T-CRED-001"
SECRET_OUTPUT_TEST_ID = "DAL-T-SECRET-OUTPUT-001"

#: The exact G2 scenario sets each family must expose. A frozen variant outside
#: its set is a harness gap, not a skipped test — the enumeration is closed.
G2_SCENARIOS: dict[str, set[str]] = {
    PATH_TEST_ID: {"absolute", "dotdot", "nested_repo", "symlink_swap"},
    NET_TEST_ID: {"finance", "inbound_listener", "lan", "personal_agent_prod"},
    CRED_TEST_ID: {
        "env--g2",
        "fd--g2",
        "keychain--g2",
        "log--g2",
        "parent_process--g2",
        "proxy--g2",
        "dependency_hook",
    },
    SECRET_OUTPUT_TEST_ID: {
        "stdout",
        "stderr",
        "patch",
        "artifact",
        "env",
        "synthetic_exception",
    },
}

ALL_TEST_IDS = (PATH_TEST_ID, NET_TEST_ID, CRED_TEST_ID, SECRET_OUTPUT_TEST_ID)

#: The pure policy behind each family, keyed for the malformed/dependency tests.
POLICIES = {
    PATH_TEST_ID: path_policy,
    NET_TEST_ID: net_policy,
    CRED_TEST_ID: cred_policy,
    SECRET_OUTPUT_TEST_ID: secret_output_policy,
}

#: The public pure function each family exposes, keyed for the malformed tests.
EVALUATORS = {
    PATH_TEST_ID: path_policy.evaluate_path,
    NET_TEST_ID: net_policy.evaluate_network_request,
    CRED_TEST_ID: cred_policy.evaluate_credential_boundary,
    SECRET_OUTPUT_TEST_ID: secret_output_policy.evaluate_secret_output,
}


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def _command(contracts: FrozenContracts, test_id: str, variant_id: str) -> dict:
    variant = next(
        v
        for v in contracts.variants(test_id)
        if v.run_gate == "G2" and v.variant_id == variant_id
    )
    return deepcopy(variant.fixture.body["operation_sequence"][0])


def test_g2_scenario_sets_are_closed(contracts: FrozenContracts) -> None:
    """Every G2 variant is consumed; nothing frozen is silently skipped."""
    for test_id, expected in G2_SCENARIOS.items():
        g2 = {
            v.variant_id for v in contracts.variants(test_id) if v.run_gate == "G2"
        }
        assert g2 == expected, (
            f"{test_id} G2 scenario set drifted: {sorted(g2 ^ expected)}"
        )


@pytest.mark.parametrize("test_id", ALL_TEST_IDS)
def test_every_guard_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every guard variant and report all divergences at once."""
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


def test_malformed_inputs_fail_closed(contracts: FrozenContracts) -> None:
    """Every guard refuses a malformed shape as DalError, never a Python error."""
    cases: list[tuple[str, str, dict]] = []

    # PATH-001
    command = _command(contracts, PATH_TEST_ID, "absolute")
    command["input"]["authoritative_facts"] = None
    cases.append((PATH_TEST_ID, "non-object facts", command))

    command = _command(contracts, PATH_TEST_ID, "absolute")
    command["input"]["authoritative_facts"].pop("requested_path")
    cases.append((PATH_TEST_ID, "missing requested path", command))

    command = _command(contracts, PATH_TEST_ID, "absolute")
    command["input"]["target"]["state"] = "intake"
    cases.append((PATH_TEST_ID, "non-coding target state", command))

    # NET-001
    command = _command(contracts, NET_TEST_ID, "finance")
    command["input"]["authoritative_facts"] = None
    cases.append((NET_TEST_ID, "non-object facts", command))

    command = _command(contracts, NET_TEST_ID, "finance")
    command["input"]["action_sequence"] = []
    cases.append((NET_TEST_ID, "empty action sequence", command))

    command = _command(contracts, NET_TEST_ID, "inbound_listener")
    command["input"]["action_sequence"][0]["request"]["direction"] = "sideways"
    cases.append((NET_TEST_ID, "unknown request direction", command))

    # CRED-001
    command = _command(contracts, CRED_TEST_ID, "env--g2")
    command["input"]["authoritative_facts"] = None
    cases.append((CRED_TEST_ID, "non-object facts", command))

    command = _command(contracts, CRED_TEST_ID, "env--g2")
    command["input"]["authoritative_facts"]["surface_probed"] = ""
    cases.append((CRED_TEST_ID, "empty surface probed", command))

    command = _command(contracts, CRED_TEST_ID, "env--g2")
    command["input"]["injected_results"] = []
    cases.append((CRED_TEST_ID, "empty injected results", command))

    # SECRET-OUTPUT-001
    command = _command(contracts, SECRET_OUTPUT_TEST_ID, "stdout")
    command["input"]["authoritative_facts"] = None
    cases.append((SECRET_OUTPUT_TEST_ID, "non-object facts", command))

    command = _command(contracts, SECRET_OUTPUT_TEST_ID, "stdout")
    command["input"]["authoritative_facts"]["redaction_policy"] = "redact_only"
    cases.append((SECRET_OUTPUT_TEST_ID, "unknown redaction policy", command))

    command = _command(contracts, SECRET_OUTPUT_TEST_ID, "stdout")
    command["input"]["target"]["state"] = "intake"
    cases.append((SECRET_OUTPUT_TEST_ID, "non-coding target state", command))

    for test_id, label, malformed in cases:
        with pytest.raises(DalError) as raised:
            EVALUATORS[test_id](malformed)
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, (test_id, label)


def test_pure_policy_dependency_surfaces_are_closed() -> None:
    """The pure guards cannot acquire an unguarded I/O dependency."""
    expected_imports = {
        "__future__",
        "dataclasses",
        "typing",
        "personal_agent_dal.errors",
    }
    for test_id, module in POLICIES.items():
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert imports == expected_imports, test_id
