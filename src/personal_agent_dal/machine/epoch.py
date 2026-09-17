"""`OP-EPOCH-001`: epoch-bound result acceptance (DAL-016, G2).

A worker result is only accepted when the epochs it was issued under are still
current. ``accept_epoch_bound_result`` judges the submitted epochs against the
current epochs and refuses a result whose binding epoch went stale — the
durability half of the lease/heartbeat boundary: a capability, lease or approval
that has since advanced must not let a stale result slip through.

The frozen G2 variants each leave at least one submitted epoch behind the
current one:

- **all_old** / **lease_capability** / **old_capability** / **old_lease** —
  a `coding` feature whose capability or lease epoch is stale → `CAPABILITY_STALE`;
- **capability_approval** / **old_approval** — an `approved` feature whose
  approval epoch is stale → `APPROVAL_INVALID`.

The phase the feature is in names the binding epoch, and therefore the refusal
code: an `approved` feature is still consuming its approval (approval epoch),
while a `coding` feature runs under its capability/lease. The decision is pure
— it never moves the feature; the `block_feature` transitions belong to the
resolver and the deterministic engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-EPOCH-001"
COMMAND_TYPE: Final[str] = "accept_epoch_bound_result"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "capability-store"

#: The target aggregate is a `feature`, so its receipt is expressed in the
#: feature transition-receipt schema — `engine.RECEIPT_SCHEMAS["feature"]`.
#: Defined locally (rather than imported from `engine`) to keep this module
#: self-contained and free of the engine's heavy import graph.
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: The three binding epochs a result carries, in the order the frozen oracles
#: name them. Any submitted epoch behind its current value is stale.
EPOCH_KEYS: Final[tuple[str, ...]] = ("approval_epoch", "capability_epoch", "lease_epoch")

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
#: The states whose binding epoch is the approval (approved) versus the
#: capability/lease (coding). The refusal code follows the phase.
APPROVAL_BOUND_STATE: Final[str] = "approved"
CODING_BOUND_STATE: Final[str] = "coding"

FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"current_epochs", "submitted_epochs"}
)
RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"lease_id", "source", "status"}
)


@dataclass(frozen=True)
class EpochEvaluation:
    """The complete observable result of the pure epoch decision.

    The executor consumes these fields instead of reconstructing state from the
    fixture. The empty traces are declarations by the production policy; the
    test harness independently guards the filesystem, process, network and DB
    boundaries while this function runs.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, str]
    final_state: str
    final_entity_type: str
    declared_write_set: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()
    external_effect_trace: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _validate_epochs(value: Any, *, field: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise _invalid(f"{field} must be an object")
    if frozenset(value) != frozenset(EPOCH_KEYS):
        raise _invalid(f"{field} shape is not closed")
    for key in EPOCH_KEYS:
        epoch = value[key]
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch < 0
        ):
            raise _invalid(f"{field}.{key} must be a non-negative integer")
    return value


def _validate_command(command: dict[str, Any]) -> None:
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
        raise _invalid("wrong epoch spec")
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
    target_state = target.get("state")
    if (
        not isinstance(target.get("entity_id"), str)
        or not target["entity_id"]
        or target.get("entity_type") != "feature"
        or target_state not in (APPROVAL_BOUND_STATE, CODING_BOUND_STATE)
        or not isinstance(target.get("version"), int)
        or isinstance(target.get("version"), bool)
        or target["version"] < 0
    ):
        raise _invalid("invalid epoch target")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 1:
        raise _invalid("action sequence must contain exactly one step")
    action = action_sequence[0]
    if not isinstance(action, dict):
        raise _invalid("the action step must be an object")
    if action.get("command") != COMMAND_TYPE:
        raise _invalid("unexpected action sequence")
    result_sha = action.get("result_sha256")
    if not isinstance(result_sha, str) or not result_sha:
        raise _invalid("result_sha256 must be a non-empty string")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    _validate_epochs(facts.get("current_epochs"), field="current_epochs")
    _validate_epochs(facts.get("submitted_epochs"), field="submitted_epochs")

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list):
        raise _invalid("injected_results must be a list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every worker result must be an object")
        if not frozenset(result) <= RESULT_FIELDS:
            raise _invalid("worker result contains an unknown field")
        if result.get("source") != "worker":
            raise _invalid("unexpected worker result source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("worker result status must be a non-empty string")
        if "lease_id" in result and (
            not isinstance(result["lease_id"], str) or not result["lease_id"]
        ):
            raise _invalid("worker result lease_id must be a non-empty string")


def _is_stale(current: dict[str, int], submitted: dict[str, int]) -> bool:
    """True when any submitted epoch is behind its current value."""
    return any(submitted[key] < current[key] for key in EPOCH_KEYS)


def accept_epoch_bound_result(command: dict[str, Any]) -> EpochEvaluation:
    """Refuse a result whose binding epoch went stale.

    The result carries the epochs it was issued under (`submitted_epochs`) and
    the store's current epochs. A result whose binding epoch is stale is refused
    with zero writes; the refusal code follows the phase the feature is in —
    `APPROVAL_INVALID` for an `approved` feature (the approval epoch is the
    binding one), `CAPABILITY_STALE` for a `coding` feature (the capability and
    lease epochs are binding). A current result is accepted, but that outcome is
    not part of the frozen G2 set.
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]
    target = payload["target"]
    state = target["state"]

    receipt = OperationReceipt(
        ReceiptCode.APPLIED,
        schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
    )
    if _is_stale(facts["current_epochs"], facts["submitted_epochs"]):
        code = (
            ReceiptCode.APPROVAL_INVALID
            if state == APPROVAL_BOUND_STATE
            else ReceiptCode.CAPABILITY_STALE
        )
        receipt = OperationReceipt(
            code, schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA
        )

    return EpochEvaluation(
        receipt=receipt,
        state_trace=(state, state),
        final_state=state,
        final_entity_type=target["entity_type"],
    )
