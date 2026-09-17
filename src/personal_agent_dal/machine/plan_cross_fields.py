"""`OP-PLAN-XFIELD-001`: plan cross-field validation boundary (DAL-022, G3).

Freeze package §4 (docs/dal/DAL021-024_合同冻结包_v0.1.md) closes the business
content of `dal.plan-artifact/1.0`. The controller re-derives six cross-field
properties from the plan structure itself and refuses the plan when any one is
violated:

- **identity** — `feature_id` and `base_sha` must equal the input manifest's.
- **path non-overlap** — across tasks, no two `allowed_paths` may be equal or
  prefix-contained (a directory `src/` overlapping file `src/auth.py` or
  directory `src/auth/`). The overlap check uses the raw `path` string plus the
  `path_type` marker, exactly as §4 defines it.
- **order** — `order` must run `1..n` with no gap.
- **dependency** — a `dependency_task_ids` entry must name an *earlier* task.
- **unknown verification** — every `acceptance_criteria[].verification_ids[]`
  must resolve to a verification id present in the input manifest's repo-rules
  registry.
- **digest** — `allowed_paths_sha256` must equal `SHA-256(RFC 8785 JCS(A))`
  where `A` is the flattened, order-sorted list of every task's
  `{path, path_type}` object, un-deduplicated and un-reordered.

A plan that passes all six moves the feature `planning → awaiting_plan_review`
via `plan.ready` with the four-write accept set. Any violation — or a plan
malformed enough that a rule cannot be computed — moves the feature
`planning → needs_human` via `feature.blocked` (`PROVIDER_CONTRACT_FAILURE`,
reason owner `feature`) with the full seven-write block set. Blocking is the
operation succeeding at its job, not a refusal.

`validate_plan_cross_fields` is the pure decision. The trusted half (command
envelope, target, facts, action shape, injected-result top-level shape) raises
`DalError(INVALID_ARGUMENT)` on drift; the plan payload is the planner's output
and fails closed as a contract block, never a crash. The digest is recomputed
with the same canonicaliser the registry and the generator share
(`personal_agent_dal.machine.registry.jcs_sha256`). No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine.registry import jcs_sha256
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-PLAN-XFIELD-001"
COMMAND_TYPE: Final[str] = "validate_plan_cross_fields"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "planner"

#: The target aggregate is a `feature`; its transition receipt schema is the
#: frozen `dal.transition-receipt/1.0` (engine.RECEIPT_SCHEMAS["feature"]).
#: Kept local so this module stays free of the engine's import graph.
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: The four-write accept set (shared by the verify/fix paths) and the full
#: seven-write block set, frozen in the transition registry.
BASE_WRITE_SET: Final[tuple[str, ...]] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
)
BLOCK_WRITE_SET: Final[tuple[str, ...]] = BASE_WRITE_SET + (
    "decision_create",
    "decision_projection",
    "notification_outbox",
)

BLOCK_REASON: Final[str] = "PROVIDER_CONTRACT_FAILURE"
BLOCK_EVENT: Final[str] = "feature.blocked"
BLOCK_STATE: Final[str] = "needs_human"

ACCEPT_EVENT: Final[str] = "plan.ready"
ACCEPT_STATE: Final[str] = "awaiting_plan_review"

#: The two-action group: cross-field validation precedes the record, so a
#: validation failure can never be followed by a recorded plan.
ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "validate_plan_cross_fields",
    "record_plan",
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

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)

#: §4: the input manifest binds feature/base; the repo-rules registry carries
#: the deterministic verification-command ids the plan may reference.
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_manifest", "repo_rules_registry"}
)

INPUT_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset({"feature_id", "base_sha"})
REPO_RULE_FIELDS: Final[frozenset[str]] = frozenset({"rule_id", "verification_id"})

#: The closed shape of the single injected planner result: the plan artifact
#: plus the harness evidence envelope. The plan's *contents* are judged in the
#: main function; only this top-level shape is envelope-validated.
RESULT_FIELDS: Final[frozenset[str]] = frozenset({"plan", "source", "status"})

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class PlanCrossFieldsEvaluation:
    """The complete observable result of the pure cross-field decision.

    `reasons` is diagnostic only — the oracle judges the block reason, the
    write set and the traces, never the reason wording.
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
    reasons: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _is_git_sha_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
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
        raise _invalid("wrong plan cross-field spec")
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
        or target.get("state") != "planning"
        or not _is_non_negative_int(target.get("version"))
    ):
        raise _invalid("plan cross-field target must be a feature in planning")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 2:
        raise _invalid("action sequence must contain exactly two steps")
    for index, step in enumerate(action_sequence):
        if not isinstance(step, dict) or frozenset(step) != frozenset({"command"}):
            raise _invalid(f"action step {index} shape is not closed")
        if step.get("command") != ACTION_COMMANDS[index]:
            raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    manifest = facts.get("input_manifest")
    if not isinstance(manifest, dict) or frozenset(manifest) != INPUT_MANIFEST_FIELDS:
        raise _invalid("input_manifest shape is not closed")
    if not _is_git_sha_hex(manifest.get("base_sha")):
        raise _invalid("input_manifest base_sha must be a 40-char git sha")
    if not isinstance(manifest.get("feature_id"), str) or not manifest["feature_id"]:
        raise _invalid("input_manifest feature_id must be a non-empty string")
    registry = facts.get("repo_rules_registry")
    if not isinstance(registry, list):
        raise _invalid("repo_rules_registry must be a list")
    for index, rule in enumerate(registry):
        if not isinstance(rule, dict) or frozenset(rule) != REPO_RULE_FIELDS:
            raise _invalid(f"repo rule {index} shape is not closed")
        for field in ("rule_id", "verification_id"):
            if not isinstance(rule.get(field), str) or not rule[field]:
                raise _invalid(f"repo rule {index} {field} must be a non-empty string")

    results = payload.get("injected_results")
    if not isinstance(results, list) or len(results) != 1:
        raise _invalid("injected_results must contain exactly one planner result")
    result = results[0]
    if not isinstance(result, dict) or frozenset(result) != RESULT_FIELDS:
        raise _invalid("planner result shape is not closed")
    for field in ("source", "status"):
        if not isinstance(result.get(field), str) or not result[field]:
            raise _invalid(f"planner result {field} must be a non-empty string")


