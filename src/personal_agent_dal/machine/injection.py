"""`OP-INJECTION-001`: untrusted-content injection boundary (DAL-012, G1).

Contract §威胁模型 (docs/dal/DAL004_威胁模型与权限矩阵_v0.1.md, line 294) names
`api_intake` as a DAL-012 / G1 gate: intake content that is tainted by the
content parser, and requests capabilities beyond the current set, must be
refused with zero capability expansion. The frozen G1 gate owns exactly one
variant — `DAL-T-INJECTION-001/api_intake` — which asserts `POLICY_DENIED` with
an empty write set and the feature left in `intake`.

`evaluate_untrusted_content` is the pure decision. It has no persistence step:
`atomic_write_sets_by_variant.api_intake` is `[]`, so a refusal writes nothing —
not even a receipt row. The G2 carriers (`diff`/`issue`/`readme`/`test_failure`)
arrive while the feature is `coding` and emit a `POLICY_FAILURE` block reason,
which the trusted resolver carries into `block_feature` (BLK-POLICY--coding);
the pure decision itself never moves the feature. The `provider_output` (G4)
carrier is later-wave work and outside this slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-INJECTION-001"
COMMAND_TYPE: Final[str] = "evaluate_untrusted_content"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "policy-engine"

#: The target aggregate is a `feature`, so its receipt is expressed in the
#: feature transition-receipt schema — `engine.RECEIPT_SCHEMAS["feature"]`
#: (§2.1). Defined locally (rather than imported from `engine`) to keep this
#: module self-contained and free of the engine's heavy import graph.
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

#: The closed action shape of the api_intake carrier: ingest the untrusted body,
#: then evaluate the capability change it asks for.
ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "ingest_untrusted_content",
    "evaluate_requested_capability_change",
)

#: The intake carrier's source: content that arrives with the api body while
#: the feature is still in `intake`. This is the G1 boundary.
INTAKE_SOURCE: Final[str] = "api_body"

#: The coding-state carriers (DAL-015/018/019, G2). An untrusted body that
#: arrives *after* the feature is already coding — from a git diff, a GitHub
#: issue, the repository README or a test failure — cannot be refused as an
#: intake; the harm is a blocked feature (`POLICY_FAILURE`), not a zero-write
#: refusal.
CODING_SOURCES: Final[frozenset[str]] = frozenset(
    {"git_diff", "github_issue", "repository_readme", "test_output"}
)

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"content", "current_capabilities", "requested_capabilities"}
)
PARSER_RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source", "status", "tainted"}
)


@dataclass(frozen=True)
class InjectionEvaluation:
    """The complete observable result of the pure injection decision.

    The executor consumes these fields instead of reconstructing state from the
    fixture.  The empty traces are declarations by the production policy; the
    test harness independently guards the filesystem, process, network and DB
    boundaries while this function runs.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, str]
    final_state: str
    final_entity_type: str
    #: The block reason the resolver must carry into `block_feature` when the
    #: carrier arrives while the feature is `coding`. None for the intake
    #: carrier, whose refusal is the operation's own zero-write receipt.
    block_reason: str | None = None
    declared_write_set: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()
    external_effect_trace: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _validate_capabilities(value: Any, *, field: str) -> list[str]:
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
        raise _invalid("wrong injection spec")
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
        or target_state not in ("intake", "coding")
        or not isinstance(target.get("version"), int)
        or isinstance(target.get("version"), bool)
        or target["version"] < 0
    ):
        raise _invalid("invalid injection target")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 2:
        raise _invalid("action sequence must contain exactly two steps")
    if any(not isinstance(step, dict) for step in action_sequence):
        raise _invalid("every action step must be an object")
    if action_sequence[1] != {"command": ACTION_COMMANDS[1]}:
        raise _invalid("unexpected action sequence")
    source = action_sequence[0].get("source")
    if action_sequence[0].get("command") != ACTION_COMMANDS[0]:
        raise _invalid("unexpected action sequence")
    if target_state == "intake":
        if source != INTAKE_SOURCE:
            raise _invalid("unexpected intake content source")
    elif source not in CODING_SOURCES:
        raise _invalid("unexpected coding content source")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    if not isinstance(facts.get("content"), str) or not facts["content"]:
        raise _invalid("content must be a non-empty string")
    _validate_capabilities(
        facts.get("current_capabilities"), field="current_capabilities"
    )
    _validate_capabilities(
        facts.get("requested_capabilities"), field="requested_capabilities"
    )

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list):
        raise _invalid("injected_results must be a list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every parser result must be an object")
        if not frozenset(result) <= PARSER_RESULT_FIELDS:
            raise _invalid("parser result contains an unknown field")
        if result.get("source") != "content_parser":
            raise _invalid("unexpected parser result source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("parser result status must be a non-empty string")
        if "tainted" in result and not isinstance(result["tainted"], bool):
            raise _invalid("parser tainted flag must be boolean")
        if result["status"] == "completed" and "tainted" not in result:
            raise _invalid("completed parser result requires a tainted flag")


def _content_is_tainted(injected_results: list[dict[str, Any]]) -> bool:
    """True when the content parser flags the content as tainted.

    Taint is the trust boundary. A parser result that is missing, or that did
    not reach `completed`, means the content was never positively cleared, so it
    is treated as tainted (fail closed): a taint that cannot be ruled out must
    not be trusted to permit a capability change.
    """
    if len(injected_results) != 1:
        return True
    result = injected_results[0]
    return not (
        result["status"] == "completed" and result.get("tainted") is False
    )


def _is_escalation(current: list[str], requested: list[str]) -> bool:
    """True when the request asks for any capability outside the current set."""
    return bool(set(requested) - set(current))


def evaluate_untrusted_content(command: dict[str, Any]) -> InjectionEvaluation:
    """Refuse or block a capability change whose content the parser has tainted.

    The intake boundary (G1): tainted content asking for capabilities beyond the
    current set is refused `POLICY_DENIED` with zero writes. The coding boundary
    (G2): the same tainted escalation arrives while the feature is already
    coding and reports `POLICY_FAILURE` as its `block_reason`, which the trusted
    resolver carries into `block_feature`. Taint is the trust boundary;
    escalation beyond the current capabilities is the concrete harm both prevent.
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]
    target = payload["target"]

    tainted = _content_is_tainted(payload["injected_results"])
    escalation = _is_escalation(
        facts["current_capabilities"], facts["requested_capabilities"]
    )
    unsafe = tainted and escalation

    receipt = OperationReceipt(
        ReceiptCode.POLICY_DENIED,
        schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
    )
    if target["state"] == "coding":
        # A coding-state carrier that is unsafe blocks the feature: the trusted
        # resolver drives `block_feature` (BLK-POLICY--coding) with this reason.
        # The pure decision reports the block reason; it does not move the
        # feature itself, so `state_trace` stays put and `final_state` is the
        # carrier's starting state.
        return InjectionEvaluation(
            receipt=receipt,
            state_trace=(target["state"], target["state"]),
            final_state=target["state"],
            final_entity_type=target["entity_type"],
            block_reason="POLICY_FAILURE" if unsafe else None,
        )

    # Intake (G1): tainted escalation is refused with zero writes and the
    # feature left where it was.
    return InjectionEvaluation(
        receipt=receipt,
        state_trace=(target["state"], target["state"]),
        final_state=target["state"],
        final_entity_type=target["entity_type"],
    )
