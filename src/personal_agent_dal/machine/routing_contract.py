"""`OP-ROUTING-CONTRACT-001`: primary→fallback handoff boundary (DAL-027, G3).

Freeze package `docs/dal/DAL027_fallback_classifier_合同冻结包_v0.1.md` fixes the
fallback decision: when the primary coder run ends in a provider/model-level
failure, the controller asks whether the work may be handed to the configured
fallback slot (Changhe GLM-5.2). The handoff must not lose the work state
(`base_sha`, `diff_sha`, `tests_receipt`, `last_verified_sha`) and must fail
closed on any classifier drift or unknown slot request.

`consume_handoff` is the pure decision. It classifies the request into one
outcome of the frozen vocabulary — `fallback_allowed`, `fallback_denied`,
`no_fallback_route`, or `blocked` — carrying `(failure_class, reason_code)`
(`policy_failure` for classifier drift, `contract_failure` for an unknown slot)
only for the fail-closed blocks. The trusted half (command envelope, target,
routing snapshot, work state, the `injected_results` container shape) validates
as closed shapes and raises `DalError(INVALID_ARGUMENT)` on drift. The untrusted
values — the observed classifier and the primary failure class — never raise:
drift is classified fail-closed, never a crash.

The slot vocabulary is closed to the frozen `routing-freeze_schema_v1.0.json`
`routing[].slot` role names (`primary / fallback / classifier / review`); the
function invents no new slot. It performs no I/O: the actual re-invocation of
the coder with a different `model_alias` belongs to the controller.

The handoff is a *decision layer only*: it never emits a new `failure_class`.
The verifier's `task_failure` (DAL-029) is deliberately not fallback-eligible —
swapping models cannot fix tests that fail, that is the review/fix loop's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-ROUTING-CONTRACT-001"
COMMAND_TYPE: Final[str] = "consume_handoff"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "routing-adapter"
CONTRACT_VERSION: Final[str] = "dal.routing-resolution/1.0"

FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: Frozen block write set (§5 of the freeze package) — a fail-closed routing
#: block is the operation succeeding at blocking, not a refusal.
BLOCK_WRITE_SET: Final[tuple[str, ...]] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
    "decision_create",
    "decision_projection",
    "notification_outbox",
)
BLOCK_EVENT: Final[str] = "feature.blocked"

#: The closed routing slot vocabulary, taken verbatim from the frozen
#: `routing-freeze_schema_v1.0.json` `routing[].slot` role names. A slot is a
#: stable *role*, never a provider name; no new slot may be invented here.
SLOT_NAMES: Final[frozenset[str]] = frozenset(
    {"primary", "fallback", "classifier", "review"}
)

#: A slot's frozen shape: provider + model. Both may be empty, which means the
#: slot is *unconfigured* — a legitimate frozen state that must not be silently
#: auto-filled with an invented route.
SLOT_FIELDS: Final[frozenset[str]] = frozenset({"provider", "model"})

#: The frozen work state a fallback coder must inherit, byte for byte (§4).
WORK_STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"base_sha", "diff_sha", "tests_receipt", "last_verified_sha"}
)

#: The observed classifier the controller reports after the primary run (§3).
CLASSIFIER_OBSERVED_FIELDS: Final[frozenset[str]] = frozenset(
    {"provider", "model", "digest_pre", "digest_post"}
)

#: `failure_class` → `reason_code` (storage FEATURE_REASON_CODES) → feature state,
#: for the fail-closed blocks only. There is deliberately no `task_failure` here:
#: the handoff never emits a failure class of its own.
FAILURE_TO_REASON: Final[dict[str, str]] = {
    "policy_failure": "POLICY_FAILURE",
    "contract_failure": "PROVIDER_CONTRACT_FAILURE",
}
REASON_TO_STATE: Final[dict[str, str]] = {
    "POLICY_FAILURE": "needs_human",
    "PROVIDER_CONTRACT_FAILURE": "needs_human",
}

#: The primary failure classes that ARE fallback-eligible: a provider/model-level
#: failure a different model can plausibly repair. Budget, policy, task and
#: cancel/success outcomes are excluded — none of them is fixed by swapping
#: models, and `task_failure` is the review/fix loop's domain (DAL-030).
FALLBACK_ALLOWED_CLASSES: Final[frozenset[str]] = frozenset(
    {"transient", "usage_limit", "auth", "contract_failure"}
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
ACTION_FIELDS: Final[frozenset[str]] = frozenset({"command", "contract_version"})
TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)

#: The authoritative (trusted) facts of a handoff (§3 of the freeze package):
#: the slot being requested, the frozen routing snapshot, the classifier digest,
#: and the work state the fallback must inherit.
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"requested_slot", "routing_snapshot", "classifier_digest", "work_state"}
)

#: The untrusted `injected_results` container (§4). The container shape is
#: trusted (drift raises); the values it carries are the controller's report of
#: the primary run and are classified, never trusted.
INJECTED_FIELDS: Final[frozenset[str]] = frozenset(
    {"primary_failure_class", "observed_classifier"}
)

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class RoutingEvaluation:
    """The complete observable result of the pure handoff decision.

    `result_status` / `failure_class` are the routing-specific outcome pair the
    executor carries alongside the generic trace; the oracle freezes them as
    `expected_result_status` / `expected_failure_class`. `handoff_state` is the
    four-field work state a `fallback_allowed` decision hands on verbatim, and
    `None` otherwise; the oracle freezes it as `expected_handoff_state`.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, ...]
    final_state: str
    final_entity_type: str
    result_status: str
    failure_class: str | None
    handoff_state: dict[str, Any] | None = None
    final_reason_code: str | None = None
    final_reason_owner: str | None = None
    declared_write_set: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()
    external_effect_trace: tuple[str, ...] = ()
    contract_failure_reasons: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _is_sha40_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX for c in value)


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
        raise _invalid("wrong routing-contract spec")
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
        or target.get("state") != "coding"
        or not _is_non_negative_int(target.get("version"))
    ):
        raise _invalid("routing-contract target must be a feature in coding")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 1:
        raise _invalid("action sequence must contain exactly one step")
    step = action_sequence[0]
    if not isinstance(step, dict) or frozenset(step) != ACTION_FIELDS:
        raise _invalid("unexpected action sequence shape")
    if step.get("command") != COMMAND_TYPE or step.get("contract_version") != CONTRACT_VERSION:
        raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    if not isinstance(facts.get("requested_slot"), str) or not facts["requested_slot"]:
        raise _invalid("requested_slot must be a non-empty string")
    if not _is_sha256_hex(facts.get("classifier_digest")):
        raise _invalid("classifier_digest must be 64 hex chars")

    snapshot = facts.get("routing_snapshot")
    if not isinstance(snapshot, dict) or not set(snapshot) <= SLOT_NAMES:
        raise _invalid("routing_snapshot keys must be a subset of the closed slot vocabulary")
    if "primary" not in snapshot or "classifier" not in snapshot:
        raise _invalid("routing_snapshot must configure primary and classifier")
    for slot, value in snapshot.items():
        if not isinstance(value, dict) or frozenset(value) != SLOT_FIELDS:
            raise _invalid("routing_snapshot slot shape is not closed")
        if not isinstance(value.get("provider"), str) or not isinstance(value.get("model"), str):
            raise _invalid("routing_snapshot slot provider/model must be strings")

    work_state = facts.get("work_state")
    if not isinstance(work_state, dict) or frozenset(work_state) != WORK_STATE_FIELDS:
        raise _invalid("work_state shape is not closed")
    if not _is_sha40_hex(work_state.get("base_sha")):
        raise _invalid("work_state.base_sha must be 40 hex chars")
    if not _is_sha256_hex(work_state.get("diff_sha")):
        raise _invalid("work_state.diff_sha must be 64 hex chars")
    if not isinstance(work_state.get("tests_receipt"), str) or not work_state["tests_receipt"]:
        raise _invalid("work_state.tests_receipt must be a non-empty string")
    if work_state.get("last_verified_sha") is not None and not _is_sha40_hex(
        work_state.get("last_verified_sha")
    ):
        raise _invalid("work_state.last_verified_sha must be 40 hex chars or null")

    injected = payload.get("injected_results")
    if not isinstance(injected, dict) or frozenset(injected) != INJECTED_FIELDS:
        raise _invalid("injected_results shape is not closed")
    observed = injected.get("observed_classifier")
    if not isinstance(observed, dict) or frozenset(observed) != CLASSIFIER_OBSERVED_FIELDS:
        raise _invalid("injected_results.observed_classifier shape is not closed")


