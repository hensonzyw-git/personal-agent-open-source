"""Executes the frozen DAL-012 injection fixture against the real policy engine.

`DAL-T-INJECTION-001/api_intake` proves the intake injection boundary: content
tainted by the content parser, requesting capabilities beyond the current set,
is refused `POLICY_DENIED` with zero writes.

The handler is pure — it evaluates the policy and returns a receipt without
touching a database, so the executor needs no database and no seeding (mirroring
`execute_config_load`, not `execute_batch_fixture`). It records the pre-state,
runs the real `evaluate_untrusted_content`, and reduces the result into a trace
the oracle comparator can judge. The receipt's schema is read from the handler's
return value, never hardcoded to the oracle's expectation — a handler that
started emitting the wrong schema would fail the comparator, not be papered over.
"""

from __future__ import annotations

from typing import Any

from personal_agent_dal.machine.injection import evaluate_untrusted_content

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe, guard_pure_policy


def execute_injection_fixture(
    fixture_body: dict[str, Any],
    *,
    probe: SideEffectProbe,
) -> ExecutionTrace:
    """Run the real injection policy engine and record its trace."""
    op = fixture_body["operation_sequence"][0]
    with guard_pure_policy(probe):
        outcome = evaluate_untrusted_content(op)

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
    # `write_set` is empty because the guarded call observed no file/DB/process/
    # network boundary.  Any crossing raises before this trace can be scored.
    return trace
