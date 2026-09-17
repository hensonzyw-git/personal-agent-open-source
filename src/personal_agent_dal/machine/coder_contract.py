"""`OP-CODER-CONTRACT-001`: coder-response contract boundary (DAL-026, G3).

Freeze package `docs/dal/DAL026_ClaudeCode_coder_adapter_合同冻结包_v0.1.md` fixes
the coder consumption rule: a `claude -p` coding run is a *multi-turn, multi-tool*
consumer whose final payload is a multi-file patch bound to the requested base
SHA and context envelope. Unlike the single-shot provider contract
(`OP-PROVIDER-CONTRACT-001`, `max_turns=1` / `max_tool_calls=0`), tool calls and
free prose are the coder's happy path — they are legal here, never a contract
failure on their own.

`consume_coder_stream` is the pure decision. It classifies the untrusted coder
output into one outcome of the frozen vocabulary — `succeeded`, `cancelled`, or a
`(failure_class, reason_code)` pair (`usage_limit / transient / auth /
contract_failure / policy_failure / budget_limit`, no `task_failure`). The
trusted half (command envelope, target, authoritative facts, the `injected_results`
container shape) validates as closed shapes and raises `DalError(INVALID_ARGUMENT)`
on drift. The untrusted values — the stream events, exit code, observed endpoint,
transport evidence and budget state — never raise: malformed output is classified
fail-closed, never a crash.

The function performs no I/O: the actual `claude -p` subprocess, hashing,
persistence and controller wiring belong to `worker/coder_launcher.py` and the
controller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-CODER-CONTRACT-001"
COMMAND_TYPE: Final[str] = "consume_coder_stream"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "coder-adapter"
CONTRACT_VERSION: Final[str] = "dal.coder-response/1.0"

FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: Frozen block write set (§5 of the freeze package) — a classified failure is
#: the operation succeeding at blocking, not a refusal.
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

#: `failure_class` → `reason_code` (storage FEATURE_REASON_CODES) → feature state.
#: There is deliberately NO `task_failure`: deterministic verification is
#: DAL-029's stage; the coder only produces a patch.
FAILURE_TO_REASON: Final[dict[str, str]] = {
    "usage_limit": "USAGE_LIMIT",
    "transient": "TRANSIENT_RETRY_EXHAUSTED",
    "auth": "AUTH_REQUIRED",
    "contract_failure": "PROVIDER_CONTRACT_FAILURE",
    "policy_failure": "POLICY_FAILURE",
    "budget_limit": "BUDGET_LIMIT",
}
REASON_TO_STATE: Final[dict[str, str]] = {
    "USAGE_LIMIT": "blocked_usage",
    "AUTH_REQUIRED": "blocked_auth",
    "TRANSIENT_RETRY_EXHAUSTED": "needs_human",
    "PROVIDER_CONTRACT_FAILURE": "needs_human",
    "POLICY_FAILURE": "needs_human",
    "BUDGET_LIMIT": "needs_human",
}

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

#: The authoritative (trusted) facts of a coder run (§3 of the freeze package).
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "allowed_tools",
        "max_turns",
        "max_wall_seconds",
        "max_patch_bytes",
        "base_sha",
        "requested_context_envelope_sha256",
        "requested_classifier_digest",
        "pinned_endpoint",
        "allowed_paths",
    }
)

#: The untrusted `injected_results` container (§4). The container shape is
#: trusted (drift raises); the values it carries are the coder's own report and
#: are classified, never trusted.
INJECTED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "stream",
        "exit_code",
        "observed_endpoint",
        "classifier_digest_post",
        "redaction_scan",
        "endpoint_policy",
        "sandbox_violation",
        "canary_observed",
        "transport",
        "budget",
    }
)
TRANSPORT_FIELDS: Final[frozenset[str]] = frozenset(
    {"http_status", "account_scoped_429", "provider_error_code", "timed_out", "disconnected"}
)
BUDGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"turns_exhausted", "wall_seconds_exhausted", "patch_bytes_exhausted"}
)

#: §2 event vocabulary: per type, the mandatory and optional fields. A coder
#: stream is multi-turn and multi-tool, so `tool_call` / free prose are legal.
EVENT_REQUIRED_FIELDS: Final[dict[str, frozenset[str]]] = {
    "final": frozenset({"type", "content", "base_sha", "context_envelope_sha256", "changed_files"}),
    "text": frozenset({"type", "content"}),
    "stream_delta": frozenset({"type", "content"}),
    "tool_call": frozenset({"type", "name"}),
    "transport_error": frozenset({"type", "code"}),
    "cancelled": frozenset({"type"}),
}
EVENT_OPTIONAL_FIELDS: Final[dict[str, frozenset[str]]] = {
    "final": frozenset({"turn"}),
    "text": frozenset(),
    "stream_delta": frozenset(),
    "tool_call": frozenset({"turn", "arguments", "arguments_json"}),
    "transport_error": frozenset(),
    "cancelled": frozenset(),
}

_ERROR_CODE_AUTH: Final[frozenset[str]] = frozenset(
    {"authentication_error", "invalid_api_key", "permission_denied"}
)
_ERROR_CODE_USAGE: Final[frozenset[str]] = frozenset(
    {"rate_limit_exceeded", "quota_exceeded", "insufficient_quota"}
)
_ERROR_CODE_TRANSIENT: Final[frozenset[str]] = frozenset(
    {"server_error", "overloaded", "connection_error", "timeout"}
)
_HTTPS_TRANSIENT: Final[frozenset[int]] = frozenset({502, 503, 504})

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class CoderContractEvaluation:
    """The complete observable result of the pure coder decision.

    `result_status` / `failure_class` are the coder-specific outcome pair the
    executor carries alongside the generic trace; the oracle freezes them as
    `expected_result_status` / `expected_failure_class`.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, ...]
    final_state: str
    final_entity_type: str
    result_status: str
    failure_class: str | None
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