def _ordered_tasks(plan: Any) -> tuple[list[Any] | None, str | None]:
    """Return the plan's tasks sorted by `order`, or a malformation reason.

    A malformed plan is a contract failure, never a crash: the planner's output
    cannot raise, it can only be judged. Every task is checked far enough to be
    read safely by the rule derivation.
    """
    if not isinstance(plan, dict):
        return None, "plan artifact is not an object"
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None, "plan tasks are missing, empty or malformed"
    ordered: list[Any] = []
    for task in tasks:
        if not isinstance(task, dict):
            return None, "a plan task is not an object"
        if not isinstance(task.get("task_id"), str) or not task["task_id"]:
            return None, "a plan task is missing a task_id"
        if not _is_non_negative_int(task.get("order")):
            return None, "a plan task has a malformed order"
        if not isinstance(task.get("dependency_task_ids"), list):
            return None, "a plan task is missing dependency_task_ids"
        allowed = task.get("allowed_paths")
        if not isinstance(allowed, list):
            return None, "a plan task is missing allowed_paths"
        for entry in allowed:
            if not isinstance(entry, dict):
                return None, "an allowed path entry is not an object"
            if not isinstance(entry.get("path"), str) or not entry["path"]:
                return None, "an allowed path entry has a malformed path"
            if entry.get("path_type") not in ("file", "directory"):
                return None, "an allowed path entry has a malformed path_type"
        ordered.append(task)
    try:
        ordered.sort(key=lambda task: task["order"])
    except TypeError:
        return None, "plan tasks are not orderable"
    return ordered, None


