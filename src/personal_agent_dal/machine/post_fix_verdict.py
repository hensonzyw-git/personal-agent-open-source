"""`OP-FIXDIFF-001`: post-fix verdict two-layer boundary (DAL-024, G3).

Freeze package §6 (docs/dal/DAL021-024_合同冻结包_v0.1.md) fixes the
controller-side structural checks on a post-fix verdict. The `verdict` string
is a provider claim, never a fact; the controller re-derives a structural
violation set from Git-executor output and the manifest roles, and only
adopts `verified` when that set is empty:

- **anchor entry** — `anchor_tree_entry` must be a present regular-file blob.
- **path continuity** — `previous_tree_entry` must still be present.
- **surviving-set/increment algebra** — the `+` side of the anchor-translation
  diff is the surviving line set; when any finding was closed, it must be
  non-empty and every surviving line must appear among the increment diff's
  deleted (`-`) lines.
- **evidence role** — a closed finding resolution's first evidence digest must
  have role `fix_diff`.
- **gap evidence** — a gap resolution's first evidence digest must have role
  `test_receipts` and its receipt's `verification_id` must belong to the
  acceptance.
- **new-finding anchor** — every new finding's `location.anchor_sha` must equal
  the verdict's `result_sha` (the regression is measured in the verdict's own
  post-fix tree). Refrozen 2026-08-29 (Henson's authorization, evidence
  `DAL_R09-A2_review-fix-loop_2026-08-29.md` §2c D2): the pre-refreeze rule
  anchored to `round_anchors.anchor_sha` — the pre-fix tree — contradicting
  the frozen contract §6 L620.
- **acceptance** — `acceptance_verified` must be true.

A structurally clean `changes_requested` moves `reviewing → fixing` whether
it carries a `remaining` resolution or only new findings (§6 makes a non-empty
`new_findings[]` itself force `changes_requested`). Refrozen 2026-08-29 (§2c
D3): the pre-refreeze rule demanded a `remaining` resolution before reaching
`fixing`, rejecting the all-closed-plus-regression shape.

A structurally clean `verified` moves the feature `reviewing → verified`
(`review.completed`, four-write base set); a structurally clean
`changes_requested` with a remaining finding moves `reviewing → fixing`
(`fix.requested`, four-write base set); any structural violation moves
`reviewing → needs_human` (`feature.blocked`, `PROVIDER_CONTRACT_FAILURE`,
reason owner `feature`, seven-write block set).

`validate_post_fix_verdict` is the pure decision. The trusted half raises
`DalError(INVALID_ARGUMENT)` on drift; the injected verdict and Git output fail
closed as a contract block, never a crash. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-FIXDIFF-001"
COMMAND_TYPE: Final[str] = "validate_post_fix_verdict"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "review-controller"

#: The target aggregate is a `feature`; its transition receipt schema is the
#: frozen `dal.transition-receipt/1.0` (engine.RECEIPT_SCHEMAS["feature"]).
#: Kept local so this module stays free of the engine's import graph.
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

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

VERIFIED_EVENT: Final[str] = "review.completed"
VERIFIED_STATE: Final[str] = "verified"

FIX_EVENT: Final[str] = "fix.requested"
FIX_STATE: Final[str] = "fixing"

ACTION_COMMANDS: Final[tuple[str, ...]] = (
    "validate_post_fix_verdict",
    "record_review",
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

#: §6: the manifest role map, the original review, the approved plan's tasks
#: and verification ids, the prior verdict chain, the round anchors, and the
#: test-receipt bundle keyed by evidence digest.
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "manifest_roles",
        "original_review",
        "plan_tasks",
        "plan_verification_ids",
        "prior_verdict_chain",
        "round_anchors",
        "test_receipts",
    }
)

ORIGINAL_REVIEW_FIELDS: Final[frozenset[str]] = frozenset(
    {"acceptance_gap_ids", "finding_ids", "finding_locations"}
)
FINDING_LOCATION_FIELDS: Final[frozenset[str]] = frozenset(
    {"anchor_sha", "line_end", "line_start", "path"}
)
PLAN_TASK_FIELDS: Final[frozenset[str]] = frozenset(
    {"acceptance_ids", "allowed_paths", "task_id"}
)
CHAIN_ENTRY_FIELDS: Final[frozenset[str]] = frozenset(
    {"finding_resolutions", "new_findings", "result_sha", "sequence"}
)
ROUND_ANCHOR_FIELDS: Final[frozenset[str]] = frozenset(
    {"anchor_sha", "previous_result_sha"}
)

GIT_RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "anchor_translation_diff",
        "anchor_tree_entry",
        "current_tree_entry",
        "increment_diff",
        "path",
        "previous_tree_entry",
        "source",
        "status",
    }
)
TREE_ENTRY_FIELDS: Final[frozenset[str]] = frozenset({"mode", "present", "type"})

REVIEWER_RESULT_FIELDS: Final[frozenset[str]] = frozenset({"source", "status", "verdict"})
VERDICT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "acceptance_gap_resolutions",
        "acceptance_verified",
        "finding_resolutions",
        "new_findings",
        "result_sha",
        "schema_version",
        "verdict",
    }
)
FINDING_RESOLUTION_FIELDS: Final[frozenset[str]] = frozenset(
    {"evidence_sha256", "finding_id", "status", "summary"}
)
GAP_RESOLUTION_FIELDS: Final[frozenset[str]] = frozenset(
    {"acceptance_id", "evidence_sha256", "status", "summary"}
)

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class PostFixVerdictEvaluation:
    """The complete observable result of the pure post-fix decision.

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
        raise _invalid("wrong post-fix verdict spec")
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
        or target.get("state") != "reviewing"
        or not _is_non_negative_int(target.get("version"))
    ):
        raise _invalid("post-fix verdict target must be a feature in reviewing")

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
    if not isinstance(facts.get("manifest_roles"), dict):
        raise _invalid("manifest_roles must be an object")
    original = facts.get("original_review")
    if not isinstance(original, dict) or frozenset(original) != ORIGINAL_REVIEW_FIELDS:
        raise _invalid("original_review shape is not closed")
    locations = original.get("finding_locations")
    if not isinstance(locations, dict):
        raise _invalid("finding_locations must be an object")
    for finding_id, location in locations.items():
        if not isinstance(finding_id, str) or not finding_id:
            raise _invalid("finding_locations keys must be non-empty strings")
        if not isinstance(location, dict) or frozenset(location) != FINDING_LOCATION_FIELDS:
            raise _invalid(f"finding_locations[{finding_id}] shape is not closed")
        if not _is_git_sha_hex(location.get("anchor_sha")):
            raise _invalid(f"finding_locations[{finding_id}] anchor_sha must be a 40-char git sha")
    plan_tasks = facts.get("plan_tasks")
    if not isinstance(plan_tasks, list) or not plan_tasks:
        raise _invalid("plan_tasks must be a non-empty list")
    for task in plan_tasks:
        if not isinstance(task, dict) or frozenset(task) != PLAN_TASK_FIELDS:
            raise _invalid("plan_tasks entry shape is not closed")
    verification_ids = facts.get("plan_verification_ids")
    if not isinstance(verification_ids, dict):
        raise _invalid("plan_verification_ids must be an object")
    chain = facts.get("prior_verdict_chain")
    if not isinstance(chain, list):
        raise _invalid("prior_verdict_chain must be a list")
    for entry in chain:
        if not isinstance(entry, dict) or frozenset(entry) != CHAIN_ENTRY_FIELDS:
            raise _invalid("prior_verdict_chain entry shape is not closed")
    anchors = facts.get("round_anchors")
    if not isinstance(anchors, dict) or frozenset(anchors) != ROUND_ANCHOR_FIELDS:
        raise _invalid("round_anchors shape is not closed")
    for field in ROUND_ANCHOR_FIELDS:
        if not _is_git_sha_hex(anchors.get(field)):
            raise _invalid(f"round_anchors {field} must be a 40-char git sha")
    if not isinstance(facts.get("test_receipts"), dict):
        raise _invalid("test_receipts must be an object")

    results = payload.get("injected_results")
    if not isinstance(results, list) or len(results) != 2:
        raise _invalid("injected_results must contain git_executor and reviewer results")
    git_result, reviewer_result = results
    if not isinstance(git_result, dict) or frozenset(git_result) != GIT_RESULT_FIELDS:
        raise _invalid("git executor result shape is not closed")
    for field in ("source", "status", "path"):
        if not isinstance(git_result.get(field), str) or not git_result[field]:
            raise _invalid(f"git executor result {field} must be a non-empty string")
    for field in ("anchor_tree_entry", "current_tree_entry", "previous_tree_entry"):
        entry = git_result.get(field)
        if not isinstance(entry, dict) or frozenset(entry) != TREE_ENTRY_FIELDS:
            raise _invalid(f"git executor result {field} shape is not closed")
    if not isinstance(reviewer_result, dict) or frozenset(reviewer_result) != REVIEWER_RESULT_FIELDS:
        raise _invalid("reviewer result shape is not closed")
    for field in ("source", "status"):
        if not isinstance(reviewer_result.get(field), str) or not reviewer_result[field]:
            raise _invalid(f"reviewer result {field} must be a non-empty string")
    verdict = reviewer_result.get("verdict")
    if not isinstance(verdict, dict) or frozenset(verdict) != VERDICT_FIELDS:
        raise _invalid("verdict shape is not closed")
    if verdict.get("schema_version") != "dal.post-fix-verdict/1.0":
        raise _invalid("wrong verdict schema")
    if verdict.get("verdict") not in ("verified", "changes_requested"):
        raise _invalid("verdict must be verified or changes_requested")
    if not isinstance(verdict.get("acceptance_verified"), bool):
        raise _invalid("verdict acceptance_verified must be boolean")
    if not _is_git_sha_hex(verdict.get("result_sha")):
        raise _invalid("verdict result_sha must be a 40-char git sha")


