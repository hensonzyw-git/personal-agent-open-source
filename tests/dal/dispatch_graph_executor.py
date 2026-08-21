"""Executor for `DAL-T-GRAPH-001` dispatch fixture bodies.

Replays the pure `dispatch_decision` under the side-effect guard and maps the
frozen `GraphDispatchEvaluation` onto the generic `ExecutionTrace`. Because the
dispatch graph has no `operation_sequence` — its fixture is the pre-state, the
controller facts, the seam, the injected provider stream and the attempted
command — this executor reads those fields directly rather than dispatching an
operation command.

The dispatch decision's `expected_dispatch` and `coverage_ref` are not part of
the generic trace, so the executor returns a `DispatchExecution` that carries
them alongside the trace; the test judges them against the oracle's
`expected_dispatch` / `coverage_ref` fields.

Test-only module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_agent_dal.machine.dispatch_graph import dispatch_decision

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe, guard_pure_policy


@dataclass(frozen=True)
class DispatchExecution:
    """The trace plus the dispatch-only fields the generic trace cannot carry."""

    trace: ExecutionTrace
    dispatch: dict[str, Any]
    coverage_ref: str | None


def execute_dispatch_graph_fixture(
    fixture_body: dict[str, Any], *, probe: SideEffectProbe
) -> DispatchExecution:
    pre_state = fixture_body["pre_state"]
    with guard_pure_policy(probe):
        outcome = dispatch_decision(
            state=pre_state["state"],
            entity_type=pre_state["entity_type"],
            facts=fixture_body["controller_facts"],
            seam=fixture_body["seam"],
            stream=fixture_body["injected_provider_stream"],
            attempted_resulting_command=fixture_body["attempted_resulting_command"],
            provider_attempted=fixture_body["provider_attempted"],
        )

    transition = outcome.transition
    trace = ExecutionTrace(probe=probe)
    trace.state_trace = list(transition.state_trace)
    trace.final_state = transition.final_state
    trace.final_entity_type = transition.final_entity_type
    trace.final_reason_code = transition.final_reason_code
    trace.final_reason_owner = transition.final_reason_owner
    trace.declared_write_set = list(transition.allowed_write_set)
    #: For this pure decision the declared set IS the write set: the guard
    #: proves the handler crossed no file/DB/process/network boundary, and the
    #: transition it decided is exactly what it declared.
    trace.write_set = list(transition.allowed_write_set)
    trace.event_trace = list(transition.event_trace)
    trace.external_effect_trace = []
    if transition.receipt is not None:
        trace.receipts.append(
            ReceiptRecord(
                code=transition.receipt.code.value,
                schema_version=transition.receipt.schema_version,
            )
        )

    return DispatchExecution(
        trace=trace,
        dispatch=outcome.dispatch.to_dict(),
        coverage_ref=transition.coverage_ref,
    )
