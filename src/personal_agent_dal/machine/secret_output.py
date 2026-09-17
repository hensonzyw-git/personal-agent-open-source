"""`OP-SECRET-OUTPUT-001`: worker secret-output isolation boundary (DAL-017, G2).

The Development Agent Loop worker must never let a credential canary escape in
its own output. ``evaluate_secret_output`` judges the output sanitizer's scan
against a drop-and-revoke redaction policy and surfaces any match as a block
signal. The frozen G2 variants each leak the canary on one channel:

- **stdout** / **stderr** / **patch** / **artifact** / **env** /
  **synthetic_exception** — the canary appears on that channel and must be
  quarantined.

This module is the guard half: it ingests the payload, the redaction policy and
the scanner verdict, and decides whether the output leaks a secret. It is a pure
decision with no persistence step — the ``block_feature`` transition that
consumes the ``POLICY_FAILURE`` result is applied by the trusted resolver and
the deterministic engine, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode

OPERATION_SPEC_ID: Final[str] = "OP-SECRET-OUTPUT-001"
COMMAND_TYPE: Final[str] = "evaluate_secret_output"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "output-sanitizer"

#: The reason a match blocks with, matching the frozen ``BLK-POLICY--coding``
#: transition's ``result_reason_code``.
BLOCK_REASON: Final[str] = "POLICY_FAILURE"

#: The scanner's positive-clean verdict: no fingerprint matched. Anything else
#: (match, missing, or inconclusive) is treated as a leak (fail closed).
CLEAN_STATUS: Final[str] = "no_match"

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

ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "scan_output",
    "quarantine_on_match",
)

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"payload", "redaction_policy", "secret_fingerprints"}
)
RESULT_FIELDS: Final[frozenset[str]] = frozenset({"fingerprint", "source", "status"})

#: The redaction policy the boundary enforces: on a match, drop the output and
#: revoke the run. A different policy is not a G2 secret-output scenario.
REDACTION_POLICY: Final[str] = "drop_and_revoke"


@dataclass(frozen=True)
class SecretOutputEvaluation:
    """The pure verdict of the secret-output check.

    ``conflict`` is True when the scanner matches a fingerprint (or fails to
    confirm none matched); ``reason`` names the block reason the resolver must
    carry into the ``block_feature`` transition.
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
        raise _invalid("wrong secret-output spec")
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
        raise _invalid("invalid secret-output target")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 2:
        raise _invalid("action sequence must contain exactly two steps")
    if any(not isinstance(step, dict) for step in action_sequence):
        raise _invalid("every action step must be an object")
    scan = action_sequence[0]
    if scan.get("command") != ACTION_COMMANDS[0]:
        raise _invalid("unexpected action sequence")
    if not isinstance(scan.get("channel"), str) or not scan["channel"]:
        raise _invalid("channel must be a non-empty string")
    if action_sequence[1] != {"command": ACTION_COMMANDS[1]}:
        raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    if not isinstance(facts.get("payload"), str) or not facts["payload"]:
        raise _invalid("payload must be a non-empty string")
    if facts.get("redaction_policy") != REDACTION_POLICY:
        raise _invalid("unexpected redaction policy")
    if not isinstance(facts.get("secret_fingerprints"), list) or not facts["secret_fingerprints"]:
        raise _invalid("secret_fingerprints must be a non-empty list")
    if any(
        not isinstance(fp, str) or not fp for fp in facts["secret_fingerprints"]
    ):
        raise _invalid("secret_fingerprints must contain non-empty strings")

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list) or not injected_results:
        raise _invalid("injected_results must be a non-empty list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every scanner result must be an object")
        if not frozenset(result) <= RESULT_FIELDS:
            raise _invalid("scanner result contains an unknown field")
        if result.get("source") != "secret_scanner":
            raise _invalid("unexpected scanner result source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("scanner status must be a non-empty string")
        if "fingerprint" in result and (
            not isinstance(result["fingerprint"], str) or not result["fingerprint"]
        ):
            raise _invalid("scanner fingerprint must be a non-empty string")


def _detector_reports_match(injected_results: list[dict[str, Any]]) -> bool:
    """True unless the scanner positively reports no fingerprint matched.

    A scan that is missing, or that did not reach ``no_match``, means the output
    could not be positively cleared, so it is treated as a leak (fail closed).
    """
    if len(injected_results) != 1:
        return True
    return injected_results[0]["status"] != CLEAN_STATUS


def evaluate_secret_output(command: dict[str, Any]) -> SecretOutputEvaluation:
    """Report whether the worker output leaks a secret fingerprint.

    Returns a conflict verdict when the scanner matches a fingerprint (or fails
    to confirm none matched). The verdict is pure: it does not move the feature,
    whose ``block_feature`` transition is the resolver's and engine's step.
    """
    _validate_command(command)
    payload = command["input"]

    if _detector_reports_match(payload["injected_results"]):
        return SecretOutputEvaluation(conflict=True, reason=BLOCK_REASON)
    return SecretOutputEvaluation(conflict=False)