def _diff_parts(diff_text: Any) -> tuple[set[str], set[str]]:
    """Return `(deleted, added)` line sets of a single-hunk unified diff.

    The first line is the hunk header and is ignored; context lines are not
    collected. A non-string diff yields empty sets — malformed provider/Git
    output fails closed (an empty surviving set is a violation when a finding
    was closed), never a crash.
    """
    if not isinstance(diff_text, str):
        return set(), set()
    deleted: set[str] = set()
    added: set[str] = set()
    for line in diff_text.split("\n")[1:]:
        if line.startswith("-"):
            deleted.add(line[1:])
        elif line.startswith("+"):
            added.add(line[1:])
    return deleted, added


def _structural(
    facts: dict[str, Any], git_result: dict[str, Any], verdict: dict[str, Any]
) -> tuple[str, ...]:
    """Re-derive the §6 structural violation set for one verdict.

    Mirrors the independent refreeze (`refreeze_controller_decisions.py`)
    exactly: the violation names are diagnostic only; the outcome is
    `"verified"`/`"fixing"`/`"block"` decided from whether this set is empty.
    """
    violations: list[str] = []
    roles = facts["manifest_roles"]

    anchor_entry = git_result["anchor_tree_entry"]
    if anchor_entry["type"] != "blob" or not anchor_entry["present"]:
        violations.append("anchor_entry_not_blob")
    if not git_result["previous_tree_entry"]["present"]:
        violations.append("path_died_between_rounds")

    _, surviving = _diff_parts(git_result["anchor_translation_diff"])
    increment_deleted, _ = _diff_parts(git_result["increment_diff"])
    has_closed = any(item["status"] == "closed" for item in verdict["finding_resolutions"])
    if has_closed and not surviving:
        violations.append("surviving_set_empty")
    if has_closed and any(line not in increment_deleted for line in surviving):
        violations.append("increment_deletion")

    for item in verdict["finding_resolutions"]:
        if item["status"] == "closed" and roles.get(item["evidence_sha256"][0]) != "fix_diff":
            violations.append("evidence_role")

    for item in verdict["acceptance_gap_resolutions"]:
        evidence = item["evidence_sha256"][0]
        receipt = facts["test_receipts"].get(evidence)
        if (
            roles.get(evidence) != "test_receipts"
            or receipt is None
            or receipt.get("verification_id")
            not in facts["plan_verification_ids"].get(item["acceptance_id"], [])
        ):
            violations.append("gap_evidence")

    for finding in verdict["new_findings"]:
        if finding["location"]["anchor_sha"] != verdict["result_sha"]:
            violations.append("new_finding_anchor")

    if not verdict["acceptance_verified"]:
        violations.append("acceptance")

    return tuple(dict.fromkeys(violations))


