"""`OP-PROVIDER-CONTRACT-001`: provider-response contract boundary (DAL-021, G4).

Freeze package §3.4 (docs/dal/DAL021-024_合同冻结包_v0.1.md) fixes the
single-shot consumption rule: a Codex turn dispatched with `max_turns=1` and
`max_tool_calls=0` must land as exactly one non-empty `final` event bound to
the requested context envelope. Anything else — a tool call, prose mixed with
a tool call, free prose (text / stream_delta) ahead of the final, a second
assistant turn, a malformed `arguments_json`, a stream that dies in a
transport error, multiple finals, an empty payload, or a final whose context
envelope is missing, malformed or drifted — is a provider contract failure
that fails closed *regardless of how valid the rest of the payload looks*.

The frozen `DAL-T-PROVIDER-CONTRACT-001` oracle freezes exactly that outcome
for all eight adversarial variants: the feature moves `coding → needs_human`
via BLK-CONTRACT--coding (`feature.blocked`, reason
`PROVIDER_CONTRACT_FAILURE`, reason owner `feature`) with the full seven-write
block set and an `APPLIED` receipt — blocking is the operation succeeding at
its job, not a refusal.

`consume_provider_stream` is the pure decision. The untrusted half is the
stream itself (`injected_results`): malformed events there are *contract
failures*, never `INVALID_ARGUMENT` — the provider's output cannot crash the
boundary, it can only fail it. The trusted half (command envelope, target,
authoritative facts, action shape) validates as closed shapes and raises
`DalError(INVALID_ARGUMENT)` on drift. The function performs no I/O: hashing,
persistence and the actual adapter subprocess belong to the controller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-PROVIDER-CONTRACT-001"
COMMAND_TYPE: Final[str] = "consume_provider_stream"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "provider-adapter"
CONTRACT_VERSION: Final[str] = "dal.provider-response/1.0"

#: The target aggregate is a `feature`; its transition receipt schema is the
#: frozen `dal.transition-receipt/1.0` (engine.RECEIPT_SCHEMAS["feature"]).
#: Kept local so this module stays free of the engine's import graph.
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: §3.4 single-shot consumption caps. The authoritative facts may describe a
#: wider harness allowance; this boundary consumes a single-shot turn, so the
#: effective ceiling is the stricter of the two — zero tool calls, one turn.
SINGLE_SHOT_MAX_TURNS: Final[int] = 1
SINGLE_SHOT_MAX_TOOL_CALLS: Final[int] = 0

#: BLK-CONTRACT--coding atomic write set, frozen in the transition registry.
BLOCK_WRITE_SET: Final[tuple[str, ...]] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
    "decision_create",
    "decision_projection",
    "notification_outbox",
)

BLOCK_REASON: Final[str] = "PROVIDER_CONTRACT_FAILURE"
BLOCK_EVENT: Final[str] = "feature.blocked"
BLOCK_STATE: Final[str] = "needs_human"

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

#: The closed action shape of the consumption step: one command, pinned to the
#: frozen response contract version.
ACTION_FIELDS: Final[frozenset[str]] = frozenset({"command", "contract_version"})

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)

FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "allowed_tool_names",
        "maximum_tool_calls",
        "requested_context_envelope_sha256",
        "require_single_final",
    }
)

#: The closed event vocabulary of a provider stream (§3.2/§3.4): per type, the
#: mandatory fields and the optional fields. An event's field set must lie
#: between the two — a missing mandatory field or a field outside the union is
#: an unknown shape on the untrusted side, i.e. a contract failure, not a crash.
#: `context_envelope_sha256` is mandatory on `final`: §3.4 context_drift fails
#: closed when the binding cannot be proven, and a final that omits the field
#: proves nothing, so absence is drift — never a silent pass. `turn` stays
#: optional because the frozen `dal.provider-response/1.0` envelope carries the
#: turn at the transport layer and the frozen fixtures legitimately omit it.
EVENT_REQUIRED_FIELDS: Final[dict[str, frozenset[str]]] = {
    "final": frozenset({"type", "content", "context_envelope_sha256"}),
    "text": frozenset({"type", "content"}),
    "stream_delta": frozenset({"type", "content"}),
    "tool_call": frozenset({"type", "name"}),
    "transport_error": frozenset({"type", "code"}),
}
EVENT_OPTIONAL_FIELDS: Final[dict[str, frozenset[str]]] = {
    "final": frozenset({"turn"}),
    "text": frozenset(),
    "stream_delta": frozenset(),
    "tool_call": frozenset({"turn", "arguments", "arguments_json"}),
    "transport_error": frozenset(),
}

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class ProviderContractEvaluation:
    """The complete observable result of the pure contract decision.

    The executor consumes these fields instead of reconstructing state from
    the fixture. `contract_failure_reasons` is diagnostic only — the oracle
    judges the block reason, the write set and the traces, never the reason
    list's wording.
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
    contract_failure_reasons: tuple[str, ...] = ()


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
        raise _invalid("wrong provider-contract spec")
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
        raise _invalid("provider-contract target must be a feature in coding")

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
    tool_names = facts.get("allowed_tool_names")
    if not isinstance(tool_names, list) or any(
        not isinstance(name, str) or not name for name in tool_names
    ):
        raise _invalid("allowed_tool_names must be a list of non-empty strings")
    if len(tool_names) != len(set(tool_names)):
        raise _invalid("allowed_tool_names must not contain duplicates")
    if not _is_non_negative_int(facts.get("maximum_tool_calls")):
        raise _invalid("maximum_tool_calls must be a non-negative integer")
    if not _is_sha256_hex(facts.get("requested_context_envelope_sha256")):
        raise _invalid("requested_context_envelope_sha256 must be 64 hex chars")
    if not isinstance(facts.get("require_single_final"), bool):
        raise _invalid("require_single_final must be boolean")

    if not isinstance(payload.get("injected_results"), list):
        raise _invalid("injected_results must be a list")


