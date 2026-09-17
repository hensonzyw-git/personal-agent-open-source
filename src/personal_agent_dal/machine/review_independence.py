"""`OP-REVIEW-INDEP-001`: reviewer-independence boundary (DAL-023, G3/G4).

Freeze package §5.1 (docs/dal/DAL021-024_合同冻结包_v0.1.md) fixes the
independence rule: a review is only acceptable from a context that is provably
not the coding context. Three reuse equalities each void it, independently of
the others — same session (`reviewer_session_id == coder_session_id`), same
context content (`reviewer_context_sha256 == coder_context_sha256`), or same
independence key (`reviewer_independence_key == coder_independence_key`).
Production derives the independence key itself as an HMAC-SHA256 binding the
reviewer session to the reviewed artifact (controller-written after the call);
this pure boundary only compares what the facts carry, and the provider's
self-reported independence claim is never evidence.

The frozen `DAL-T-REVIEW-INDEP-001` oracle freezes both outcomes: a fresh
reviewer moves the feature `reviewing → verified` with `review.completed` and
the four-write accept set; a reused context is a clean `POLICY_DENIED` refusal
with **zero** writes and no state change — the feature stays `reviewing`, and
nothing about the refusal is treated as progress.

`accept_independent_review` is the pure decision. The trusted half (command
envelope, target, facts, two-action shape) validates as closed shapes and
raises `DalError(INVALID_ARGUMENT)` on drift; the injected reviewer result is
checked only far enough to be well-formed evidence — its content cannot
authorize anything, only the independence comparison can. No I/O: the actual
reviewer subprocess and the HMAC derivation belong to the controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-REVIEW-INDEP-001"
COMMAND_TYPE: Final[str] = "accept_independent_review"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "review-controller"

#: The target aggregate is a `feature`; its transition receipt schema is the
#: frozen `dal.transition-receipt/1.0` (engine.RECEIPT_SCHEMAS["feature"]).
#: Kept local so this module stays free of the engine's import graph.
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: SM-REVIEW-ACCEPT atomic write set for the fresh path, frozen in the
#: transition registry. The refusal path writes nothing at all.
ACCEPT_WRITE_SET: Final[tuple[str, ...]] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
)

ACCEPT_EVENT: Final[str] = "review.completed"
ACCEPT_STATE: Final[str] = "verified"

#: The two-action group, in order: independence is verified *before* the
#: receipt is accepted, so a refusal can never be followed by an accept.
ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "verify_reviewer_independence",
    "accept_review_receipt",
)

REQUIRED_INPUT_SECTIONS: Final[frozenset[str]] = frozenset(
    {"target", "action_sequence", "authoritative_facts", "injected_results"}
)
COMMAND_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "actor_type",
        "evidence_source_type",
        "idempotency_key",
        "input",
        "operation_id",
        "operation_spec_id",
        "schema_version",
    }
)
INPUT_FIELDS: Final[frozenset[str]] = REQUIRED_INPUT_SECTIONS | {"schema_version"}

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)

#: §5.1: coder and reviewer identities, sessions, contexts and independence
#: keys, plus the evidence kind. Closed — an unknown fact field is drift.
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "coder_identity",
        "coder_session_id",
        "coder_independence_key",
        "coder_context_sha256",
        "reviewer_identity",
        "reviewer_session_id",
        "reviewer_independence_key",
        "reviewer_context_sha256",
        "evidence_kind",
    }
)

#: The closed shape of the injected reviewer result. `finding_count` is the
#: reviewer's own claim; it is recorded but never authorizes anything.
RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source", "status", "finding_count"}
)

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class ReviewIndependenceEvaluation:
    """The complete observable result of the pure independence decision.

    The executor consumes these fields instead of reconstructing state from
    the fixture. On refusal, every trace field stays empty — the frozen
    oracle asserts exactly that.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, str]
    final_state: str
    final_entity_type: str
    final_reason_code: str | None = None
    final_reason_owner: str | None = None
    declared_write_set: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()
    external_effect_trace: tuple[str, ...] = ()
    independence_violations: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_command(command: dict[str, Any]) -> None:
    """Validate the trusted envelope; raise `DalError` on any drift."""
    if not isinstance(command, dict):
        raise _invalid("command must be an object")
    if frozenset(command) != COMMAND_FIELDS:
        raise _invalid("command shape is not closed")
    if command.get("schema_version") != "dal.test-operation-command/1.0":
        raise _invalid("wrong command schema")
    if not isinstance(command.get("operation_id"), str) or not command["operation_id"]:
        raise _invalid("operation_id must be a non-empty string")
    if not isinstance(command.get("idempotency_key"), str) or not command["idempotency_key"]:
        raise _invalid("idempotency_key must be a non-empty string")
    if command.get("operation_spec_id") != OPERATION_SPEC_ID:
        raise _invalid("wrong review-independence spec")
    if command.get("actor_type") != SERVICE_ACTOR:
        raise DalError(DalErrorCode.ACTOR_NOT_ALLOWED)
    if command.get("evidence_source_type") != EVIDENCE_SOURCE:
        raise DalError(DalErrorCode.SCOPE_DENIED)

    payload = command.get("input")
    if not isinstance(payload, dict):
        raise _invalid("input must be an object")
    if frozenset(payload) != INPUT_FIELDS:
        raise _invalid("input shape is not closed")
    if payload.get("schema_version") != "dal.operation-input/1.0":
        raise _invalid("wrong input schema")

    target = payload.get("target")
    if not isinstance(target, dict) or frozenset(target) != TARGET_FIELDS:
        raise _invalid("target shape is not closed")
    if (
        not isinstance(target.get("entity_id"), str)
        or not target["entity_id"]
        or target.get("entity_type") != "feature"
        or target.get("state") != "reviewing"
        or not _is_non_negative_int(target.get("version"))
    ):
        raise _invalid("review-independence target must be a feature in reviewing")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 2:
        raise _invalid("action sequence must contain exactly two steps")
    for index, step in enumerate(action_sequence):
        if not isinstance(step, dict) or frozenset(step) != frozenset({"command"}):
            raise _invalid(f"action step {index} shape is not closed")
        if step.get("command") != ACTION_COMMANDS[index]:
            raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    for field in (
        "coder_identity",
        "coder_session_id",
        "coder_independence_key",
        "reviewer_identity",
        "reviewer_session_id",
        "reviewer_independence_key",
    ):
        if not isinstance(facts.get(field), str) or not facts[field]:
            raise _invalid(f"{field} must be a non-empty string")
    if not _is_sha256_hex(facts.get("coder_context_sha256")):
        raise _invalid("coder_context_sha256 must be 64 hex chars")
    if not _is_sha256_hex(facts.get("reviewer_context_sha256")):
        raise _invalid("reviewer_context_sha256 must be 64 hex chars")
    if facts.get("evidence_kind") not in ("synthetic", "live"):
        raise _invalid("evidence_kind must be synthetic or live")

    results = payload.get("injected_results")
    if not isinstance(results, list):
        raise _invalid("injected_results must be a list")
    #: Shape violations of the injected result are envelope errors; *quality*
    #: gaps (wrong count, wrong source, not completed) are judged in the main
    #: function and fail closed as a POLICY_DENIED refusal, never as a crash.
    for index, result in enumerate(results):
        if not isinstance(result, dict) or frozenset(result) != RESULT_FIELDS:
            raise _invalid(f"reviewer result {index} shape is not closed")
        for field in ("source", "status"):
            if not isinstance(result.get(field), str) or not result[field]:
                raise _invalid(f"reviewer result {index} {field} must be a non-empty string")
        if not _is_non_negative_int(result.get("finding_count")):
            raise _invalid(f"reviewer result {index} finding_count must be a non-negative integer")