def validate_post_fix_verdict(command: dict[str, Any]) -> PostFixVerdictEvaluation:
    """Validate one post-fix verdict's structural invariants and decide.

    A structurally clean `verified` verifies the feature; a structurally
    clean `changes_requested` requests a fix; any structural violation
    blocks the feature.
    """
    _validate_command(command)
    payload = command["input"]
    target = payload["target"]
    facts = payload["authoritative_facts"]

    git_result, reviewer_result = payload["injected_results"]
    verdict = reviewer_result["verdict"]

    reasons: list[str] = []
    if (
        git_result["source"] != "git_executor"
        or git_result["status"] != "completed"
        or reviewer_result["source"] != "reviewer"
        or reviewer_result["status"] != "completed"
    ):
        reasons.append("injected evidence is missing, duplicated or not completed")

    violations = _structural(facts, git_result, verdict)
    reasons.extend(violations)

    declared = verdict["verdict"]
    if declared == "verified":
        outcome = "verified" if not reasons else None
    else:
        #: §6 D3 (refrozen 2026-08-29): a non-empty ``new_findings`` itself
        #: forces ``changes_requested``; the verdict no longer needs a
        #: ``remaining`` resolution to reach ``fixing``.
        outcome = "fixing" if not reasons else None

    if outcome is None:
        return PostFixVerdictEvaluation(
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
            reasons=tuple(reasons),
        )

    if outcome == "verified":
        return PostFixVerdictEvaluation(
            receipt=OperationReceipt(
                ReceiptCode.APPLIED,
                schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
            ),
            state_trace=(target["state"], VERIFIED_STATE),
            final_state=VERIFIED_STATE,
            final_entity_type=target["entity_type"],
            declared_write_set=BASE_WRITE_SET,
            event_trace=(VERIFIED_EVENT,),
        )

    return PostFixVerdictEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(target["state"], FIX_STATE),
        final_state=FIX_STATE,
        final_entity_type=target["entity_type"],
        declared_write_set=BASE_WRITE_SET,
        event_trace=(FIX_EVENT,),
    )
