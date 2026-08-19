"""`OP-DISPOSITION-001`: review-disposition recompute boundary (DAL-023, G3).

Freeze package §5 (docs/dal/DAL021-024_合同冻结包_v0.1.md) makes the review
disposition a pure function of three things the controller re-derives from
protected facts, never of the provider's self-reported `disposition` string:

- **coverage** — the reviewer's `coverage[]` must name exactly the plan's
  acceptance ids, each with a verification-id set equal to that acceptance's.
- **pins** — `reviewed_input_manifest_sha256`, `reviewed_diff_base_sha` and
  `reviewed_result_sha` must equal the recomputed inputs.
- **named work** — `findings[]` or `acceptance_gaps[]` non-empty.

The recomputed disposition is then compared against the provider's declared
value: `approve` is legal only when coverage is complete, pins match and there
is no named work; otherwise `request_changes`. A provider that declares
`approve` with findings/gaps present, declares `request_changes` with nothing
named, or reports incomplete coverage, is a contract failure — the provider's
own text cannot authorize the outcome.

A legal `approve` moves the feature `reviewing → verified` (`review.completed`,
four-write accept set); a legal `request_changes` moves `reviewing → fixing`
(`fix.requested`, four-write base set); any recompute disagreement moves
`reviewing → needs_human` (`feature.blocked`, `PROVIDER_CONTRACT_FAILURE`,
reason owner `feature`, seven-write block set).

`recompute_review_disposition` is the pure decision. The trusted half raises
`DalError(INVALID_ARGUMENT)` on drift; the review payload is provider output
and fails closed as a contract block, never a crash. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-DISPOSITION-001"
COMMAND_TYPE: Final[str] = "recompute_review_disposition"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "review-controller"

#: The target aggregate is a `feature`; its transition receipt schema is the
#: frozen `dal.transition-receipt/1.0` (engine.RECEIPT_SCHEMAS["feature"]).
#: Kept local so this module stays free of the engine's import graph.
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

BASE_WRITE_SET: Final[tuple[str, ...]] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
)
BLOCK_WRITE_SET: Final[tuple[str, ...]] = BASE_WRITE_SET + (
    "decision_create",
    "decision_projection",
    "notification_outbox",
)

BLOCK_REASON: Final[str] = "PROVIDER_CONTRACT_FAILURE"
BLOCK_EVENT: Final[str] = "feature.blocked"
BLOCK_STATE: Final[str] = "needs_human"

VERIFIED_EVENT: Final[str] = "review.completed"
VERIFIED_STATE: Final[str] = "verified"

FIX_EVENT: Final[str] = "fix.requested"
FIX_STATE: Final[str] = "fixing"

ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "recompute_review_disposition",
    "record_review",
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

#: §5: the plan's acceptance ids, their verification ids, and the controller's
#: recomputed input pins the provider's review must equal.
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"plan_acceptance_ids", "plan_verification_ids", "recomputed_review_inputs"}
)

RECOMPUTED_INPUT_FIELDS: Final[frozenset[str]] = frozenset(
    {"diff_base_sha", "input_manifest_sha256", "result_sha"}
)

#: The closed shape of the injected reviewer result envelope. Its contents are
#: judged in the main function; only this top-level shape is envelope-validated.
RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "acceptance_gaps",
        "coverage",
        "disposition",
        "findings",
        "reviewed_diff_base_sha",
        "reviewed_input_manifest_sha256",
        "reviewed_result_sha",
        "source",
        "status",
    }
)

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class ReviewDispositionEvaluation:
    """The complete observable result of the pure disposition decision.

    `reasons` is diagnostic only — the oracle judges the block reason, the
    write set and the traces, never the reason wording.
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
    reasons: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _is_git_sha_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
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
        raise _invalid("wrong review-disposition spec")
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
        raise _invalid("review-disposition target must be a feature in reviewing")

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
    acceptance_ids = facts.get("plan_acceptance_ids")
    if not isinstance(acceptance_ids, list) or not acceptance_ids or any(
        not isinstance(aid, str) or not aid for aid in acceptance_ids
    ):
        raise _invalid("plan_acceptance_ids must be a non-empty list of non-empty strings")
    if len(acceptance_ids) != len(set(acceptance_ids)):
        raise _invalid("plan_acceptance_ids must not contain duplicates")
    verification_ids = facts.get("plan_verification_ids")
    if not isinstance(verification_ids, dict) or frozenset(verification_ids) != frozenset(
        acceptance_ids
    ):
        raise _invalid("plan_verification_ids must cover exactly the plan_acceptance_ids")
    for aid, vids in verification_ids.items():
        if not isinstance(vids, list) or not vids or any(
            not isinstance(vid, str) or not vid for vid in vids
        ):
            raise _invalid(f"plan_verification_ids[{aid}] must be a non-empty list of non-empty strings")
    recomputed = facts.get("recomputed_review_inputs")
    if not isinstance(recomputed, dict) or frozenset(recomputed) != RECOMPUTED_INPUT_FIELDS:
        raise _invalid("recomputed_review_inputs shape is not closed")
    if not _is_sha256_hex(recomputed.get("input_manifest_sha256")):
        raise _invalid("recomputed_review_inputs input_manifest_sha256 must be 64 hex chars")
    for field in ("diff_base_sha", "result_sha"):
        if not _is_git_sha_hex(recomputed.get(field)):
            raise _invalid(f"recomputed_review_inputs {field} must be a 40-char git sha")

    results = payload.get("injected_results")
    if not isinstance(results, list) or len(results) != 1:
        raise _invalid("injected_results must contain exactly one reviewer result")
    result = results[0]
    if not isinstance(result, dict) or frozenset(result) != RESULT_FIELDS:
        raise _invalid("reviewer result shape is not closed")
    for field in ("source", "status", "disposition"):
        if not isinstance(result.get(field), str) or not result[field]:
            raise _invalid(f"reviewer result {field} must be a non-empty string")
    for field in ("findings", "acceptance_gaps", "coverage"):
        if not isinstance(result.get(field), list):
            raise _invalid(f"reviewer result {field} must be a list")
    if not _is_sha256_hex(result.get("reviewed_input_manifest_sha256")):
        raise _invalid("reviewer result reviewed_input_manifest_sha256 must be 64 hex chars")
    for field in ("reviewed_diff_base_sha", "reviewed_result_sha"):
        if not _is_git_sha_hex(result.get(field)):
            raise _invalid(f"reviewer result {field} must be a 40-char git sha")


