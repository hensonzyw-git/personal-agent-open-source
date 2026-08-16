"""`OP-GIT-BASE-001`: git mutation-precondition boundary (DAL-015/018, G2).

The Development Agent Loop only mutates a repository through the approved
base/PR-head SHA and a clean index/worktree. ``verify_git_mutation_preconditions``
judges a git read-back against those preconditions and surfaces drift or
conflict as a block signal. The frozen G2 variants each fail one precondition:

- **base_drift** — the observed base SHA differs from the approved base;
- **pr_head_drift** — the observed PR-head SHA differs from the approved head;
- **index_conflict** — the index is not clean;
- **content_conflict** — the worktree is not clean.

This module is the facts-adapter half: it ingests the read-back and decides
whether a mutation is safe. It is a pure decision with no persistence step —
the ``block_feature`` transition that consumes the ``GIT_CONFLICT`` result is
applied by the trusted resolver and the deterministic engine, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode

OPERATION_SPEC_ID: Final[str] = "OP-GIT-BASE-001"
COMMAND_TYPE: Final[str] = "verify_git_mutation_preconditions"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "git-controller"

#: The reason a failed precondition blocks with, matching the frozen
#: ``BLK-GIT--coding`` transition's ``result_reason_code``.
BLOCK_REASON: Final[str] = "GIT_CONFLICT"

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
    "verify_precommit_state",
    "verify_prepush_state",
)

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "approved_base_sha",
        "approved_pr_head_sha",
        "index_clean",
        "observed_base_sha",
        "observed_pr_head_sha",
        "worktree_clean",
    }
)
READBACK_RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"head_sha", "pr_head_sha", "source", "status"}
)


@dataclass(frozen=True)
class GitBaseEvaluation:
    """The pure verdict of the mutation-precondition check.

    ``conflict`` is True when any precondition fails; ``reason`` names the block
    reason the resolver must carry into the ``block_feature`` transition.
    """

    conflict: bool
    reason: str | None = None


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _validate_sha(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{field} must be a non-empty string")
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
        raise _invalid("wrong git-base spec")
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
        raise _invalid("invalid git-base target")

    action_sequence = payload.get("action_sequence")
    expected = [{"command": ACTION_COMMANDS[0]}, {"command": ACTION_COMMANDS[1]}]
    if action_sequence != expected:
        raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    _validate_sha(facts.get("approved_base_sha"), field="approved_base_sha")
    _validate_sha(facts.get("approved_pr_head_sha"), field="approved_pr_head_sha")
    _validate_sha(facts.get("observed_base_sha"), field="observed_base_sha")
    _validate_sha(facts.get("observed_pr_head_sha"), field="observed_pr_head_sha")
    for flag in ("index_clean", "worktree_clean"):
        if not isinstance(facts.get(flag), bool):
            raise _invalid(f"{flag} must be boolean")

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list) or not injected_results:
        raise _invalid("injected_results must be a non-empty list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every read-back result must be an object")
        if not frozenset(result) <= READBACK_RESULT_FIELDS:
            raise _invalid("read-back result contains an unknown field")
        if result.get("source") != "git":
            raise _invalid("unexpected read-back source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("read-back status must be a non-empty string")
        if "head_sha" in result:
            _validate_sha(result["head_sha"], field="read-back head_sha")
        if "pr_head_sha" in result:
            _validate_sha(result["pr_head_sha"], field="read-back pr_head_sha")


def _readback_completed(injected_results: list[dict[str, Any]]) -> bool:
    """True only when a single completed git read-back is present.

    A read-back that is missing, or that did not reach ``completed``, means the
    preconditions could not be positively verified, so the mutation is unsafe
    (fail closed).
    """
    if len(injected_results) != 1:
        return False
    return injected_results[0]["status"] == "completed"


def verify_git_mutation_preconditions(
    command: dict[str, Any],
) -> GitBaseEvaluation:
    """Report whether the git mutation preconditions hold.

    Returns a conflict verdict when the read-back is incomplete, or when any of
    base drift, PR-head drift, index conflict or content conflict is present.
    The verdict is pure: it does not move the feature, whose ``block_feature``
    transition is the resolver's and engine's step.
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]

    if not _readback_completed(payload["injected_results"]):
        return GitBaseEvaluation(conflict=True, reason=BLOCK_REASON)
    if facts["observed_base_sha"] != facts["approved_base_sha"]:
        return GitBaseEvaluation(conflict=True, reason=BLOCK_REASON)
    if facts["observed_pr_head_sha"] != facts["approved_pr_head_sha"]:
        return GitBaseEvaluation(conflict=True, reason=BLOCK_REASON)
    if not facts["index_clean"]:
        return GitBaseEvaluation(conflict=True, reason=BLOCK_REASON)
    if not facts["worktree_clean"]:
        return GitBaseEvaluation(conflict=True, reason=BLOCK_REASON)
    return GitBaseEvaluation(conflict=False)
