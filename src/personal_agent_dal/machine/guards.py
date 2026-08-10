"""Evaluation of the frozen guard predicates.

A guard is declarative: an `all_of` list of `{field, operator, value}` clauses
over a flat fact namespace, plus two explicit fail-closed switches
(`missing_field_result`, `evaluation_error_result`). The evaluator implements
exactly those operators and nothing else — an unknown operator denies rather
than being skipped, because a guard that quietly evaluates to "no clauses left
to check" is a guard that passes.

`equals_field` is the important one, and the reason this module exists at all.
Contract §3.6 forbids a caller from submitting a derived boolean such as
`binding_valid=true`; the guard has to compare two facts the **server** holds.
So the right-hand side of `equals_field` is resolved from the same fact
namespace, never from the request, and a right-hand side that is absent is a
missing field, not an empty match.

Facts reach this module through `GuardFacts`, which the caller builds from
database rows and resolved protected evidence. The evaluator has no access to
the request payload, so it cannot accidentally satisfy a guard from the thing
the guard is meant to check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Final

from personal_agent_core.timeutil import parse_rfc3339


#: Run gates, ordered. `at_least` compares positions here rather than strings,
#: because "G10" < "G5" lexicographically and that would silently open a gate.
RUN_GATES: Final[tuple[str, ...]] = ("G0", "G1", "G2", "G3", "G4", "G5", "G6")

#: Sentinel distinguishing "the fact is absent" from "the fact is None". A
#: guard clause comparing against a genuinely null fact is a real comparison;
#: a clause referring to a fact nobody supplied is a missing field.
MISSING: Final[object] = object()


class GuardEvaluationError(Exception):
    """A clause could not be evaluated at all (bad type, unknown operator)."""


@dataclass(frozen=True)
class GuardFacts:
    """The server-derived facts a guard may see.

    Deliberately a plain flat mapping: the guard registry addresses facts by
    dotted name (`evidence.subject_aggregate_type`, `checkpoint.state`), and
    keeping the shape flat means there is exactly one way to spell a fact and
    no traversal logic that could invent one.
    """

    values: dict[str, Any]

    def get(self, field: str) -> Any:
        return self.values.get(field, MISSING)


def _op_equals(actual: Any, expected: Any, facts: GuardFacts) -> bool:
    return actual == expected


def _op_equals_field(actual: Any, expected: Any, facts: GuardFacts) -> bool:
    """Compare two server-held facts. The RHS names a fact, not a literal."""
    if not isinstance(expected, str):
        raise GuardEvaluationError("equals_field needs a field name")
    other = facts.get(expected)
    if other is MISSING:
        # The comparison cannot be made. Treated as a missing field by the
        # caller's fail-closed policy rather than as inequality, so a guard
        # never passes because half of it was absent.
        raise KeyError(expected)
    return actual == other


def _op_in(actual: Any, expected: Any, facts: GuardFacts) -> bool:
    if not isinstance(expected, (list, tuple)):
        raise GuardEvaluationError("in needs a list")
    return actual in expected


def _op_greater_than(actual: Any, expected: Any, facts: GuardFacts) -> bool:
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        raise GuardEvaluationError("greater_than needs a number")
    return actual > expected


def _op_at_least(actual: Any, expected: Any, facts: GuardFacts) -> bool:
    """Ordered run-gate comparison, by position rather than by string."""
    if actual not in RUN_GATES or expected not in RUN_GATES:
        raise GuardEvaluationError(f"unknown run gate in {actual!r} >= {expected!r}")
    return RUN_GATES.index(actual) >= RUN_GATES.index(expected)


def _as_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return parse_rfc3339(value)
    raise GuardEvaluationError(f"not a timestamp: {value!r}")


def _op_timestamp_after_field(actual: Any, expected: Any, facts: GuardFacts) -> bool:
    if not isinstance(expected, str):
        raise GuardEvaluationError("timestamp_after_field needs a field name")
    other = facts.get(expected)
    if other is MISSING:
        raise KeyError(expected)
    return _as_timestamp(actual) > _as_timestamp(other)


#: The closed operator set. All handlers share the signature
#: `(actual, expected, facts)`, including the three that ignore `facts`: a
#: dispatch table whose functions differ in shape needs an adapter at the call
#: site, and the adapter is where one of them stops being adapted.
_OPERATORS: Final[dict[str, Callable[[Any, Any, GuardFacts], bool]]] = {
    "equals": _op_equals,
    "equals_field": _op_equals_field,
    "in": _op_in,
    "greater_than": _op_greater_than,
    "at_least": _op_at_least,
    "timestamp_after_field": _op_timestamp_after_field,
}


@dataclass(frozen=True)
class GuardOutcome:
    """Whether the guard passed, and if not, which clause stopped it."""

    passed: bool
    failed_clause: str | None = None


def evaluate_guard(predicate: dict[str, Any], facts: GuardFacts) -> GuardOutcome:
    """Evaluate one frozen guard predicate against server-derived facts.

    Every clause must hold (`all_of`). A missing fact yields
    `missing_field_result` and an unevaluable clause yields
    `evaluation_error_result`; both are `false` throughout the frozen registry,
    which is what makes the guards fail closed.
    """
    missing_result = predicate.get("missing_field_result", False)
    error_result = predicate.get("evaluation_error_result", False)

    for clause in predicate.get("all_of", []):
        field = clause["field"]
        operator = clause["operator"]
        expected = clause["value"]

        handler = _OPERATORS.get(operator)
        if handler is None:
            # Never skip a clause the evaluator does not understand: that
            # turns an unimplemented rule into a satisfied one.
            if not error_result:
                return GuardOutcome(False, f"{field}: unknown operator {operator}")
            continue

        actual = facts.get(field)
        if actual is MISSING:
            if not missing_result:
                return GuardOutcome(False, f"{field}: fact not supplied")
            continue

        try:
            satisfied = handler(actual, expected, facts)
        except KeyError as missing_rhs:
            if not missing_result:
                return GuardOutcome(
                    False, f"{field}: comparison fact {missing_rhs.args[0]} absent"
                )
            continue
        except (GuardEvaluationError, TypeError, ValueError) as error:
            if not error_result:
                return GuardOutcome(False, f"{field}: {error}")
            continue

        if not satisfied:
            return GuardOutcome(False, f"{field} failed {operator}")

    return GuardOutcome(True)
