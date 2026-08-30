"""`OP-OPENSET-001`: carry-forward open-finding-set boundary (DAL-024, G3).

Freeze package §6 (docs/dal/DAL021-024_合同冻结包_v0.1.md) makes the open
finding set a controller-derived state, not a provider claim. The open set is
re-derived per round from the protected inputs: it starts as the original
review's findings, and after each prior verdict V_j it loses the findings V_j
resolved ``closed`` and gains V_j's ``new_findings`` (kept verbatim, by id) —
so a regression carried in by one round can never silently vanish behind a
later chain. Refrozen 2026-08-29 (Henson's authorization, evidence
`DAL_R09-A2_review-fix-loop_2026-08-29.md` §2c D1): the pre-refreeze rule
replaced the whole set with the chain's accumulated ``new_findings`` whenever
the chain was non-empty, dropping original findings still ``remaining``.

- New-finding ID collisions are checked against the **full** namespace the
  feature has seen (`original ∪ chain`), so a regression can never silently
  reuse an already-seen ID.
- Every carried chain finding's old-side line must actually be deleted by
  this round's increment diff (`old<line_start>` present among deleted
  lines).

`derive_open_finding_set` judges the injected verdict against that set:

- `verified` is legal only when the open set is fully resolved (no unresolved,
  no unknown, no collision) and there are no new findings.
- `changes_requested` is legal only when it names remaining or new findings
  and introduces no unknown resolution and no collision.
- Anything else — omitted carried findings, renamed carried findings, a
  `verified` verdict that still carries new findings, an ID reuse — fails
  closed.

A legal `verified` moves the feature `reviewing → verified` (`review.completed`,
four-write base set); a legal `changes_requested` moves `reviewing → fixing`
(`fix.requested`, four-write base set); any violation moves `reviewing →
needs_human` (`feature.blocked`, `PROVIDER_CONTRACT_FAILURE`, reason owner
`feature`, seven-write block set).

`derive_open_finding_set` is the pure decision. The trusted half raises
`DalError(INVALID_ARGUMENT)` on drift; the injected verdict and diff are
provider/Git output and fail closed as a contract block, never a crash.
No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-OPENSET-001"
COMMAND_TYPE: Final[str] = "derive_open_finding_set"
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
    "derive_open_finding_set",
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

#: §6: manifest role map, the original review's finding/gap ids, the prior
#: verdict chain (whose new findings carry forward), and the round anchors.
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "manifest_roles",
        "original_review",
        "prior_verdict_chain",
        "recomputed_result_sha",
        "round_anchors",
    }
)

ORIGINAL_REVIEW_FIELDS: Final[frozenset[str]] = frozenset(
    {"acceptance_gap_ids", "finding_ids"}
)
CHAIN_ENTRY_FIELDS: Final[frozenset[str]] = frozenset(
    {"finding_resolutions", "new_findings", "result_sha", "sequence"}
)
NEW_FINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {"category", "failure_scenario", "finding_id", "location", "severity", "summary"}
)
LOCATION_FIELDS: Final[frozenset[str]] = frozenset(
    {"anchor_sha", "line_end", "line_start", "path"}
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
RESOLUTION_FIELDS: Final[frozenset[str]] = frozenset(
    {"evidence_sha256", "finding_id", "status", "summary"}
)

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class OpenFindingSetEvaluation:
    """The complete observable result of the pure open-set decision.

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
        raise _invalid("wrong open-finding-set spec")
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
        raise _invalid("open-finding-set target must be a feature in reviewing")

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
    if not _is_git_sha_hex(facts.get("recomputed_result_sha")):
        raise _invalid("recomputed_result_sha must be a 40-char git sha")
    anchors = facts.get("round_anchors")
    if not isinstance(anchors, dict) or frozenset(anchors) != ROUND_ANCHOR_FIELDS:
        raise _invalid("round_anchors shape is not closed")
    for field in ROUND_ANCHOR_FIELDS:
        if not _is_git_sha_hex(anchors.get(field)):
            raise _invalid(f"round_anchors {field} must be a 40-char git sha")
    chain = facts.get("prior_verdict_chain")
    if not isinstance(chain, list):
        raise _invalid("prior_verdict_chain must be a list")
    for entry in chain:
        if not isinstance(entry, dict) or frozenset(entry) != CHAIN_ENTRY_FIELDS:
            raise _invalid("prior_verdict_chain entry shape is not closed")

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
    if not _is_git_sha_hex(verdict.get("result_sha")):
        raise _invalid("verdict result_sha must be a 40-char git sha")


