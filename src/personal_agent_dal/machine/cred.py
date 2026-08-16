"""`OP-CRED-001`: worker credential-read boundary (DAL-017, G2).

The Development Agent Loop launches the provider adapter as a child with a
closed credential surface: a dedicated stdin pipe for credentials, an
environment allowlist, and a canary planted to detect any child attempt to read
a credential from another surface. ``evaluate_credential_boundary`` judges the
child's boundary probe against those rules and surfaces any canary observation
as a block signal. The frozen G2 variants each probe one forbidden surface:

- **env** / **fd** / **keychain** / **log** / **parent_process** / **proxy** /
  **dependency_hook** — the child is observed reading the credential canary from
  a surface that must stay closed.

This module is the guard half: it ingests the boundary facts and the probe
verdict, and decides whether a credential surface was breached. It is a pure
decision with no persistence step — the ``block_feature`` transition that
consumes the ``POLICY_FAILURE`` result is applied by the trusted resolver and
the deterministic engine, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode

OPERATION_SPEC_ID: Final[str] = "OP-CRED-001"
COMMAND_TYPE: Final[str] = "evaluate_credential_boundary"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "sandbox-controller"

#: The reason a canary observation blocks with, matching the frozen
#: ``BLK-POLICY--coding`` transition's ``result_reason_code``.
BLOCK_REASON: Final[str] = "POLICY_FAILURE"

#: The probe's positive-clean verdict: no canary observed. Anything else
#: (observed, missing, or inconclusive) is treated as a breach (fail closed).
CLEAN_STATUS: Final[str] = "canary_not_observed"

#: The credential surfaces that must stay closed to the child. A canary observed
#: from any of them is a breach; probing them is the harm the boundary prevents.
FORBIDDEN_SURFACES: Final[frozenset[str]] = frozenset(
    {"env", "fd", "keychain", "log", "parent_process", "proxy", "dependency_hook"}
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

ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "launch_child",
    "inspect_child_boundary",
)

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "canary_fingerprint",
        "child_environment_allowlist",
        "credential_delivery",
        "gate_profile",
        "parent_environment_contains_secret",
        "surface_probed",
    }
)
RESULT_FIELDS: Final[frozenset[str]] = frozenset({"source", "status", "surface"})


@dataclass(frozen=True)
class CredEvaluation:
    """The pure verdict of the credential-boundary check.

    ``conflict`` is True when the canary is observed from a forbidden surface;
    ``reason`` names the block reason the resolver must carry into the
    ``block_feature`` transition.
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
        raise _invalid("wrong credential-boundary spec")
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
        raise _invalid("invalid credential-boundary target")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 2:
        raise _invalid("action sequence must contain exactly two steps")
    if any(not isinstance(step, dict) for step in action_sequence):
        raise _invalid("every action step must be an object")
    launch = action_sequence[0]
    if launch.get("command") != ACTION_COMMANDS[0]:
        raise _invalid("unexpected action sequence")
    if not isinstance(launch.get("argv"), list) or not launch["argv"]:
        raise _invalid("argv must be a non-empty list")
    if any(not isinstance(part, str) or not part for part in launch["argv"]):
        raise _invalid("argv must contain non-empty strings")
    if action_sequence[1] != {"command": ACTION_COMMANDS[1]}:
        raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    for field in ("canary_fingerprint", "credential_delivery", "gate_profile", "surface_probed"):
        if not isinstance(facts.get(field), str) or not facts[field]:
            raise _invalid(f"{field} must be a non-empty string")
    if not isinstance(facts.get("child_environment_allowlist"), list):
        raise _invalid("child_environment_allowlist must be a list")
    if any(not isinstance(key, str) or not key for key in facts["child_environment_allowlist"]):
        raise _invalid("child_environment_allowlist must contain non-empty strings")
    if not isinstance(facts.get("parent_environment_contains_secret"), bool):
        raise _invalid("parent_environment_contains_secret must be boolean")

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list) or not injected_results:
        raise _invalid("injected_results must be a non-empty list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every probe result must be an object")
        if not frozenset(result) <= RESULT_FIELDS:
            raise _invalid("probe result contains an unknown field")
        if result.get("source") != "malicious_child_probe":
            raise _invalid("unexpected probe result source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("probe status must be a non-empty string")
        if "surface" in result and (
            not isinstance(result["surface"], str) or not result["surface"]
        ):
            raise _invalid("probe surface must be a non-empty string")


def _detector_reports_breach(injected_results: list[dict[str, Any]]) -> bool:
    """True unless the probe positively reports no canary observed.

    A probe that is missing, or that did not reach ``canary_not_observed``, means
    the credential surface could not be positively verified, so it is treated as
    a breach (fail closed).
    """
    if len(injected_results) != 1:
        return True
    return injected_results[0]["status"] != CLEAN_STATUS


def _facts_show_breach(facts: dict[str, Any]) -> bool:
    """True when the probed surface is one that must stay closed.

    Defence in depth: even a probe that misreports ``canary_not_observed``
    cannot rescue a child that probed a forbidden credential surface.
    """
    return facts["surface_probed"] in FORBIDDEN_SURFACES


def evaluate_credential_boundary(command: dict[str, Any]) -> CredEvaluation:
    """Report whether the child breached a closed credential surface.

    Returns a conflict verdict when the probe observes the canary from a
    forbidden surface (or fails to confirm it did not) or when the probed
    surface is one that must stay closed. The verdict is pure: it does not move
    the feature, whose ``block_feature`` transition is the resolver's and
    engine's step.
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]

    if _detector_reports_breach(payload["injected_results"]) or _facts_show_breach(facts):
        return CredEvaluation(conflict=True, reason=BLOCK_REASON)
    return CredEvaluation(conflict=False)
