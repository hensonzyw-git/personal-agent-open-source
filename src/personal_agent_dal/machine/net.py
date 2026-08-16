"""`OP-NET-001`: worker network-deny boundary (DAL-017, G2).

The Development Agent Loop worker runs under an isolated profile with an empty
destination allowlist and no inbound listeners. ``evaluate_network_request``
judges a network request against that profile and surfaces any unauthorised
attempt as a block signal. The frozen G2 variants each attempt one forbidden
request:

- **finance** — outbound to a finance host not in the (empty) allowlist;
- **lan** — outbound to a LAN address not in the allowlist;
- **personal_agent_prod** — outbound to the production host not in the allowlist;
- **inbound_listener** — an inbound bind while listeners are forbidden.

This module is the guard half: it ingests the request and the network-policy
verdict, and decides whether the request is authorised. It is a pure decision
with no persistence step — the ``block_feature`` transition that consumes the
``POLICY_FAILURE`` result is applied by the trusted resolver and the
deterministic engine, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode

OPERATION_SPEC_ID: Final[str] = "OP-NET-001"
COMMAND_TYPE: Final[str] = "evaluate_network_request"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "network-policy"

#: The reason a denied request blocks with, matching the frozen
#: ``BLK-POLICY--coding`` transition's ``result_reason_code``.
BLOCK_REASON: Final[str] = "POLICY_FAILURE"

#: The policy's positive-clean verdict: the request was allowed. Anything else
#: (denied, missing, or timed out) is treated as a denial (fail closed).
CLEAN_STATUS: Final[str] = "allowed"

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

#: The single-step action shape: authorise the request carried on the step.
ACTION_COMMANDS: Final[tuple[str, ...]] = ("authorize_network_request",)

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"allowed_destinations", "inbound_listeners_allowed", "profile"}
)
RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"credential_bytes_sent", "source", "status"}
)
REQUEST_FIELDS_OUTBOUND: Final[frozenset[str]] = frozenset(
    {"direction", "host", "port"}
)
REQUEST_FIELDS_INBOUND: Final[frozenset[str]] = frozenset(
    {"bind", "direction", "port"}
)


@dataclass(frozen=True)
class NetEvaluation:
    """The pure verdict of the network-request check.

    ``conflict`` is True when the request is denied or outside the profile's
    allowlist; ``reason`` names the block reason the resolver must carry into
    the ``block_feature`` transition.
    """

    conflict: bool
    reason: str | None = None


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


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
        raise _invalid("wrong network spec")
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
        or not isinstance(target.get("version"), int)
        or isinstance(target.get("version"), bool)
        or target["version"] < 0
    ):
        raise _invalid("invalid network target")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 1:
        raise _invalid("action sequence must contain exactly one step")
    step = action_sequence[0]
    if not isinstance(step, dict) or step.get("command") != ACTION_COMMANDS[0]:
        raise _invalid("unexpected action sequence")
    request = step.get("request")
    if not isinstance(request, dict):
        raise _invalid("request must be an object")
    direction = request.get("direction")
    if direction == "outbound":
        if frozenset(request) != REQUEST_FIELDS_OUTBOUND:
            raise _invalid("outbound request shape is not closed")
        if not isinstance(request.get("host"), str) or not request["host"]:
            raise _invalid("host must be a non-empty string")
    elif direction == "inbound":
        if frozenset(request) != REQUEST_FIELDS_INBOUND:
            raise _invalid("inbound request shape is not closed")
        if not isinstance(request.get("bind"), str) or not request["bind"]:
            raise _invalid("bind must be a non-empty string")
    else:
        raise _invalid("unknown request direction")
    if not isinstance(request.get("port"), int) or isinstance(request["port"], bool):
        raise _invalid("port must be an integer")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    if not isinstance(facts.get("allowed_destinations"), list):
        raise _invalid("allowed_destinations must be a list")
    if any(not isinstance(host, str) or not host for host in facts["allowed_destinations"]):
        raise _invalid("allowed_destinations must contain non-empty strings")
    if not isinstance(facts.get("inbound_listeners_allowed"), bool):
        raise _invalid("inbound_listeners_allowed must be boolean")
    if not isinstance(facts.get("profile"), str) or not facts["profile"]:
        raise _invalid("profile must be a non-empty string")

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list) or not injected_results:
        raise _invalid("injected_results must be a non-empty list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every network-policy result must be an object")
        if not frozenset(result) <= RESULT_FIELDS:
            raise _invalid("network-policy result contains an unknown field")
        if result.get("source") != "network_policy":
            raise _invalid("unexpected network-policy result source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("network-policy status must be a non-empty string")


def _detector_reports_denial(injected_results: list[dict[str, Any]]) -> bool:
    """True unless the network policy positively reports the request allowed.

    A verdict that is missing, or that did not reach ``allowed``, means the
    request was not authorised, so it is treated as a denial (fail closed).
    """
    if len(injected_results) != 1:
        return True
    return injected_results[0]["status"] != CLEAN_STATUS


def _facts_show_denial(facts: dict[str, Any], request: dict[str, Any]) -> bool:
    """True when the request is outside the profile's allowlist.

    Defence in depth: even a policy that misreports ``allowed`` cannot authorise
    an inbound bind when listeners are forbidden, or an outbound host that is
    not in the destination allowlist.
    """
    if request["direction"] == "inbound":
        return not facts["inbound_listeners_allowed"]
    return request["host"] not in facts["allowed_destinations"]


def evaluate_network_request(command: dict[str, Any]) -> NetEvaluation:
    """Report whether a network request is authorised under the worker profile.

    Returns a conflict verdict when the network policy denies the request (or
    fails to confirm it was allowed) or when the request is outside the
    allowlist. The verdict is pure: it does not move the feature, whose
    ``block_feature`` transition is the resolver's and engine's step.
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]
    request = payload["action_sequence"][0]["request"]

    if _detector_reports_denial(payload["injected_results"]) or _facts_show_denial(
        facts, request
    ):
        return NetEvaluation(conflict=True, reason=BLOCK_REASON)
    return NetEvaluation(conflict=False)