def _recompute(facts: dict[str, Any], result: dict[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """Re-derive the disposition from coverage, pins and named work.

    Returns `(recomputed_disposition_or_None, reasons)`. `None` means the
    review is malformed (e.g. coverage references an unknown acceptance) and
    can only fail closed. The recomputed disposition is `"approve"` /
    `"request_changes"` only when coverage and pins are both provable.
    """
    reasons: list[str] = []
    coverage = result["coverage"]
    #: Coverage entries are provider output; a malformed entry is a contract
    #: failure, never a crash. The only safe shape is a dict naming an
    #: acceptance id — anything else means coverage cannot be proved complete.
    covered: set[str] = set()
    coverage_ok = True
    for entry in coverage:
        if not isinstance(entry, dict) or not isinstance(entry.get("acceptance_id"), str):
            coverage_ok = False
            break
        covered.add(entry["acceptance_id"])
    if not coverage_ok or covered != set(facts["plan_acceptance_ids"]):
        reasons.append("coverage is incomplete or references an unknown acceptance")

    pins = facts["recomputed_review_inputs"]
    if (
        result["reviewed_input_manifest_sha256"] != pins["input_manifest_sha256"]
        or result["reviewed_diff_base_sha"] != pins["diff_base_sha"]
        or result["reviewed_result_sha"] != pins["result_sha"]
    ):
        reasons.append("reviewed inputs drifted from the recomputed pins")

    if reasons:
        return None, tuple(reasons)

    named = bool(result["findings"]) or bool(result["acceptance_gaps"])
    recomputed = "approve" if not named else "request_changes"
    return recomputed, ()


def recompute_review_disposition(command: dict[str, Any]) -> ReviewDispositionEvaluation:
    """Recompute one review's disposition and compare it to the provider's text.

    A legal `approve` verifies the feature; a legal `request_changes` requests
    a fix; any recompute disagreement with the provider's declared disposition
    — or a review malformed enough to be unjudgeable — blocks the feature.
    """
    _validate_command(command)
    payload = command["input"]
    target = payload["target"]
    facts = payload["authoritative_facts"]

    result = payload["injected_results"][0]
    reasons: list[str] = []
    if result.get("source") != "reviewer" or result.get("status") != "completed":
        reasons.append("reviewer evidence is missing, duplicated or not completed")

    recomputed, recompute_reasons = _recompute(facts, result)
    reasons.extend(recompute_reasons)

    declared = result["disposition"]
    if recomputed is not None and declared not in ("approve", "request_changes"):
        reasons.append("reviewer declared an unknown disposition")

    #: The provider's declared disposition must equal the recomputed one.
    #: Disagreement — including `approve` with findings, `request_changes` with
    #: nothing named, or `approve` over incomplete coverage — is a contract
    #: failure, never adopted.
    if recomputed is not None and not reasons and declared != recomputed:
        reasons.append("provider disposition disagrees with the recomputed value")

    if reasons:
        return ReviewDispositionEvaluation(
            receipt=OperationReceipt(
                ReceiptCode.APPLIED,
                schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
            ),
            state_trace=(target["state"], BLOCK_STATE),
            final_state=BLOCK_STATE,
            final_entity_type=target["entity_type"],
            final_reason_code=BLOCK_REASON,
            final_reason_owner="feature",
            declared_write_set=BLOCK_WRITE_SET,
            event_trace=(BLOCK_EVENT,),
            reasons=tuple(reasons),
        )

    if recomputed == "approve":
        return ReviewDispositionEvaluation(
            receipt=OperationReceipt(
                ReceiptCode.APPLIED,
                schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
            ),
            state_trace=(target["state"], VERIFIED_STATE),
            final_state=VERIFIED_STATE,
            final_entity_type=target["entity_type"],
            declared_write_set=BASE_WRITE_SET,
            event_trace=(VERIFIED_EVENT,),
        )

    return ReviewDispositionEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(target["state"], FIX_STATE),
        final_state=FIX_STATE,
        final_entity_type=target["entity_type"],
        declared_write_set=BASE_WRITE_SET,
        event_trace=(FIX_EVENT,),
    )
