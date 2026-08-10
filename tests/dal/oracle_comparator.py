"""Judges an execution trace against a frozen oracle.

The comparator is the only place that decides pass or fail. It checks every
dimension the oracle freezes -- state trace, receipts, write set, event and
external-effect traces, final snapshot and forbidden side effects -- and
collects every mismatch rather than stopping at the first, so a failing run
reports the full divergence between what the code did and what the contract
required.

A dimension the oracle leaves empty is asserted as empty: an oracle with an
empty `allowed_write_set` means the operation wrote nothing, so a trace that
records a write fails. Nothing is asserted vacuously.

Test-only module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.dal.operation_executor import ExecutionTrace


@dataclass
class ComparisonResult:
    """The outcome of judging one trace against one oracle."""

    mismatches: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.mismatches

    def add(self, dimension: str, expected: Any, actual: Any) -> None:
        self.mismatches.append(
            f"{dimension}: expected {expected!r}, got {actual!r}"
        )


def _receipts(trace: ExecutionTrace) -> list[dict[str, str]]:
    return [{"code": r.code, "schema_version": r.schema_version}
            for r in trace.receipts]


def compare(trace: ExecutionTrace, oracle_body: dict[str, Any]) -> ComparisonResult:
    """Compare one execution trace against one frozen oracle body."""
    result = ComparisonResult()

    # State trace: the sequence of states the entity passed through.
    expected_states = oracle_body.get("expected_state_trace", [])
    if trace.state_trace != expected_states:
        result.add("state_trace", expected_states, trace.state_trace)

    # Receipts: exact code and schema, in order, with multiplicity.
    expected_receipts = oracle_body.get("expected_receipts", [])
    actual_receipts = _receipts(trace)
    normalised_expected = [
        {"code": r["code"], "schema_version": r["schema_version"]}
        for r in expected_receipts
    ]
    if actual_receipts != normalised_expected:
        result.add("receipts", normalised_expected, actual_receipts)

    # Write set: an empty allowed_write_set means the operation wrote nothing.
    allowed_write_set = oracle_body.get("allowed_write_set", [])
    if not allowed_write_set and trace.write_set:
        result.add("write_set (must be empty)", [], trace.write_set)

    # Event and external-effect traces.
    expected_events = oracle_body.get("expected_event_trace", [])
    if trace.event_trace != expected_events:
        result.add("event_trace", expected_events, trace.event_trace)
    expected_effects = oracle_body.get("expected_external_effect_trace", [])
    if trace.external_effect_trace != expected_effects:
        result.add(
            "external_effect_trace", expected_effects, trace.external_effect_trace
        )

    # Final snapshot: entity type and terminal state.
    snapshot = oracle_body.get("expected_final_snapshot", {})
    expected_final_state = snapshot.get("state")
    if expected_final_state is not None and trace.final_state != expected_final_state:
        result.add("final_state", expected_final_state, trace.final_state)

    # Forbidden side effects: any observed crossing of a forbidden boundary.
    forbidden = oracle_body.get("forbidden_side_effects", [])
    if trace.probe is not None:
        crossings = trace.probe.forbidden_crossings(forbidden)
        if crossings:
            result.add("forbidden_side_effects", "no crossing", sorted(crossings))

    return result
