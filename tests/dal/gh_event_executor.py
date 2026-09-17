"""Executes the frozen `DAL-T-GH-EVENT-001` fixtures against the real intake policy.

`DAL-T-GH-EVENT-001` proves the GitHub intake boundary (DAL-014/015, G2): a
webhook/poll event that fails the single-repo permission matrix — a fork, an
unknown repository, an unknown sender, an edited event or a replayed delivery —
is refused `POLICY_DENIED` with zero writes and the feature left in `intake`.

The handler is pure — it evaluates the matrix and returns a receipt without
touching a database, so the executor needs no database and no seeding (mirroring
`execute_injection_fixture`). It runs the real `accept_github_intake` under
`guard_pure_policy` and reduces the result into a trace the oracle comparator
can judge. The receipt's schema is read from the handler's return value, never
hardcoded to the oracle's expectation.
"""

from __future__ import annotations

from typing import Any

from personal_agent_dal.github.webhook import accept_github_intake

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe, guard_pure_policy


def execute_gh_event_fixture(
    fixture_body: dict[str, Any],
    *,
    probe: SideEffectProbe,
) -> ExecutionTrace:
    """Run the real GitHub intake policy and record its trace."""
    op = fixture_body["operation_sequence"][0]
    with guard_pure_policy(probe):
        outcome = accept_github_intake(op)

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
