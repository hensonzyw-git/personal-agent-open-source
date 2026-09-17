"""Executes the frozen `DAL-T-EPOCH-001` fixtures against the real epoch policy.

`DAL-T-EPOCH-001` proves the epoch-bound result boundary (DAL-016, G2): a worker
result whose submitted binding epoch is stale is refused — `APPROVAL_INVALID`
for an `approved` feature, `CAPABILITY_STALE` for a `coding` one — with zero
writes and the feature left where it was.

The handler is pure — it judges the epochs and returns a receipt without
touching a database, so the executor needs no database and no seeding (mirroring
`execute_gh_event_fixture`). It runs the real `accept_epoch_bound_result` under
`guard_pure_policy` and reduces the result into a trace the oracle comparator
can judge. The receipt's schema is read from the handler's return value, never
hardcoded to the oracle's expectation.
"""

from __future__ import annotations

from typing import Any

from personal_agent_dal.machine.epoch import accept_epoch_bound_result

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe, guard_pure_policy


def execute_epoch_fixture(
    fixture_body: dict[str, Any],
    *,
    probe: SideEffectProbe,
) -> ExecutionTrace:
    """Run the real epoch policy and record its trace."""
    op = fixture_body["operation_sequence"][0]
    with guard_pure_policy(probe):
        outcome = accept_epoch_bound_result(op)

    trace = ExecutionTrace(probe=probe)
    trace.state_trace = list(outcome.state_trace)
    trace.final_state = outcome.final_state
    trace.final_entity_type = outcome.final_entity_type
    trace.declared_write_set = list(outcome.declared_write_set)
    trace.event_trace = list(outcome.event_trace)
    trace.external_effect_trace = list(outcome.external_effect_trace)
    trace.receipts.append(
        ReceiptRecord(
            code=outcome.receipt.code.value,
            schema_version=outcome.receipt.schema_version,
        )
    )
    return trace