def _is_non_empty_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
        and len(value) == len(set(value))
    )


def _path_allowed(path: Any, allowed_paths: list[str]) -> bool:
    if not isinstance(path, str) or not path:
        return False
    for prefix in allowed_paths:
        stripped = prefix.rstrip("/")
        if path == stripped or path.startswith(stripped + "/"):
            return True
    return False


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
        raise _invalid("wrong coder-contract spec")
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
        raise _invalid("coder-contract target must be a feature in coding")

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
    if not _is_non_empty_string_list(facts.get("allowed_tools")):
        raise _invalid("allowed_tools must be a non-empty, duplicate-free list of non-empty strings")
    for key in ("max_turns", "max_wall_seconds", "max_patch_bytes"):
        if not _is_non_negative_int(facts.get(key)):
            raise _invalid(f"{key} must be a non-negative integer")
    if not _is_sha40_hex(facts.get("base_sha")):
        raise _invalid("base_sha must be 40 hex chars")
    if not _is_sha256_hex(facts.get("requested_context_envelope_sha256")):
        raise _invalid("requested_context_envelope_sha256 must be 64 hex chars")
    if not _is_sha256_hex(facts.get("requested_classifier_digest")):
        raise _invalid("requested_classifier_digest must be 64 hex chars")
    if not isinstance(facts.get("pinned_endpoint"), str) or not facts["pinned_endpoint"]:
        raise _invalid("pinned_endpoint must be a non-empty string")
    if not _is_non_empty_string_list(facts.get("allowed_paths")):
        raise _invalid("allowed_paths must be a non-empty, duplicate-free list of non-empty strings")

    injected = payload.get("injected_results")
    if not isinstance(injected, dict) or frozenset(injected) != INJECTED_FIELDS:
        raise _invalid("injected_results shape is not closed")
    if not isinstance(injected.get("stream"), list):
        raise _invalid("injected_results.stream must be a list")
    transport = injected.get("transport")
    if not isinstance(transport, dict) or frozenset(transport) != TRANSPORT_FIELDS:
        raise _invalid("injected_results.transport shape is not closed")
    budget = injected.get("budget")
    if not isinstance(budget, dict) or frozenset(budget) != BUDGET_FIELDS:
        raise _invalid("injected_results.budget shape is not closed")