def _classifier_integrity(snapshot: dict[str, Any], observed: dict[str, Any], digest: str) -> bool:
    """True iff the observed classifier matches the frozen classifier slot.

    The classifier is the one component the routing decision must never let
    drift: a modified (provider), replaced (model) or digest-drifted (pre/post)
    classifier fails the whole decision closed. The observed values are
    untrusted, so this never raises — any mismatch is simply False.
    """
    frozen = snapshot["classifier"]
    if observed.get("provider") != frozen["provider"]:
        return False
    if observed.get("model") != frozen["model"]:
        return False
    if observed.get("digest_pre") != digest:
        return False
    if observed.get("digest_post") != digest:
        return False
    return True


def _fallback_configured(snapshot: dict[str, Any]) -> bool:
    """True iff the fallback slot is present and its provider/model are set.

    An absent slot, or a slot whose provider or model is empty, is an
    unconfigured route: the decision must not invent one.
    """
    slot = snapshot.get("fallback")
    if slot is None:
        return False
    return bool(slot.get("provider")) and bool(slot.get("model"))


def _classify(
    facts: dict[str, Any], injected: dict[str, Any]
) -> tuple[str, str | None, str | None]:
    """The §6 precedence tree: `(result_status, failure_class, reason_code)`.

    Classifier integrity (a leak/policy check) runs before slot resolution and
    the fallback decision (§5.2: leak checks precede correctness checks). An
    unknown slot request fails closed as a contract failure. The fallback
    decision itself is a pure function of the primary failure class.
    """
    snapshot = facts["routing_snapshot"]
    observed = injected["observed_classifier"]

    if not _classifier_integrity(snapshot, observed, facts["classifier_digest"]):
        return ("blocked", "policy_failure", "POLICY_FAILURE")

    requested = facts["requested_slot"]
    if requested not in SLOT_NAMES:
        return ("blocked", "contract_failure", "PROVIDER_CONTRACT_FAILURE")

    if requested != "fallback":
        return ("fallback_denied", None, None)

    if not _fallback_configured(snapshot):
        return ("no_fallback_route", None, None)

    failure_class = injected["primary_failure_class"]
    if isinstance(failure_class, str) and failure_class in FALLBACK_ALLOWED_CLASSES:
        return ("fallback_allowed", None, None)
    return ("fallback_denied", None, None)


