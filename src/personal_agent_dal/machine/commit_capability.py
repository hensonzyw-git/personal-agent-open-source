"""`DAL-031`: the one-time commit capability (R09-A3).

The boundary between "the feature is verified" and "a candidate commit may
be created". Two pure gates in the ``patch_policy`` / ``review_fix_loop``
family (G2 guard form: no I/O, no operation spec id, no manifest entry, no
dispatch-graph import):

- **issue_commit_capability** — call-before. The trusted controller asserts
  the feature is `verified` and presents the binding the capability will
  carry: approval identity, capability/lease epochs, base/result SHAs,
  the approved plan's allowed paths, the four
  frozen commit trailers, the issuing idempotency key, an expiry, and
  ``max_uses`` (frozen to 1). A go verdict here authorises exactly one
  candidate-commit consumption under exactly this binding.
- **consume_commit_capability** — the deterministic git executor's gate.
  The presented commit intent (capability id, SHAs, idempotency key,
  trailers, touched paths) is judged against the issued capability under
  three ordered classes:

  1. trusted shape drift raises ``INVALID_ARGUMENT`` (facts the controller
     vouches for must be well-formed; never silently repaired);
  2. a **dead** capability — consumed, revoked, expired, or left behind by
     an epoch bump (DAL-016) — refuses ``CAPABILITY_STALE`` with zero
     writes. Classification order is a safety property: the caller may
     present anything against a dead capability; the capability is already
     bad, and judging tampering first would let binding drift decide which
     refusal a replay sees. Expiry is inclusive of the instant
     (``now <= expires_at`` is live).
  3. a **live** capability whose presentation diverges from its binding —
     a different SHA, id, idempotency key, trailer set, or any touched path
     outside the allowed set — lands the frozen ``BLK-POLICY--verified``
     row (verified → needs_human, `POLICY_FAILURE` / `feature`, the
     seven-write block set, ``APPLIED``) with every violation labelled so
     the audit trail names all of them, never a crash and never a silent
     pass.

These are eligibility predicates, not persistent issue/consume operations.
No controller or executor composition calls them yet. This module has no I/O;
that alone does not prove provider process isolation or single consumption.
Trailers are frozen by the technical design §9.3
(``Feature-Id``/``Task-Id``/``Plan-Hash``/``Review-Id``); the issue gate
binds ``Feature-Id`` to the target so a capability cannot be issued for one
feature and spent on another. Path semantics are the frozen plan semantics
(DAL021-024 §plan): ``file`` covers only the exact path, ``directory``
covers itself and everything beneath it at a component boundary.

Time is epoch seconds (integer). Native-str-only guards (round-7 review F5
family): string validators accept only native JSON strings — a str subclass
can override hash/equality, so trusted values carrying one raise and
untrusted presentation values carrying one land the tamper block, never a
crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

#: The target aggregate is a `feature`; defined locally to keep this module
#: self-contained and free of the engine's heavy import graph (family form).
FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

ISSUE_FACTS_SCHEMA: Final[str] = "dal.commit-capability-issue-facts/1.0"
CONSUME_FACTS_SCHEMA: Final[str] = "dal.commit-capability-consume-facts/1.0"

#: The frozen block landing for a live capability whose presentation
#: diverges from its binding (BLK-POLICY--verified in the transition
#: registry).
BLOCK_REASON: Final[str] = "POLICY_FAILURE"
BLOCK_OWNER: Final[str] = "feature"
BLOCK_EVENT: Final[str] = "feature.blocked"
BLOCK_STATE: Final[str] = "needs_human"
VERIFIED_STATE: Final[str] = "verified"

#: The four frozen commit trailers (技术方案 §9.3). A closed set: an extra
#: trailer is drift, a missing one is drift.
TRAILER_KEYS: Final[frozenset[str]] = frozenset(
    {"Feature-Id", "Task-Id", "Plan-Hash", "Review-Id"}
)

#: Path types follow the frozen plan semantics (DAL021-024 §plan).
PATH_TYPES: Final[frozenset[str]] = frozenset({"file", "directory"})

_HEX: Final[str] = "0123456789abcdef"

ISSUE_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "target", "binding", "now"}
)
BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "capability_id",
        "approval_id",
        "lease_epoch",
        "base_sha",
        "result_sha",
        "allowed_paths",
        "trailers",
        "idempotency_key",
        "expires_at",
        "max_uses",
        "capability_epoch",
    }
)
ALLOWED_PATH_FIELDS: Final[frozenset[str]] = frozenset({"path", "path_type"})

CONSUME_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version", "target", "capability", "presented", "now",
        "current_epoch", "current_lease_epoch",
    }
)
CAPABILITY_FIELDS: Final[frozenset[str]] = BINDING_FIELDS | {
    "uses_consumed",
    "consumed_by",
    "revoked_at",
}
PRESENTED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "capability_id",
        "approval_id",
        "base_sha",
        "result_sha",
        "touched_paths",
        "trailers",
        "idempotency_key",
    }
)
TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)


@dataclass(frozen=True)
class CommitCapabilityEvaluation:
    """The complete observable result of one capability-gate decision.

    ``violations`` carries the tamper labels on the frozen block landing and
    stays empty for a go verdict or a clean stale refusal. On a zero-write
    stale refusal every trace field stays at its no-op value.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, ...]
    final_state: str
    final_entity_type: str
    final_reason_code: str | None = None
    final_reason_owner: str | None = None
    declared_write_set: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_git_sha_hex(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 40
        and all(char in _HEX for char in value)
    )