def _classify_transport(transport: dict[str, Any], exit_code: Any) -> tuple[str, str, str] | None:
    """Classify transport/auth/usage evidence; `None` means clean transport."""
    http = transport.get("http_status")
    account = transport.get("account_scoped_429")
    errcode = transport.get("provider_error_code")

    if http is not None:
        if http in (401, 403):
            return ("blocked", "auth", "AUTH_REQUIRED")
        if http == 429:
            if account is True:
                return ("blocked", "usage_limit", "USAGE_LIMIT")
            if account is False:
                return ("failed", "transient", "TRANSIENT_RETRY_EXHAUSTED")
            return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
        if http in _HTTPS_TRANSIENT:
            return ("failed", "transient", "TRANSIENT_RETRY_EXHAUSTED")
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    if errcode is not None:
        if errcode in _ERROR_CODE_AUTH:
            return ("blocked", "auth", "AUTH_REQUIRED")
        if errcode in _ERROR_CODE_USAGE:
            return ("blocked", "usage_limit", "USAGE_LIMIT")
        if errcode in _ERROR_CODE_TRANSIENT:
            return ("failed", "transient", "TRANSIENT_RETRY_EXHAUSTED")
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    if transport.get("timed_out") or transport.get("disconnected"):
        return ("failed", "transient", "TRANSIENT_RETRY_EXHAUSTED")
    if exit_code != 0:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    return None


def _out_of_scope(stream: list[Any], facts: dict[str, Any]) -> bool:
    """A tool or changed path outside the frozen allowlist (§6 step 5)."""
    for event in stream:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_call":
            name = event.get("name")
            if isinstance(name, str) and name not in facts["allowed_tools"]:
                return True
        if event.get("type") == "final":
            for path in event.get("changed_files", []):
                if not _path_allowed(path, facts["allowed_paths"]):
                    return True
    return False


def _has_conforming_final(stream: list[Any], facts: dict[str, Any]) -> bool:
    for event in stream:
        if not isinstance(event, dict) or event.get("type") != "final":
            continue
        if (
            _is_sha40_hex(event.get("base_sha"))
            and event.get("base_sha") == facts["base_sha"]
            and _is_sha256_hex(event.get("context_envelope_sha256"))
            and event.get("context_envelope_sha256") == facts["requested_context_envelope_sha256"]
            and isinstance(event.get("changed_files"), list)
            and event["changed_files"]
        ):
            return True
    return False


def _classify_stream(stream: list[Any], facts: dict[str, Any]) -> list[str]:
    """Collect contract-failure reasons; `[]` means a conforming stream shape.

    The stream is the untrusted half: malformed events are contract failures,
    never crashes. `succeeded` requires at least one final that binds the
    requested base SHA and context envelope and declares a non-empty diff.
    """
    reasons: list[str] = []
    finals = 0
    for event in stream:
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

        if event_type == "cancelled":
            if len(stream) != 1:
                reasons.append("cancelled event is not the whole stream")
        elif event_type == "tool_call":
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
            turn = event.get("turn")
            if turn is not None and (not isinstance(turn, int) or turn < 1):
                reasons.append("tool call turn is not a positive integer")
        elif event_type in ("text", "stream_delta"):
            if not isinstance(event.get("content"), str):
                reasons.append(f"{event_type} content is not a string")
        elif event_type == "transport_error":
            if not isinstance(event.get("code"), str) or not event["code"]:
                reasons.append("transport error code is not a non-empty string")
            reasons.append("stream terminated by a transport error")
        elif event_type == "final":
            finals += 1
            if not isinstance(event.get("content"), str) or not event["content"]:
                reasons.append("final content is not a non-empty string")
            if not _is_sha40_hex(event.get("base_sha")):
                reasons.append("final base_sha is malformed")
            elif event["base_sha"] != facts["base_sha"]:
                reasons.append("final base_sha drifted from the request")
            if not _is_sha256_hex(event.get("context_envelope_sha256")):
                reasons.append("final context envelope digest is malformed")
            elif event["context_envelope_sha256"] != facts["requested_context_envelope_sha256"]:
                reasons.append("final context envelope drifted from the request")
            if not isinstance(event.get("changed_files"), list):
                reasons.append("final changed_files is not a list")
            elif not event["changed_files"]:
                reasons.append("final declares an empty diff")

    if finals > 1:
        reasons.append("stream carries more than one final event")
    if finals == 0 and stream != [{"type": "cancelled"}]:
        reasons.append("stream carries no final event")
    return reasons


