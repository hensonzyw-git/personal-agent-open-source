"""`OP-LEASE-001`: worker-lease issuance and result acceptance (DAL-016, G2).

The durable-jobs half of the lease/heartbeat boundary. A paused feature resumes
when a worker is leased to it; the lease is only safe to issue or act on while
the lease's epoch is current and the repository base has not drifted. Two
operations share this spec:

- **issue_worker_lease** — issue a fresh lease. A lease whose base read-back no
  longer matches the approved base is a drift, surfaced as a `STATE_DRIFT` block
  signal the resolver carries into `BLK-DRIFT--paused`. A lease that is already
  expired or revoked is refused `CAPABILITY_STALE` with zero writes.
- **accept_worker_result** — accept a worker's result. A result for a lease that
  has been revoked is refused `CAPABILITY_STALE` with zero writes.

The frozen G2 variants are **new_lease_after_drift** (block), and
**old_worker_result** / **pause_expire** (refusals). The decision is pure — it
never moves the feature; the `block_feature` transition is applied by the
trusted resolver and the deterministic engine, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-LEASE-001"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "worker-controller"

#: The target aggregate is a `feature`, so its receipt is expressed in the
#: feature transition-receipt schema — `engine.RECEIPT_SCHEMAS["feature"]`.
#: Defined locally (rather than imported from `engine`) to keep this module
#: self-contained and free of the engine's heavy import graph.
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: The reason a base-drift block carries, matching the frozen
#: `BLK-DRIFT--paused` transition's allowed reason code.
BLOCK_REASON: Final[str] = "STATE_DRIFT"

ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "issue_worker_lease",
    "accept_worker_result",
)

#: A fresh lease request is judged by its base read-back; a dead lease is judged
#: by its epoch and refused.
LEASE_STATUS_REQUESTED: Final[str] = "requested"
DEAD_LEASE_STATUSES: Final[tuple[str, ...]] = ("expired", "revoked")

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
TARGET_STATE: Final[str] = "paused"

#: The two closed facts shapes. A `requested` lease carries its base read-back;
#: a dead lease carries the lease epoch that went stale.
BASE_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"approved_base_sha", "lease_status", "observed_base_sha"}
)
EPOCH_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"current_epoch", "lease_epoch", "lease_status"}
)

RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"head_sha", "result_sha256", "source", "status"}
)


@dataclass(frozen=True)
class LeaseEvaluation:
    """The complete observable result of the pure lease decision.

    ``block_reason`` is the signal the resolver must carry into
    ``block_feature`` (`BLK-DRIFT--paused`) when the base drifted; it is None
    for a refusal or an acceptance. The executor consumes these fields instead
    of reconstructing state from the fixture; the empty traces are declarations
    by the production policy.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, str]
    final_state: str
    final_entity_type: str
    block_reason: str | None = None
    declared_write_set: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()
    external_effect_trace: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _validate_sha(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{field} must be a non-empty string")
    return value


def _validate_epoch(value: Any, *, field: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise _invalid(f"{field} must be a non-negative integer")
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
        raise _invalid("wrong lease spec")
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
        or target.get("state") != TARGET_STATE
        or not isinstance(target.get("version"), int)
        or isinstance(target.get("version"), bool)
        or target["version"] < 0
    ):
        raise _invalid("invalid lease target")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 1:
        raise _invalid("action sequence must contain exactly one step")
    action = action_sequence[0]
    if not isinstance(action, dict):
        raise _invalid("the action step must be an object")
    if action.get("command") not in ACTION_COMMANDS:
        raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict):
        raise _invalid("authoritative facts must be an object")
    lease_status = facts.get("lease_status")
    if lease_status == LEASE_STATUS_REQUESTED:
        if frozenset(facts) != BASE_FACT_FIELDS:
            raise _invalid("requested lease facts shape is not closed")
        _validate_sha(facts.get("approved_base_sha"), field="approved_base_sha")
        _validate_sha(facts.get("observed_base_sha"), field="observed_base_sha")
    elif lease_status in DEAD_LEASE_STATUSES:
        if frozenset(facts) != EPOCH_FACT_FIELDS:
            raise _invalid("dead lease facts shape is not closed")
        _validate_epoch(facts.get("current_epoch"), field="current_epoch")
        _validate_epoch(facts.get("lease_epoch"), field="lease_epoch")
    else:
        raise _invalid("lease_status must be requested, expired or revoked")

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list):
        raise _invalid("injected_results must be a list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every worker-controller result must be an object")
        if not frozenset(result) <= RESULT_FIELDS:
            raise _invalid("worker-controller result contains an unknown field")
        if result.get("source") != "worker_controller":
            raise _invalid("unexpected worker-controller result source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("worker-controller result status must be a non-empty string")
        if "head_sha" in result:
            _validate_sha(result["head_sha"], field="result head_sha")
        if "result_sha256" in result:
            _validate_sha(result["result_sha256"], field="result_sha256")


def evaluate_lease(command: dict[str, Any]) -> LeaseEvaluation:
    """Refuse a dead lease, block a base drift, or accept a clean lease.

    The decision is pure: it never moves the feature. A lease that is expired or
    revoked is refused `CAPABILITY_STALE` with zero writes. A fresh lease whose
    base read-back drifted is reported as a `STATE_DRIFT` block signal, which the
    resolver carries into `BLK-DRIFT--paused`. A clean lease is accepted, but
    that outcome is not part of the frozen G2 set.
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]
    target = payload["target"]
    action = payload["action_sequence"][0]["command"]
    state = target["state"]
    entity_type = target["entity_type"]

    refused = OperationReceipt(
        ReceiptCode.CAPABILITY_STALE,
        schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
    )

    # A dead lease cannot issue a new lease or accept a worker result: the
    # capability bound to the lease is stale.
    if facts["lease_status"] in DEAD_LEASE_STATUSES:
        return LeaseEvaluation(
            receipt=refused,
            state_trace=(state, state),
            final_state=state,
            final_entity_type=entity_type,
        )

    # A fresh lease whose base read-back no longer matches the approved base is
    # a drift: the resolver blocks the feature. The receipt here is a
    # placeholder — the resolver records the engine's APPLIED receipt.
    if action == ACTION_COMMANDS[0] and (
        facts["observed_base_sha"] != facts["approved_base_sha"]
    ):
        return LeaseEvaluation(
            receipt=refused,
            state_trace=(state, state),
            final_state=state,
            final_entity_type=entity_type,
            block_reason=BLOCK_REASON,
        )

    return LeaseEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(state, state),
        final_state=state,
        final_entity_type=entity_type,
    )
