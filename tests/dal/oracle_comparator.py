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

Two extensions support the DAL-009 state-machine scenarios:

- An `expected_receipts` entry may carry `count` (multiplicity of that receipt
  in the sequence), `duplicate_flags` (whether each occurrence was an idempotent
  replay returning the original receipt, §2.6) and `unique_receipt_ids` (how
  many distinct persisted receipt rows the sequence touched). Without those
  fields the behaviour is exactly the old one-receipt-per-entry comparison.
- `scenario_assertions` clauses are judged against the trace's `metrics`
  (e.g. `business_event_count`, `aggregate_version_increment`).

Test-only module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord


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


def _expanded_expected_receipts(expected_receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand oracle receipt entries by their `count` into one row per receipt."""
    expanded: list[dict[str, Any]] = []
    for entry in expected_receipts:
        count = entry.get("count", 1)
        flags = entry.get("duplicate_flags")
        for index in range(count):
            row = {
                "code": entry["code"],
                "schema_version": entry["schema_version"],
            }
            if flags is not None:
                row["duplicate"] = flags[index]
            expanded.append(row)
    return expanded


def _actual_receipts(trace: ExecutionTrace) -> list[dict[str, Any]]:
    return [
        {"code": r.code, "schema_version": r.schema_version, "duplicate": r.duplicate}
        for r in trace.receipts
    ]


def _compare_receipts(
    trace: ExecutionTrace, oracle_body: dict[str, Any], result: ComparisonResult
) -> None:
    expected_receipts = oracle_body.get("expected_receipts", [])
    if not expected_receipts:
        if trace.receipts:
            result.add("receipts", [], _actual_receipts(trace))
        return

    expanded = _expanded_expected_receipts(expected_receipts)
    actual = _actual_receipts(trace)
    if len(actual) != len(expanded):
        result.add("receipts", expanded, actual)
        return
    for index, (expected_row, actual_row) in enumerate(zip(expanded, actual)):
        if (
            expected_row["code"] != actual_row["code"]
            or expected_row["schema_version"] != actual_row["schema_version"]
        ):
            result.add(f"receipts[{index}]", expected_row, actual_row)
            continue
        if "duplicate" in expected_row and expected_row["duplicate"] != actual_row["duplicate"]:
            result.add(f"receipts[{index}].duplicate", expected_row["duplicate"], actual_row["duplicate"])

    # When the oracle freezes how many distinct persisted receipt rows the
    # sequence touched (an idempotent replay must not write a second receipt),
    # count distinct receipt ids actually observed.
    unique_expected = None
    for entry in expected_receipts:
        if "unique_receipt_ids" in entry:
            unique_expected = entry["unique_receipt_ids"]
            break
    if unique_expected is not None:
        ids = [r.receipt_id for r in trace.receipts if r.receipt_id is not None]
        unique_actual = len(set(ids))
        if unique_actual != unique_expected:
            result.add("unique_receipt_ids", unique_expected, unique_actual)


def _compare_scenario_assertions(
    trace: ExecutionTrace, oracle_body: dict[str, Any], result: ComparisonResult
) -> None:
    for clause in oracle_body.get("scenario_assertions", []):
        field = clause["field"]
        operator = clause["operator"]
        expected = clause["value"]
        if field not in trace.metrics:
            result.add(
                f"scenario_assertion.{field}",
                expected,
                "<not measured>",
            )
            continue
        actual = trace.metrics[field]
        if operator == "equals":
            passed = actual == expected
        elif operator == "greater_than":
            passed = actual > expected
        elif operator == "in":
            passed = actual in expected
        elif operator == "at_least":
            passed = actual >= expected
        else:
            result.add(f"scenario_assertion.{field}.operator", operator, "unsupported")
            continue
        if not passed:
            result.add(f"scenario_assertion.{field}", expected, actual)


def compare(trace: ExecutionTrace, oracle_body: dict[str, Any]) -> ComparisonResult:
    """Compare one execution trace against one frozen oracle body."""
    result = ComparisonResult()

    # State trace: the sequence of states the entity passed through.
    expected_states = oracle_body.get("expected_state_trace", [])
    if trace.state_trace != expected_states:
        result.add("state_trace", expected_states, trace.state_trace)

    # Receipts: exact code and schema, in order, with multiplicity and
    # duplicate/replay flags where the oracle freezes them.
    _compare_receipts(trace, oracle_body, result)

    # Write set. An empty allowed_write_set means the operation wrote nothing.
    # A non-empty one is asserted as *set equality*, not as a subset: the
    # oracle names the write classes the operation must perform, so both an
    # extra write and a missing one are failures. Checking only emptiness
    # would let every non-empty oracle pass while writing nothing at all.
    allowed_write_set = oracle_body.get("allowed_write_set", [])
    if set(trace.write_set) != set(allowed_write_set):
        result.add(
            "write_set", sorted(set(allowed_write_set)), sorted(set(trace.write_set))
        )

    # Event and external-effect traces.
    expected_events = oracle_body.get("expected_event_trace", [])
    if trace.event_trace != expected_events:
        result.add("event_trace", expected_events, trace.event_trace)
    expected_effects = oracle_body.get("expected_external_effect_trace", [])
    if trace.external_effect_trace != expected_effects:
        result.add(
            "external_effect_trace", expected_effects, trace.external_effect_trace
        )

    # Final snapshot: entity type, terminal state, and the persisted stop
    # reason. The oracle freezes `reason_code` and `reason_owner` alongside the
    # state; a transition that stops in the right state for the wrong reason is
    # exactly the mis-classification a judge must catch (an unknown result is
    # not the same as a drift block). `None` is a real expectation for the
    # scenarios whose reason the scenario freezes as None -- asserted only when
    # the trace measured the field (final_entity_type set), so the older
    # executors that leave it empty are unchanged.
    snapshot = oracle_body.get("expected_final_snapshot", {})
    expected_final_state = snapshot.get("state")
    if expected_final_state is not None and trace.final_state != expected_final_state:
        result.add("final_state", expected_final_state, trace.final_state)
    measured_final = bool(trace.final_entity_type)
    expected_entity_type = snapshot.get("entity_type")
    if expected_entity_type is not None and measured_final:
        if trace.final_entity_type != expected_entity_type:
            result.add("final_entity_type", expected_entity_type, trace.final_entity_type)
        if "reason_code" in snapshot and trace.final_reason_code != snapshot["reason_code"]:
            result.add("final_reason_code", snapshot["reason_code"], trace.final_reason_code)
        if "reason_owner" in snapshot and trace.final_reason_owner != snapshot["reason_owner"]:
            result.add("final_reason_owner", snapshot["reason_owner"], trace.final_reason_owner)

    # Companion transitions. The oracle freezes which companion transitions a
    # spec carries (§2.3.1); for all ten operation scenarios that is the empty
    # set. An empty expectation must not silently pass a transition that gained
    # a companion: a companion receipt persists with its own id in
    # `transition_receipts.spec_id`, and the executor records any it observed.
    expected_companions = oracle_body.get("expected_atomic_companion_transitions", [])
    if not expected_companions and trace.companion_ids:
        result.add("atomic_companion_transitions", [], trace.companion_ids)

    # Scenario metrics the oracle freezes beyond the trace dimensions.
    _compare_scenario_assertions(trace, oracle_body, result)

    # Forbidden side effects: any observed crossing of a forbidden boundary.
    forbidden = oracle_body.get("forbidden_side_effects", [])
    if trace.probe is not None:
        crossings = trace.probe.forbidden_crossings(forbidden)
        if crossings:
            result.add("forbidden_side_effects", "no crossing", sorted(crossings))

    return result