def _check_independence(
    facts: dict[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    """Compare the three §5.1 reuse equalities; returns `(independent, violations)`.

    Each equality voids independence on its own; all applicable violations
    are collected so the refusal is auditable, not just binary.
    """
    violations: list[str] = []
    if facts["reviewer_session_id"] == facts["coder_session_id"]:
        violations.append("reviewer reuses the coder session")
    if facts["reviewer_context_sha256"] == facts["coder_context_sha256"]:
        violations.append("reviewer context matches the coder context")
    if facts["reviewer_independence_key"] == facts["coder_independence_key"]:
        violations.append("reviewer reuses the coder independence key")
    return (not violations, tuple(violations))


def accept_independent_review(command: dict[str, Any]) -> ReviewIndependenceEvaluation:
    """Accept or refuse one review receipt under the §5.1 independence rule.

    A fresh, provably independent reviewer completes the review: `reviewing →
    verified`, `review.completed`, the four-write accept set and an `APPLIED`
    receipt. Any reuse equality is a clean `POLICY_DENIED`: the feature stays
    `reviewing`, nothing is written, no event fires.
    """
    _validate_command(command)
    payload = command["input"]
    target = payload["target"]
    facts = payload["authoritative_facts"]

    independent, violations = _check_independence(facts)

    #: Incomplete reviewer evidence fails closed: missing, non-reviewer or
    #: unfinished results can never permit an accept, but the refusal stays a
    #: clean POLICY_DENIED rather than an envelope error.
    reasons = list(violations)
    results = payload["injected_results"]
    completed = [
        r for r in results if r.get("source") == "reviewer" and r.get("status") == "completed"
    ]
    if len(results) != 1 or len(completed) != 1:
        reasons.append("reviewer evidence is missing, duplicated or not completed")

    if reasons:
        return ReviewIndependenceEvaluation(
            receipt=OperationReceipt(
                ReceiptCode.POLICY_DENIED,
                schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
            ),
            state_trace=(target["state"], target["state"]),
            final_state=target["state"],
            final_entity_type=target["entity_type"],
            independence_violations=tuple(reasons),
        )

    return ReviewIndependenceEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(target["state"], ACCEPT_STATE),
        final_state=ACCEPT_STATE,
        final_entity_type=target["entity_type"],
        declared_write_set=ACCEPT_WRITE_SET,
        event_trace=(ACCEPT_EVENT,),
    )