def _is_sha256_hex(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _is_non_empty_str(value: Any) -> bool:
    """Only native JSON strings; subclasses can override hash/equality."""
    return type(value) is str and bool(value)


def _is_epoch_seconds(value: Any) -> bool:
    """Epoch seconds are non-negative native ints; ``True`` is not one."""
    return type(value) is int and value >= 0


def _native_object(value: Any) -> bool:
    """Do not hash or look up attacker-controlled keys before checking types."""
    return type(value) is dict and all(type(key) is str for key in value)


def _closed_object(value: Any, fields: frozenset[str]) -> bool:
    return _native_object(value) and frozenset(value) == fields


def _is_normalized_repo_path(value: Any) -> bool:
    if not _is_non_empty_str(value) or value.startswith("/") or "\0" in value:
        return False
    return all(component not in ("", ".", "..") for component in value.split("/"))


def _normalized_repo_path(value: Any, *, field: str) -> str:
    """Validate a canonical repo-relative POSIX path (DAL021-024 §plan)."""
    if not _is_normalized_repo_path(value):
        raise _invalid(f"{field} must be a normalized repo-relative POSIX path")
    return value


def _validate_target(target: Any) -> None:
    if not _closed_object(target, TARGET_FIELDS):
        raise _invalid("target shape is not closed")
    if (
        not _is_non_empty_str(target.get("entity_id"))
        or type(target.get("entity_type")) is not str
        or target.get("entity_type") != "feature"
        or type(target.get("state")) is not str
        or target.get("state") != VERIFIED_STATE
        or not _is_epoch_seconds(target.get("version"))
    ):
        raise _invalid("commit capability target must be a verified feature")


def _validate_trailers(trailers: Any, *, field: str) -> None:
    """The closed four-trailer set with value-class checks."""
    if not _closed_object(trailers, TRAILER_KEYS):
        raise _invalid(f"{field} must carry exactly the four frozen trailers")
    for key in ("Feature-Id", "Task-Id", "Review-Id"):
        if not _is_non_empty_str(trailers[key]):
            raise _invalid(f"{field} {key} must be a non-empty string")
    if not _is_sha256_hex(trailers["Plan-Hash"]):
        raise _invalid(f"{field} Plan-Hash must be a sha256 digest")


def _validate_allowed_paths(allowed_paths: Any) -> None:
    if type(allowed_paths) is not list or not allowed_paths:
        raise _invalid("allowed_paths must be a non-empty list")
    for entry in allowed_paths:
        if not _closed_object(entry, ALLOWED_PATH_FIELDS):
            raise _invalid("allowed path shape is not closed")
        path = _normalized_repo_path(entry.get("path"), field="allowed path")
        # The frozen plan-artifact schema forbids the exact ``.git`` component
        # (``.github``/``.gitignore`` remain expressible in a plan).
        if any(component == ".git" for component in path.split("/")):
            raise _invalid("allowed path must not name a .git subtree")
        if type(entry.get("path_type")) is not str or entry["path_type"] not in PATH_TYPES:
            raise _invalid("allowed path_type must be file or directory")


def _validate_binding_core(binding: dict[str, Any]) -> None:
    """The binding values shared by the issue binding and the consume row.

    The closed-set check lives at the two entries: the issue binding is
    exactly ``BINDING_FIELDS``, the consume row additionally carries the
    lifecycle fields (``uses_consumed``, ``consumed_by``, ``revoked_at``).
    """
    if not _native_object(binding):
        raise _invalid("capability binding must be an object")
    if (
        not _is_non_empty_str(binding.get("capability_id"))
        or not _is_non_empty_str(binding.get("approval_id"))
        or not _is_git_sha_hex(binding.get("base_sha"))
        or not _is_git_sha_hex(binding.get("result_sha"))
        or not _is_non_empty_str(binding.get("idempotency_key"))
        or not _is_epoch_seconds(binding.get("expires_at"))
        or not _is_epoch_seconds(binding.get("capability_epoch"))
        or not _is_epoch_seconds(binding.get("lease_epoch"))
        or type(binding.get("max_uses")) is not int
        # ``max_uses`` is frozen to 1 (技术方案 §6.2): a one-time capability.
        # Any other value is trusted drift — a row with one could never have
        # been legally issued.
        or binding["max_uses"] != 1
    ):
        raise _invalid("capability binding values are malformed")
    _validate_trailers(binding.get("trailers"), field="binding trailers")
    _validate_allowed_paths(binding.get("allowed_paths"))


def _validate_issue_facts(facts: dict[str, Any]) -> None:
    if not _closed_object(facts, ISSUE_FACT_FIELDS):
        raise _invalid("issue facts shape is not closed")
    if type(facts.get("schema_version")) is not str or facts["schema_version"] != ISSUE_FACTS_SCHEMA:
        raise _invalid("wrong issue facts schema")
    _validate_target(facts.get("target"))
    binding = facts.get("binding")
    if not _closed_object(binding, BINDING_FIELDS):
        raise _invalid("binding shape is not closed")
    _validate_binding_core(binding)
    if not _is_epoch_seconds(facts.get("now")):
        raise _invalid("issue time must be epoch seconds")
    #: The capability names the feature it is issued for: the trailer is the
    #: candidate commit's own claim of ownership, so it must equal the target.
    if binding["trailers"]["Feature-Id"] != facts["target"]["entity_id"]:
        raise _invalid("binding trailers Feature-Id must name the target feature")
    if binding["expires_at"] <= facts["now"]:
        raise _invalid("capability expiry must be in the future")


def _validate_consume_facts(facts: dict[str, Any]) -> None:
    if not _closed_object(facts, CONSUME_FACT_FIELDS):
        raise _invalid("consume facts shape is not closed")
    if type(facts.get("schema_version")) is not str or facts["schema_version"] != CONSUME_FACTS_SCHEMA:
        raise _invalid("wrong consume facts schema")
    _validate_target(facts.get("target"))
    capability = facts.get("capability")
    if not _closed_object(capability, CAPABILITY_FIELDS):
        raise _invalid("capability row shape is not closed")
    _validate_binding_core(capability)
    if capability["trailers"]["Feature-Id"] != facts["target"]["entity_id"]:
        raise _invalid("capability trailers Feature-Id must name the target feature")
    if not _is_epoch_seconds(capability.get("uses_consumed")):
        raise _invalid("capability uses_consumed must be epoch-less non-negative int")
    if capability["uses_consumed"] > capability["max_uses"]:
        raise _invalid("capability uses_consumed exceeds max_uses")
    consumed_by = capability["consumed_by"]
    if consumed_by is not None and not _is_non_empty_str(consumed_by):
        raise _invalid("capability consumed_by must name an effect/command or be null")
    if (capability["uses_consumed"] == 0) != (consumed_by is None):
        raise _invalid("capability consumption count and identity disagree")
    if capability.get("revoked_at") is not None and not _is_epoch_seconds(
        capability.get("revoked_at")
    ):
        raise _invalid("capability revoked_at must be epoch seconds or null")
    # Only the outer controller envelope is trusted. Do not even inspect
    # presented until liveness has been decided.
    if not _is_epoch_seconds(facts.get("now")):
        raise _invalid("consume time must be epoch seconds")
    for current in ("current_epoch", "current_lease_epoch"):
        if not _is_epoch_seconds(facts.get(current)):
            raise _invalid("current epochs must be non-negative native integers")
    #: The controller issues under the feature's *current* epoch, so a bound
    #: epoch ahead of the current one is history the controller could not
    #: have produced — forged state, not staleness (the reverse direction,
    #: bound behind current, is the DAL-016 revocation-by-epoch and is judged
    #: as liveness below).
    if capability["capability_epoch"] > facts["current_epoch"]:
        raise _invalid("capability epoch is ahead of the current epoch")
    if capability["lease_epoch"] > facts["current_lease_epoch"]:
        raise _invalid("lease epoch is ahead of the current lease epoch")


def _is_within(path: str, prefix: str) -> bool:
    """Component-boundary containment (family form): ``src/dal`` covers
    ``src/dal/x.py`` and itself, never ``src/dalfoo``."""
    return path == prefix or path.startswith(prefix + "/")


def _inside_allowed(path: str, allowed_paths: list[dict[str, Any]]) -> bool:
    """The frozen allowed-paths semantics (DAL021-024 §plan)."""
    for entry in allowed_paths:
        if entry["path_type"] == "file":
            if path == entry["path"]:
                return True
        elif _is_within(path, entry["path"]):
            return True
    return False


def _presentation_violations(presented: Any, capability: dict[str, Any]) -> tuple[str, ...]:
    """Inspect only native containers/values; never invoke executor callbacks.

    An unsafe container cannot be dereferenced. Safe sibling fields are still
    checked, including trailer values when the trailer key set differs.
    """
    if not _native_object(presented):
        return ("presented must be a native object with native string keys",)
    violations: list[str] = []
    if frozenset(presented) != PRESENTED_FIELDS:
        violations.append("presented shape is not closed")
    for field in ("capability_id", "approval_id", "base_sha", "result_sha", "idempotency_key"):
        value = presented.get(field)
        if type(value) is not str:
            violations.append(f"presented {field} is not a native string")
        elif value != capability[field]:
            violations.append(f"presented {field} does not match the issued binding")

    trailers = presented.get("trailers")
    if not _native_object(trailers):
        violations.append("presented trailers must be a native object with native string keys")
    else:
        if frozenset(trailers) != TRAILER_KEYS:
            violations.append("presented trailer set diverges from the issued binding")
        if any(type(value) is not str for value in trailers.values()):
            violations.append("presented trailers are not native string pairs")
        for key in ("Feature-Id", "Task-Id", "Plan-Hash", "Review-Id"):
            value = trailers.get(key)
            if type(value) is not str:
                violations.append(f"presented trailer {key} is missing or not a native string")
            elif value != capability["trailers"][key]:
                violations.append(f"presented trailer {key} diverges from the issued binding")

    touched = presented.get("touched_paths")
    if type(touched) is not list or not touched:
        violations.append("presented touched_paths must be a non-empty native list")
    else:
        seen: set[str] = set()
        for index, path in enumerate(touched):
            if not _is_normalized_repo_path(path):
                violations.append(f"touched path at index {index} is not a native normalized path")
                continue
            if any(component == ".git" for component in path.split("/")):
                violations.append(f"touched path at index {index} names a .git subtree")
            if path in seen:
                violations.append(f"touched path at index {index} repeats a path")
            seen.add(path)
            if not _inside_allowed(path, capability["allowed_paths"]):
                # Labels must not echo arbitrary executor strings into audit.
                violations.append(f"touched path at index {index} is outside the allowed set")
    return tuple(violations)


def _policy_block(
    target: dict[str, Any], violations: tuple[str, ...]
) -> CommitCapabilityEvaluation:
    """The frozen BLK-POLICY--verified landing: verified → needs_human."""
    return CommitCapabilityEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(VERIFIED_STATE, BLOCK_STATE),
        final_state=BLOCK_STATE,
        final_entity_type=target["entity_type"],
        final_reason_code=BLOCK_REASON,
        final_reason_owner=BLOCK_OWNER,
        declared_write_set=(
            "aggregate",
            "business_event",
            "transition_receipt",
            "audit",
            "decision_create",
            "decision_projection",
            "notification_outbox",
        ),
        event_trace=(BLOCK_EVENT,),
        violations=violations,
    )


