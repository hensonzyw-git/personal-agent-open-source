"""`OP-GH-EVENT-001`: GitHub webhook/poll intake boundary (DAL-014/015, G2).

Contract §威胁模型 (docs/dal/DAL004_威胁模型与权限矩阵_v0.1.md) and the frozen
operation-spec registry name ``accept_github_intake`` as the DAL-014/015 G2
gate: a GitHub event is judged against the single-repo permission matrix before
any job, lease or external effect exists. Five conditions refuse, each with zero
writes and the feature left where it was:

- **fork** — the event's head repository differs from its target repository
  (content originated in a repository the installation does not control);
- **unknown_repo** — the target repository is outside the allowed set;
- **unknown_sender** — the sender installation is outside the allowed set;
- **edited_event** — the event action is outside the accepted set;
- **replay_delivery** — the delivery id was already seen.

`accept_github_intake` is a pure decision. It has no persistence step: every
frozen variant's `atomic_write_sets_by_variant` entry is `[]`, so a refusal
writes nothing — not even a receipt row. The positive path (an accepted event
spawning the intake → plan work) is not frozen in G2 and is out of this slice.

This module mirrors `machine/injection.py` in shape: a closed input validator,
then a deterministic refusal predicate. It imports nothing but `errors` and
`receipt`, so the pure-policy dependency surface stays exactly as narrow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-GH-EVENT-001"
COMMAND_TYPE: Final[str] = "accept_github_intake"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "github-intake"

#: The target aggregate is a `feature`, so its receipt is expressed in the
#: feature transition-receipt schema — the frozen oracle asserts
#: `dal.transition-receipt/1.0`. Defined locally (rather than imported from
#: `engine`) to keep this module free of the engine's heavy import graph.
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

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

#: The closed action shape of the intake carrier: verify the delivery came from
#: GitHub, authorise the repository and sender, then deduplicate the delivery.
ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "verify_webhook_signature",
    "authorize_repository_and_sender",
    "deduplicate_delivery",
)

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "accepted_actions",
        "allowed_repository_ids",
        "allowed_sender_ids",
        "envelope",
        "seen_delivery_ids",
    }
)
ENVELOPE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "action",
        "delivery_id",
        "event",
        "head_repository_id",
        "repository_id",
        "sender_id",
    }
)
SIGNATURE_RESULT_FIELDS: Final[frozenset[str]] = frozenset({"source", "status"})
VALID_SIGNATURE_STATUS: Final[str] = "signature_valid"


@dataclass(frozen=True)
class GithubEventEvaluation:
    """The complete observable result of the pure intake decision.

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


def _validate_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise _invalid(f"{field} must be a list")
    if any(not isinstance(item, str) or not item for item in value):
        raise _invalid(f"{field} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise _invalid(f"{field} must not contain duplicates")
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
        raise _invalid("wrong intake spec")
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
        or target.get("state") != "intake"
        or not isinstance(target.get("version"), int)
        or isinstance(target.get("version"), bool)
        or target["version"] < 0
    ):
        raise _invalid("invalid intake target")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 3:
        raise _invalid("action sequence must contain exactly three steps")
    if any(not isinstance(step, dict) for step in action_sequence):
        raise _invalid("every action step must be an object")
    expected = [
        {"command": ACTION_COMMANDS[0]},
        {"command": ACTION_COMMANDS[1]},
        {"command": ACTION_COMMANDS[2]},
    ]
    if action_sequence != expected:
        raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    _validate_string_list(facts.get("accepted_actions"), field="accepted_actions")
    _validate_string_list(
        facts.get("allowed_repository_ids"), field="allowed_repository_ids"
    )
    _validate_string_list(facts.get("allowed_sender_ids"), field="allowed_sender_ids")
    _validate_string_list(facts.get("seen_delivery_ids"), field="seen_delivery_ids")

    envelope = facts.get("envelope")
    if not isinstance(envelope, dict) or frozenset(envelope) != ENVELOPE_FIELDS:
        raise _invalid("envelope shape is not closed")
    for field in ENVELOPE_FIELDS:
        value = envelope.get(field)
        if not isinstance(value, str) or not value:
            raise _invalid(f"envelope {field} must be a non-empty string")

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list) or not injected_results:
        raise _invalid("injected_results must be a non-empty list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every signature result must be an object")
        if not frozenset(result) <= SIGNATURE_RESULT_FIELDS:
            raise _invalid("signature result contains an unknown field")
        if result.get("source") != "github":
            raise _invalid("unexpected signature result source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("signature result status must be a non-empty string")


def _signature_is_valid(injected_results: list[dict[str, Any]]) -> bool:
    """True only when a single, completed GitHub signature check says valid.

    A delivery that is missing its signature evidence, or whose evidence does
    not reach the closed ``signature_valid`` status, is never positively
    cleared — fail closed.
    """
    if len(injected_results) != 1:
        return False
    result = injected_results[0]
    return result["status"] == VALID_SIGNATURE_STATUS


def _refused(target: dict[str, Any]) -> GithubEventEvaluation:
    return GithubEventEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.POLICY_DENIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(target["state"], target["state"]),
        final_state=target["state"],
        final_entity_type=target["entity_type"],
    )


def accept_github_intake(command: dict[str, Any]) -> GithubEventEvaluation:
    """Refuse a GitHub event that fails the single-repo permission matrix.

    The three actions run in order and the first failure refuses the delivery
    with ``POLICY_DENIED`` and zero writes. Every frozen G2 variant fails one of
    them; an event that passes all three is accepted, which is not frozen in G2
    and is deliberately not modelled as a state change here.
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]
    envelope = facts["envelope"]
    target = payload["target"]

    # 1. verify_webhook_signature.
    if not _signature_is_valid(payload["injected_results"]):
        return _refused(target)

    # 2. authorize_repository_and_sender. The allowlist checks precede the fork
    #    check so an out-of-allowlist repository is reported as unknown, not as
    #    a fork: both are refusals, but the classification is a safety property
    #    (a fork of an allowed repository is a different exposure than a
    #    repository the installation does not own at all).
    if envelope["repository_id"] not in facts["allowed_repository_ids"]:
        return _refused(target)
    if envelope["sender_id"] not in facts["allowed_sender_ids"]:
        return _refused(target)
    if envelope["action"] not in facts["accepted_actions"]:
        return _refused(target)
    if envelope["head_repository_id"] != envelope["repository_id"]:
        return _refused(target)

    # 3. deduplicate_delivery.
    if envelope["delivery_id"] in facts["seen_delivery_ids"]:
        return _refused(target)

    return GithubEventEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(target["state"], target["state"]),
        final_state=target["state"],
        final_entity_type=target["entity_type"],
    )
