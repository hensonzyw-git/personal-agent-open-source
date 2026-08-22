"""Executor for `DAL-T-ROUTING-CONTRACT-001` fixture bodies.

Replays the single `consume_handoff` operation command under the pure policy
guard and maps the handler's frozen result onto the generic `ExecutionTrace`.
The routing classifier's own `(result_status, failure_class)` pair and the
preserved `handoff_state` are not part of the generic trace, so the executor
returns a `RoutingExecution` that carries them alongside the trace; the test
judges them against the oracle's `expected_result_status` / `expected_failure_class`
/ `expected_handoff_state` fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_agent_dal.machine.routing_contract import consume_handoff

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe, guard_pure_policy


@dataclass(frozen=True)
class RoutingExecution:
    """The trace plus the classifier-only fields the generic trace cannot carry."""

    trace: ExecutionTrace
    result_status: str
    failure_class: str | None
    handoff_state: dict[str, Any] | None


def execute_routing_fixture(
    fixture_body: dict[str, Any], *, probe: SideEffectProbe
) -> RoutingExecution:
    operation = fixture_body["operation_sequence"][0]
    with guard_pure_policy(probe):
        outcome = consume_handoff(operation)

    trace = ExecutionTrace(probe=probe)
    trace.state_trace = list(outcome.state_trace)
    trace.final_state = outcome.final_state
    trace.final_entity_type = outcome.final_entity_type
    trace.final_reason_code = outcome.final_reason_code
    trace.final_reason_owner = outcome.final_reason_owner
    trace.declared_write_set = list(outcome.declared_write_set)
    #: For this pure handler the declared set IS the operation's write set: the
    #: guard proves the handler itself crossed no file/DB/process/network
    #: boundary, and the transition it decided is exactly what it declared.
    trace.write_set = list(outcome.declared_write_set)
    trace.event_trace = list(outcome.event_trace)
    trace.external_effect_trace = list(outcome.external_effect_trace)
    trace.receipts.append(
        ReceiptRecord(
            code=outcome.receipt.code.value,
            schema_version=outcome.receipt.schema_version,
        )
    )
    return RoutingExecution(
        trace=trace,
        result_status=outcome.result_status,
        failure_class=outcome.failure_class,
        handoff_state=outcome.handoff_state,
    )