def _classify(
    injected: dict[str, Any], facts: dict[str, Any]
) -> tuple[str, str | None, str | None, list[str]]:
    """The §6 precedence tree: `(result_status, failure_class, reason_code, reasons)`.

    Policy checks run before the cancel / budget / transport / stream checks
    (leak and scope checks precede correctness checks — §5.2). The stream
    correctness reasons are only material for a `contract_failure` outcome.
    """
    stream = injected["stream"]

    if injected["classifier_digest_post"] != facts["requested_classifier_digest"]:
        return ("failed", "policy_failure", "POLICY_FAILURE", [])
    if injected["observed_endpoint"] != facts["pinned_endpoint"]:
        return ("failed", "policy_failure", "POLICY_FAILURE", [])
    if injected["redaction_scan"] == "failed" or injected["endpoint_policy"] == "failed":
        return ("failed", "policy_failure", "POLICY_FAILURE", [])
    if injected["sandbox_violation"] or injected["canary_observed"]:
        return ("failed", "policy_failure", "POLICY_FAILURE", [])
    if _out_of_scope(stream, facts):
        return ("failed", "policy_failure", "POLICY_FAILURE", [])
    if stream == [{"type": "cancelled"}]:
        return ("cancelled", None, None, [])
    if any(injected["budget"].values()) and not _has_conforming_final(stream, facts):
        return ("blocked", "budget_limit", "BUDGET_LIMIT", [])

    transport = _classify_transport(injected["transport"], injected["exit_code"])
    if transport is not None:
        return (*transport, [])

    reasons = _classify_stream(stream, facts)
    if reasons:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE", reasons)
    return ("succeeded", None, None, [])


def consume_coder_stream(command: dict[str, Any]) -> CoderContractEvaluation:
    """Consume one coder stream and decide the contract boundary.

    A conforming multi-tool stream with a bound, non-empty final diff reports the
    feature unchanged (`succeeded`) — the patch's downstream use is the
    controller's business. A lone cancel marker reports `cancelled` with zero
    writes. Any failure shape moves the feature to its block state with the
    seven-write block set; blocking is the operation succeeding at its job.
    """
    _validate_command(command)
    payload = command["input"]
    target = payload["target"]
    facts = payload["authoritative_facts"]
    injected = payload["injected_results"]

    result_status, failure_class, reason_code, reasons = _classify(injected, facts)

    receipt = OperationReceipt(
        ReceiptCode.APPLIED, schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA
    )

    if result_status in ("succeeded", "cancelled"):
        #: The feature never left `coding`: a one-state trace, matching the
        #: frozen oracle's `expected_state_trace == ["coding"]` (a conforming or
        #: cancelled run writes nothing and moves nothing).
        return CoderContractEvaluation(
            receipt=receipt,
            state_trace=(target["state"],),
            final_state=target["state"],
            final_entity_type=target["entity_type"],
            result_status=result_status,
            failure_class=None,
        )

    final_state = REASON_TO_STATE[reason_code]
    return CoderContractEvaluation(
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
        contract_failure_reasons=tuple(reasons),
    )