def _path_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """§4 overlap: equal paths, or a directory containing the other path."""
    left_path, right_path = left.get("path"), right.get("path")
    if not (isinstance(left_path, str) and isinstance(right_path, str)):
        return False
    if left_path == right_path:
        return True
    for dir_entry, other in ((left, right), (right, left)):
        if dir_entry.get("path_type") == "directory" and other.get("path", "").startswith(
            dir_entry.get("path", "") + "/"
        ):
            return True
    return False


def _plan_reasons(plan: Any, facts: dict[str, Any]) -> tuple[str, ...]:
    """Re-derive every §4 cross-field rule; return the violated rule names.

    The list is `()` exactly when the plan conforms. The rule names mirror the
    freeze-package vocabulary so a divergence is auditable, but they are
    diagnostic only.
    """
    reasons: list[str] = []
    manifest = facts["input_manifest"]

    if plan.get("feature_id") != manifest["feature_id"] or plan.get("base_sha") != manifest["base_sha"]:
        reasons.append("identity_mismatch")

    tasks, malformed = _ordered_tasks(plan)
    if malformed is not None:
        return (malformed,)

    paths = [entry for task in tasks for entry in task["allowed_paths"]]
    for left, right in combinations(paths, 2):
        if _path_overlaps(left, right):
            reasons.append("paths_overlap")

    orders = [task["order"] for task in tasks]
    if orders != list(range(1, len(tasks) + 1)):
        reasons.append("order_gap")

    order_by_id = {task["task_id"]: task["order"] for task in tasks}
    for task in tasks:
        for dependency in task["dependency_task_ids"]:
            if order_by_id.get(dependency, 10**9) >= task["order"]:
                reasons.append("dependency_not_earlier")

    registry_ids = {rule["verification_id"] for rule in facts["repo_rules_registry"]}
    criteria = plan.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        return ("plan acceptance_criteria are missing, empty or malformed",)
    for criterion in criteria:
        if not isinstance(criterion, dict):
            return ("an acceptance criterion is not an object",)
        verification_ids = criterion.get("verification_ids")
        if not isinstance(verification_ids, list):
            return ("an acceptance criterion is missing verification_ids",)
        for vid in verification_ids:
            if vid not in registry_ids:
                reasons.append("unknown_verification")

    try:
        actual_digest = jcs_sha256(paths)
    except ValueError:
        return ("allowed paths are not RFC 8785 I-JSON representable",)
    if plan.get("allowed_paths_sha256") != actual_digest:
        reasons.append("digest_drift")

    #: A failure variant isolates one rule; the happy path none. Dedup keeps
    #: the audit list free of a single overlap reported twice per direction.
    return tuple(dict.fromkeys(reasons))


def validate_plan_cross_fields(command: dict[str, Any]) -> PlanCrossFieldsEvaluation:
    """Validate one plan's cross-field properties and decide the boundary.

    A conforming plan continues the operation (`planning → awaiting_plan_review`,
    `plan.ready`, the four-write accept set, `APPLIED`). Any violation — or a
    plan too malformed to judge — fails closed to `planning → needs_human`
    (`feature.blocked`, `PROVIDER_CONTRACT_FAILURE`, reason owner `feature`,
    the seven-write block set).
    """
    _validate_command(command)
    payload = command["input"]
    target = payload["target"]
    facts = payload["authoritative_facts"]

    result = payload["injected_results"][0]
    #: The planner's own source/status claim is never evidence of a valid plan;
    #: only the cross-field rules are. A missing or unfinished planner result
    #: cannot be validated, so it blocks like any other malformation.
    plan = result.get("plan")
    reasons = _plan_reasons(plan, facts)
    if result.get("source") != "planner" or result.get("status") != "completed":
        reasons = ("planner evidence is missing, duplicated or not completed",)

    if reasons:
        return PlanCrossFieldsEvaluation(
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
            reasons=reasons,
        )

    return PlanCrossFieldsEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(target["state"], ACCEPT_STATE),
        final_state=ACCEPT_STATE,
        final_entity_type=target["entity_type"],
        declared_write_set=BASE_WRITE_SET,
        event_trace=(ACCEPT_EVENT,),
    )