def _deleted_lines(diff_text: Any) -> set[str]:
    """The deleted (`-`) lines of a unified diff, minus the header line."""
    if not isinstance(diff_text, str):
        return set()
    deleted: set[str] = set()
    for line in diff_text.split("\n")[1:]:
        if line.startswith("-"):
            deleted.add(line[1:])
    return deleted


def _derive(facts: dict[str, Any], git_result: dict[str, Any], verdict: dict[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """Judge the verdict against the derived open set; returns `(outcome, reasons)`.

    `outcome` is `"verified"`, `"fixing"` or `None` (block). The derivation
    mirrors the independent refreeze so a conforming controller reaches the
    same open set; the rule names are diagnostic only.
    """
    reasons: list[str] = []

    if verdict["result_sha"] != facts["recomputed_result_sha"]:
        reasons.append("verdict is not bound to the recomputed result")

    if (
        git_result["anchor_tree_entry"] != {"mode": "100644", "type": "blob", "present": True}
        or not git_result["previous_tree_entry"]["present"]
        or not git_result["current_tree_entry"]["present"]
    ):
        reasons.append("git tree entries are not well-formed")

    original_ids = set(facts["original_review"]["finding_ids"])
    chain_findings = [
        item
        for entry in facts["prior_verdict_chain"]
        for item in entry["new_findings"]
    ]
    chain_ids = {item["finding_id"] for item in chain_findings if isinstance(item, dict)}

    #: §6 per-round carry-forward: the open set starts as the original
    #: review's findings and after each prior verdict V_j it loses the
    #: findings V_j resolved ``closed`` and gains V_j's new findings (kept
    #: verbatim, by id). Refrozen 2026-08-29 (§2c D1) — the pre-refreeze
    #: rule replaced the whole set with the chain's accumulated
    #: ``new_findings``, dropping original findings still ``remaining``.
    open_ids = set(original_ids)
    for entry in facts["prior_verdict_chain"]:
        closed_ids = {
            item["finding_id"]
            for item in entry["finding_resolutions"]
            if isinstance(item, dict) and item.get("status") == "closed"
        }
        open_ids -= closed_ids
        open_ids |= {
            item["finding_id"]
            for item in entry["new_findings"]
            if isinstance(item, dict)
        }

    resolutions = verdict["finding_resolutions"]
    closed = {item["finding_id"] for item in resolutions if item["status"] == "closed"}
    remaining = {item["finding_id"] for item in resolutions if item["status"] == "remaining"}
    unknown = {item["finding_id"] for item in resolutions} - open_ids
    new_ids = {item["finding_id"] for item in verdict["new_findings"]}
    collisions = new_ids & (original_ids | chain_ids)
    unresolved = open_ids - closed - remaining

    deleted = _deleted_lines(git_result["increment_diff"])
    for finding in chain_findings:
        location = finding.get("location")
        line_start = location.get("line_start") if isinstance(location, dict) else None
        if f"old{line_start}" not in deleted:
            reasons.append("the increment does not delete a carried finding's line")

    if unknown:
        reasons.append("a resolution references a finding outside the open set")
    if collisions:
        reasons.append("a new finding reuses an already-seen id")

    declared = verdict["verdict"]
    if declared == "verified":
        if unresolved:
            reasons.append("the open set is not fully resolved")
        if verdict["new_findings"]:
            reasons.append("a verified verdict still carries new findings")
        outcome = "verified" if not reasons else None
    else:
        if not (remaining or verdict["new_findings"]):
            reasons.append("changes_requested names no remaining or new findings")
        outcome = "fixing" if not reasons else None

    return outcome, tuple(reasons)


def derive_open_finding_set(command: dict[str, Any]) -> OpenFindingSetEvaluation:
    """Derive the open finding set and judge one verdict against it.

    A legal `verified` verifies the feature; a legal `changes_requested`
    requests a fix; any violation of the carry-forward invariants blocks the
    feature.
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

    outcome, derive_reasons = _derive(facts, git_result, verdict)
    reasons.extend(derive_reasons)

    if outcome is None:
        return OpenFindingSetEvaluation(
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
        return OpenFindingSetEvaluation(
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

    return OpenFindingSetEvaluation(
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
