"""Executor for `DAL-T-VERIFICATION-CONTRACT-001` fixture bodies.

Replays the single `consume_verification` operation command under the pure policy
guard and maps the handler's frozen result onto the generic `ExecutionTrace`.
The verification classifier's own `(result_status, failure_class)` pair, the
`last_verified_sha` it advances or preserves, and the `report_hash` it binds are
not part of the generic trace, so the executor returns a `VerificationExecution`
that carries them alongside the trace; the test judges them against the oracle's
`expected_result_status` / `expected_failure_class` / `expected_last_verified_sha`
/ `expected_report_hash` fields.
"""

from __future__ import annotations

from dataclasses import dataclass

from personal_agent_dal.machine.verification_contract import consume_verification
from personal_agent_dal.receipt import OperationReceipt

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe, guard_pure_policy


@dataclass(frozen=True)
class VerificationExecution:
    """The trace plus the classifier-only fields the generic trace cannot carry."""

    trace: ExecutionTrace
    result_status: str
    failure_class: str | None
    last_verified_sha: str | None
    report_hash: str
    check_receipt: OperationReceipt


def execute_verification_fixture(
    fixture_body: dict, *, probe: SideEffectProbe
) -> VerificationExecution:
    operation = fixture_body["operation_sequence"][0]
    with guard_pure_policy(probe):
        outcome = consume_verification(operation)

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
    return VerificationExecution(
        trace=trace,
        result_status=outcome.result_status,
        failure_class=outcome.failure_class,
        last_verified_sha=outcome.last_verified_sha,
        report_hash=outcome.report.report_hash,
        check_receipt=outcome.report.check_receipt,
    )
