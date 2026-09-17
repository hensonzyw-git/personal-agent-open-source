"""`OP-PATH-001`: repository allowlist + worktree isolation boundary (DAL-018, G2).

The Development Agent Loop only opens files beneath the workspace root of the
approved repository, and never through a symlink that could be swapped after the
containment check. ``evaluate_path`` judges a path resolution against those rules
and surfaces any escape as a block signal. The frozen G2 variants each fail one
rule:

- **absolute** — the requested path is absolute, outside the workspace root;
- **dotdot** — the requested path traverses ``..`` out of the workspace;
- **nested_repo** — the resolved repository is not the allowed repository;
- **symlink_swap** — the inode changes between ``lstat`` and ``openat`` (a
  symlink was swapped after the containment check).

This module is the guard half: it ingests the resolution facts and the
filesystem verdict, and decides whether the open is safe. It is a pure decision
with no persistence step — the ``block_feature`` transition that consumes the
``POLICY_FAILURE`` result is applied by the trusted resolver and the
deterministic engine, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode

OPERATION_SPEC_ID: Final[str] = "OP-PATH-001"
COMMAND_TYPE: Final[str] = "evaluate_path"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "workspace-controller"

#: The reason a path escape blocks with, matching the frozen
#: ``BLK-POLICY--coding`` transition's ``result_reason_code``.
BLOCK_REASON: Final[str] = "POLICY_FAILURE"

#: The detector's positive-clean verdict: the filesystem resolved the path
#: beneath the workspace root with no symlink swap. Anything else (including a
#: missing or timed-out resolution) is treated as an escape (fail closed).
CLEAN_STATUS: Final[str] = "contained"

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
    "resolve_beneath_workspace",
    "open_with_no_follow",
)

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
#: The common resolution facts present on every variant.
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"allowed_repository_id", "requested_path", "resolved_repository_id", "workspace_root"}
)
#: The symlink-swap variant carries the two inode observations plus the resolved
#: path that escaped, on top of the common facts.
SYMLINK_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"lstat_inode_before", "openat_inode_after", "resolved_path"}
)
RESULT_FIELDS: Final[frozenset[str]] = frozenset({"source", "status"})


@dataclass(frozen=True)
class PathEvaluation:
    """The pure verdict of the path-resolution check.

    ``conflict`` is True when the path escapes the workspace or its repository
    boundary; ``reason`` names the block reason the resolver must carry into the
    ``block_feature`` transition.
    """

    conflict: bool
    reason: str | None = None


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _validate_str(value: Any, *, field: str) -> str:
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
        raise _invalid("wrong path spec")
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
        raise _invalid("invalid path target")

    action_sequence = payload.get("action_sequence")
    expected = [{"command": ACTION_COMMANDS[0]}, {"command": ACTION_COMMANDS[1]}]
    if action_sequence != expected:
        raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict):
        raise _invalid("authoritative facts must be an object")
    if frozenset(facts) not in (FACT_FIELDS, FACT_FIELDS | SYMLINK_FACT_FIELDS):
        raise _invalid("authoritative facts shape is not closed")
    for field in FACT_FIELDS:
        _validate_str(facts.get(field), field=field)
    for field in ("lstat_inode_before", "openat_inode_after"):
        if field in facts and (
            not isinstance(facts[field], int) or isinstance(facts[field], bool)
        ):
            raise _invalid(f"{field} must be an integer")
    if "resolved_path" in facts:
        _validate_str(facts.get("resolved_path"), field="resolved_path")

    injected_results = payload.get("injected_results")
    if not isinstance(injected_results, list) or not injected_results:
        raise _invalid("injected_results must be a non-empty list")
    for result in injected_results:
        if not isinstance(result, dict):
            raise _invalid("every filesystem result must be an object")
        if not frozenset(result) <= RESULT_FIELDS:
            raise _invalid("filesystem result contains an unknown field")
        if result.get("source") != "filesystem":
            raise _invalid("unexpected filesystem result source")
        if not isinstance(result.get("status"), str) or not result["status"]:
            raise _invalid("filesystem status must be a non-empty string")


def _detector_reports_escape(injected_results: list[dict[str, Any]]) -> bool:
    """True unless the filesystem positively reports the path is contained.

    A resolution that is missing, or that did not reach ``contained``, means the
    open could not be positively verified, so it is treated as an escape (fail
    closed).
    """
    if len(injected_results) != 1:
        return True
    return injected_results[0]["status"] != CLEAN_STATUS


def _facts_show_escape(facts: dict[str, Any]) -> bool:
    """True when the resolution facts themselves prove an escape.

    Defence in depth: even a detector that misreports ``contained`` cannot
    rescue an absolute or ``..`` path, a repository mismatch, or an inode change
    between the containment check and the open.
    """
    requested = facts["requested_path"]
    if requested.startswith("/"):
        return True
    if any(component == ".." for component in requested.split("/")):
        return True
    if facts["resolved_repository_id"] != facts["allowed_repository_id"]:
        return True
    if "lstat_inode_before" in facts and facts["lstat_inode_before"] != facts["openat_inode_after"]:
        return True
    return False


def evaluate_path(command: dict[str, Any]) -> PathEvaluation:
    """Report whether a path resolution escapes the workspace boundary.

    Returns a conflict verdict when the filesystem reports an escape (or fails
    to confirm containment) or when the resolution facts themselves prove one.
    The verdict is pure: it does not move the feature, whose ``block_feature``
    transition is the resolver's and engine's step.
    """
    _validate_command(command)
    payload = command["input"]
    facts = payload["authoritative_facts"]

    if _detector_reports_escape(payload["injected_results"]) or _facts_show_escape(facts):
        return PathEvaluation(conflict=True, reason=BLOCK_REASON)
    return PathEvaluation(conflict=False)
