"""Executor for `DAL-T-REVIEW-INDEP-001` fixture bodies.

Replays the `accept_independent_review` operation command under the pure
policy guard and maps the handler's frozen result onto the generic
`ExecutionTrace`. The two-action group is consumed as one operation by the
handler (independence verification precedes receipt acceptance inside it), so
the executor dispatches exactly one call — a generic per-action dispatcher
would split a group whose refusal half must never run.
"""

from __future__ import annotations

from typing import Any

from personal_agent_dal.machine.review_independence import accept_independent_review

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe, guard_pure_policy


def execute_review_independence_fixture(
    fixture_body: dict[str, Any], *, probe: SideEffectProbe
) -> ExecutionTrace:
    operation = fixture_body["operation_sequence"][0]
    with guard_pure_policy(probe):
        outcome = accept_independent_review(operation)

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
    #: Refusal variants declare (and therefore observe) the empty set.
    trace.write_set = list(outcome.declared_write_set)
    trace.event_trace = list(outcome.event_trace)
    trace.external_effect_trace = list(outcome.external_effect_trace)
    trace.receipts.append(
        ReceiptRecord(
            code=outcome.receipt.code.value,
            schema_version=outcome.receipt.schema_version,
        )
    )
    return trace