def consume_handoff(command: dict[str, Any]) -> RoutingEvaluation:
    """Consume one handoff request and decide the routing boundary.

    A classifier-drifted or unknown-slot request fails closed (`blocked`) and
    moves the feature to `needs_human` with the seven-write block set. A valid
    `fallback` request whose primary failure is fallback-eligible reports
    `fallback_allowed` and hands the work state on verbatim; an ineligible
    failure reports `fallback_denied`; an unconfigured fallback reports
    `no_fallback_route`. None of the non-blocked outcomes writes or moves the
    feature — the fallback re-invocation is the controller's business.
    """
    _validate_command(command)
    payload = command["input"]
    target = payload["target"]
    facts = payload["authoritative_facts"]
    injected = payload["injected_results"]

    result_status, failure_class, reason_code = _classify(facts, injected)

    receipt = OperationReceipt(
        ReceiptCode.APPLIED, schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA
    )

    if result_status != "blocked":
        handoff_state = (
            dict(facts["work_state"]) if result_status == "fallback_allowed" else None
        )
        return RoutingEvaluation(
            receipt=receipt,
            state_trace=(target["state"],),
            final_state=target["state"],
            final_entity_type=target["entity_type"],
            result_status=result_status,
            failure_class=None,
            handoff_state=handoff_state,
        )

    final_state = REASON_TO_STATE[reason_code]
    return RoutingEvaluation(
        receipt=receipt,
        state_trace=(target["state"], final_state),
        final_state=final_state,
        final_entity_type=target["entity_type"],
        result_status=result_status,
        failure_class=failure_class,
        final_reason_code=reason_code,
        final_reason_owner="feature",
        declared_write_set=BLOCK_WRITE_SET,
        event_trace=(BLOCK_EVENT,),
    )