def _stale_refusal(target: dict[str, Any]) -> CommitCapabilityEvaluation:
    """The lease-family refusal: CAPABILITY_STALE with zero writes."""
    return CommitCapabilityEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.CAPABILITY_STALE,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(VERIFIED_STATE,),
        final_state=VERIFIED_STATE,
        final_entity_type=target["entity_type"],
    )


def issue_commit_capability(facts: dict[str, Any]) -> CommitCapabilityEvaluation:
    """Authorise one candidate-commit consumption under the presented binding.

    A go verdict is a pure declaration: it moves nothing and writes nothing —
    the controller persists the capability row and applies the engine
    transition; this judge only guarantees the binding is well-formed and
    the feature is verified. ``max_uses`` is frozen to 1: this is a one-time
    capability.
    """
    _validate_issue_facts(facts)
    return CommitCapabilityEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(VERIFIED_STATE,),
        final_state=VERIFIED_STATE,
        final_entity_type=facts["target"]["entity_type"],
    )


def consume_commit_capability(facts: dict[str, Any]) -> CommitCapabilityEvaluation:
    """Judge the deterministic executor's commit intent against the capability.

    Classification order: trusted shape → capability liveness → binding
    tampering. Every divergence on a live capability is labelled and lands
    the frozen block; a dead capability refuses before its binding is judged.
    """
    _validate_consume_facts(facts)
    target = facts["target"]
    capability = facts["capability"]
    presented = facts["presented"]

    # Liveness first (class 2): a dead capability is already bad.
    if capability["uses_consumed"] >= capability["max_uses"]:
        return _stale_refusal(target)
    if capability["revoked_at"] is not None:
        return _stale_refusal(target)
    if facts["now"] > capability["expires_at"]:
        return _stale_refusal(target)
    if capability["capability_epoch"] != facts["current_epoch"]:
        return _stale_refusal(target)
    if capability["lease_epoch"] != facts["current_lease_epoch"]:
        return _stale_refusal(target)

    # Binding tampering on a live capability (class 3): label every
    # divergence, then land the frozen block once.
    violations = _presentation_violations(presented, capability)
    if violations:
        return _policy_block(target, violations)

    return CommitCapabilityEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(VERIFIED_STATE,),
        final_state=VERIFIED_STATE,
        final_entity_type=target["entity_type"],
    )
