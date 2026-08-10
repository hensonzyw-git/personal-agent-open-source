"""Executes frozen DAL operation fixtures against real component entry points.

This is the harness half of "tests-first against the frozen contracts": it does
not reimplement the component's policy. It reads a fixture's `dal.operation-
input/1.0` payload, drives the real production entry point (for DAL-007, the
`ConfigLoader`/`ConfigPolicy`), and reduces whatever happened into an
`ExecutionTrace` the oracle comparator can judge.

The separation matters. The production code decides whether the load is in
policy; the executor only records the outcome faithfully. If the component
silently loaded a config it should have refused, the trace records `loaded`
plus an `APPLIED` receipt, and the comparator -- not the executor -- fails it
against the oracle's `POLICY_DENIED` expectation. The executor never turns a
real outcome into the expected one.

Test-only module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from personal_agent_dal.config import ConfigLoader, ConfigPolicy
from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import (
    OPERATION_RECEIPT_SCHEMA,
    OperationReceipt,
    ReceiptCode,
)

from tests.dal.side_effects import SideEffectProbe


@dataclass(frozen=True)
class ReceiptRecord:
    """One emitted receipt, in the shape the oracle asserts against."""

    code: str
    schema_version: str


@dataclass
class ExecutionTrace:
    """What actually happened when a fixture's operation sequence ran.

    Every field defaults to the empty/no-op outcome, so a refused operation
    leaves a trace that can only match an oracle expecting a refusal.
    """

    state_trace: list[str] = field(default_factory=list)
    receipts: list[ReceiptRecord] = field(default_factory=list)
    write_set: list[str] = field(default_factory=list)
    event_trace: list[str] = field(default_factory=list)
    external_effect_trace: list[str] = field(default_factory=list)
    final_state: str = ""
    probe: SideEffectProbe | None = None


class UnsupportedCommandError(RuntimeError):
    """A fixture command the harness has no executor for. Never a silent pass."""


def _receipt_for_error(error: DalError) -> ReceiptRecord:
    code = error.code
    if code is DalErrorCode.CONFIG_POLICY_DENIED:
        return ReceiptRecord(code=ReceiptCode.POLICY_DENIED.value,
                             schema_version=OPERATION_RECEIPT_SCHEMA)
    # Any other refusal is surfaced as its own code so the comparator can
    # distinguish "denied by policy" from "unavailable"; it is never folded
    # into the expected code to force a pass.
    return ReceiptRecord(code=code.value, schema_version=OPERATION_RECEIPT_SCHEMA)


def execute_config_load(
    command: dict[str, Any],
    *,
    loader: ConfigLoader,
    probe: SideEffectProbe,
) -> ExecutionTrace:
    """Run one `load_declared_service_config` operation against the real loader.

    The fixture's `authoritative_facts` are the declared inputs a real config
    load would carry: which module the config belongs to, which names were
    requested, and (for a secret file) its path and OS mode. The executor maps
    each onto the corresponding policy check in the same order a real load
    would apply them, then records the resulting state and receipt.
    """
    facts = command["input"]["authoritative_facts"]
    target = command["input"]["target"]
    trace = ExecutionTrace(probe=probe)

    # The pre-state is the entity's starting point, recorded so the state
    # trace always begins where the fixture said the entity was.
    current_state = target["state"]
    trace.state_trace.append(current_state)

    policy: ConfigPolicy = loader.policy
    try:
        # 1. Module namespace (finance_import lives here).
        policy.check_module(facts["module"])

        # 2. Requested secret names (production_credential lives here).
        for secret_name in facts.get("requested_secret_names", []):
            policy.check_secret_name(secret_name)

        # 3. Requested config names (unknown_config lives here).
        for config_name in facts.get("requested_config_names", []):
            policy.check_config_name(config_name)

        # 4. Secret file mode (insecure_secret_file lives here). The fixture
        #    supplies the mode as metadata; the policy normalises and judges it.
        if "secret_file" in facts:
            policy.check_secret_file_mode(
                facts["secret_file"], facts.get("secret_file_mode")
            )

    except DalError as error:
        # A refused load leaves the entity where it was and writes nothing.
        trace.receipts.append(_receipt_for_error(error))
        trace.final_state = current_state
        trace.state_trace.append(current_state)
        return trace

    # Reaching here means the load was in policy. For the frozen DAL-007
    # variants this path is never expected; if it is reached the trace records
    # an APPLIED receipt and the comparator fails it against POLICY_DENIED.
    trace.receipts.append(
        ReceiptRecord(code=ReceiptCode.APPLIED.value,
                      schema_version=OPERATION_RECEIPT_SCHEMA)
    )
    trace.final_state = "loaded"
    trace.state_trace.append("loaded")
    return trace


#: The commands the DAL-007–013 harness knows how to execute. An operation
#: whose command is absent is a harness gap, surfaced as UnsupportedCommandError
#: rather than silently scored.
def execute_operation_command(
    command: dict[str, Any],
    *,
    probe: SideEffectProbe,
    loader: ConfigLoader | None = None,
) -> ExecutionTrace:
    """Dispatch one `dal.test-operation-command/1.0` to its executor."""
    action_sequence = command["input"]["action_sequence"]
    if len(action_sequence) != 1:
        raise UnsupportedCommandError(
            f"expected exactly one action, got {len(action_sequence)}"
        )
    action = action_sequence[0]["command"]

    if action == "load_declared_service_config":
        if loader is None:
            loader = ConfigLoader()
        return execute_config_load(command, loader=loader, probe=probe)

    raise UnsupportedCommandError(f"no executor for command: {action!r}")


def execute_fixture(
    fixture_body: dict[str, Any],
    *,
    probe: SideEffectProbe,
    loader: ConfigLoader | None = None,
) -> ExecutionTrace:
    """Execute a fixture's full operation sequence and merge the traces.

    The DAL-007 variants each carry exactly one operation command; the loop is
    written for the general sequence shape so later waves with multi-command
    sequences reuse it. The merged trace accumulates states, receipts and
    write sets in order; the final state is the last operation's.
    """
    merged = ExecutionTrace(probe=probe)
    for command in fixture_body["operation_sequence"]:
        trace = execute_operation_command(command, probe=probe, loader=loader)
        merged.state_trace.extend(trace.state_trace)
        merged.receipts.extend(trace.receipts)
        merged.write_set.extend(trace.write_set)
        merged.event_trace.extend(trace.event_trace)
        merged.external_effect_trace.extend(trace.external_effect_trace)
        merged.final_state = trace.final_state
    return merged