def _classify_stream(
    events: list[Any], facts: dict[str, Any]
) -> tuple[str, list[str]]:
    """Classify the untrusted stream per §3.4; returns `(verdict, reasons)`.

    The verdict is ``"conforming"`` only when the stream is exactly one
    non-empty final carrying a context envelope that matches the request,
    with no tool calls, no prose, no extra turns and no transport error.
    Every other shape — including shapes too malformed to interpret — is
    ``"contract_failure"``. The provider's output never raises and is never
    silently repaired: what cannot be parsed fails the contract.
    """
    reasons: list[str] = []
    finals: list[dict[str, Any]] = []
    tool_calls = 0

    for event in events:
        if not isinstance(event, dict):
            reasons.append("stream event is not an object")
            continue
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in EVENT_REQUIRED_FIELDS:
            reasons.append("stream event has an unknown type")
            continue
        if EVENT_REQUIRED_FIELDS[event_type] - frozenset(event):
            reasons.append(f"{event_type} event is missing a mandatory field")
            continue
        allowed_fields = (
            EVENT_REQUIRED_FIELDS[event_type] | EVENT_OPTIONAL_FIELDS[event_type]
        )
        if frozenset(event) - allowed_fields:
            reasons.append(f"{event_type} event carries an unknown field")
            continue

        turn = event.get("turn") if "turn" in event else 1
        if not _is_non_negative_int(turn) or turn < 1 or turn > SINGLE_SHOT_MAX_TURNS:
            reasons.append("stream exceeds the single-shot turn ceiling")

        if event_type == "tool_call":
            tool_calls += 1
            has_arguments = "arguments" in event
            has_arguments_json = "arguments_json" in event
            if has_arguments == has_arguments_json:
                reasons.append("tool call carries neither or both argument forms")
            elif has_arguments and not isinstance(event["arguments"], dict):
                reasons.append("tool call arguments are not an object")
            elif has_arguments_json:
                if not isinstance(event["arguments_json"], str):
                    reasons.append("tool call arguments_json is not a string")
                else:
                    try:
                        parsed = json.loads(event["arguments_json"])
                    except ValueError:
                        reasons.append("tool call arguments_json is not valid JSON")
                    else:
                        if not isinstance(parsed, dict):
                            reasons.append("tool call arguments_json is not an object")
            if event.get("name") not in facts["allowed_tool_names"]:
                reasons.append("tool call names a tool outside the allowed set")

        elif event_type in ("text", "stream_delta"):
            if not isinstance(event.get("content"), str):
                reasons.append(f"{event_type} content is not a string")
            #: §3.4 prose 混正文: any model free-prose text outside the final
            #: structured payload fails closed, even when the content is a
            #: well-formed string and the final is otherwise valid. A clean
            #: final never launders prose that preceded it.
            reasons.append(f"{event_type} is free prose outside the final payload")

        elif event_type == "transport_error":
            if not isinstance(event.get("code"), str) or not event["code"]:
                reasons.append("transport error code is not a non-empty string")
            #: §3.4 half stream / 断流: a transport_error event means the stream
            #: died mid-flight regardless of whether its code is well-formed.
            reasons.append("stream terminated by a transport error")

        elif event_type == "final":
            if not isinstance(event.get("content"), str):
                reasons.append("final content is not a string")
            elif not event["content"]:
                reasons.append("final payload is empty")
            #: The field is mandatory (EVENT_REQUIRED_FIELDS), so reaching here
            #: guarantees its presence; only its value is judged.
            if not _is_sha256_hex(event["context_envelope_sha256"]):
                reasons.append("final context envelope digest is malformed")
            elif (
                event["context_envelope_sha256"]
                != facts["requested_context_envelope_sha256"]
            ):
                reasons.append("final context envelope drifted from the request")
            finals.append(event)

    #: §3.4 single-shot caps tool calls at zero; `maximum_tool_calls` only
    #: widens the harness allowance, never narrows it below the operation's own
    #: `SINGLE_SHOT_MAX_TOOL_CALLS`, so the effective ceiling is the constant.
    if tool_calls > SINGLE_SHOT_MAX_TOOL_CALLS:
        reasons.append("single-shot consumption permits no tool calls")
    if facts["require_single_final"] and len(finals) != 1:
        reasons.append(
            "stream must carry exactly one final event" if finals
            else "stream carries no final event"
        )
    elif not facts["require_single_final"] and not finals:
        reasons.append("stream carries no final event")

    return ("contract_failure" if reasons else "conforming", reasons)


def consume_provider_stream(command: dict[str, Any]) -> ProviderContractEvaluation:
    """Consume one provider stream and decide the contract boundary.

    A conforming single-shot stream continues the operation (this pure
    decision reports the feature unchanged — the payload's downstream use is
    the controller's business, and no frozen oracle covers that path). Any
    violation moves the decision to BLK-CONTRACT--coding: `coding →
    needs_human`, `feature.blocked`, reason `PROVIDER_CONTRACT_FAILURE` owned
    by `feature`, with the frozen seven-write block set.
    """
    _validate_command(command)
    payload = command["input"]
    target = payload["target"]
    facts = payload["authoritative_facts"]

    verdict, reasons = _classify_stream(payload["injected_results"], facts)

    if verdict == "conforming":
        return ProviderContractEvaluation(
            receipt=OperationReceipt(
                ReceiptCode.APPLIED,
                schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
            ),
            state_trace=(target["state"], target["state"]),
            final_state=target["state"],
            final_entity_type=target["entity_type"],
        )

    return ProviderContractEvaluation(
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
        contract_failure_reasons=tuple(reasons),
    )
