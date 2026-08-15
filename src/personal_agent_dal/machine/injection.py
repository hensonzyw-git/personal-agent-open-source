"""`OP-INJECTION-001`: untrusted-content injection boundary (DAL-012, G1).

Contract §威胁模型 (docs/dal/DAL004_威胁模型与权限矩阵_v0.1.md, line 294) names
`api_intake` as a DAL-012 / G1 gate: intake content that is tainted by the
content parser, and requests capabilities beyond the current set, must be
refused with zero capability expansion. The frozen G1 gate owns exactly one
variant — `DAL-T-INJECTION-001/api_intake` — which asserts `POLICY_DENIED` with
an empty write set and the feature left in `intake`.

`evaluate_untrusted_content` is the pure decision. It has no persistence step:
`atomic_write_sets_by_variant.api_intake` is `[]`, so a refusal writes nothing —
not even a receipt row. The positive path (accepting clean content and applying
a real capability change, writing `decision_create` / `transition_receipt`) is
the `diff`/`issue`/`readme`/`test_failure` (G2) and `provider_output` (G4)
carriers, which are later-wave work and outside this slice.
"""

from __future__ import annotations

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

#: The closed action shape of the api_intake carrier: ingest the untrusted body,
#: then evaluate the capability change it asks for.
ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "ingest_untrusted_content",
    "evaluate_requested_capability_change",
)


def _validate_command(command: dict[str, Any]) -> None:
    if command.get("operation_spec_id") != OPERATION_SPEC_ID:
        raise DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail="wrong injection spec")
    if command.get("actor_type") != SERVICE_ACTOR:
        raise DalError(DalErrorCode.ACTOR_NOT_ALLOWED)
    if command.get("evidence_source_type") != EVIDENCE_SOURCE:
        raise DalError(DalErrorCode.SCOPE_DENIED)
    payload = command.get("input")
    if not isinstance(payload, dict):
        raise DalError(DalErrorCode.INVALID_ARGUMENT)
    if not REQUIRED_INPUT_SECTIONS <= frozenset(payload):
        raise DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail="missing input section")
    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list):
        raise DalError(DalErrorCode.INVALID_ARGUMENT)
    commands = tuple(
        step.get("command") for step in action_sequence if isinstance(step, dict)
    )
    if commands != ACTION_COMMANDS:
        raise DalError(
            DalErrorCode.INVALID_ARGUMENT, internal_detail="unexpected action sequence"
        )


def _content_is_tainted(injected_results: list[dict[str, Any]]) -> bool:
    """True when the content parser flags the content as tainted.

    Taint is the trust boundary. A parser result that is missing, or that did
    not reach `completed`, means the content was never positively cleared, so it
    is treated as tainted (fail closed): a taint that cannot be ruled out must
    not be trusted to permit a capability change.
    """
    for result in injected_results:
        if result.get("source") == "content_parser":
            if result.get("status") == "completed":
                return bool(result.get("tainted"))
            return True
    return True


def _is_escalation(current: list[str], requested: list[str]) -> bool:
    """True when the request asks for any capability outside the current set."""
    return bool(set(requested) - set(current))


def evaluate_untrusted_content(command: dict[str, Any]) -> OperationReceipt:
    """Refuse a capability change whose content the parser has tainted.

    The one boundary this slice proves: tainted content asking for capabilities
    beyond the current set is refused `POLICY_DENIED` with zero writes. Taint is
    the trust boundary; escalation beyond the current capabilities is the
    concrete harm the refusal prevents. Both hold in the frozen `api_intake`
    variant (production_credentials + unrestricted_repository_write requested
    over the current read_workspace).
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]

    tainted = _content_is_tainted(payload["injected_results"])
    escalation = _is_escalation(
        facts["current_capabilities"], facts["requested_capabilities"]
    )

    if tainted and escalation:
        # The frozen api_intake variant. Refused with zero writes.
        return OperationReceipt(
            ReceiptCode.POLICY_DENIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        )

    # Everything else is outside this slice's proven surface. The positive path
    # — accepting clean content and applying a real capability change — is the
    # G2/G4 carrier work. Fail closed rather than fabricate an unproven allow
    # rule: no frozen oracle can observe an unexpected APPLIED.
    return OperationReceipt(
        ReceiptCode.POLICY_DENIED,
        schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
    )
