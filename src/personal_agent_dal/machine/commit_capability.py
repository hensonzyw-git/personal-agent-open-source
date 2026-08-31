"""`DAL-031`: the one-time commit capability (R09-A3).

The boundary between "the feature is verified" and "a candidate commit may
be created". Two pure gates in the ``patch_policy`` / ``review_fix_loop``
family (G2 guard form: no I/O, no operation spec id, no manifest entry, no
dispatch-graph import):

- **issue_commit_capability** — call-before. The trusted controller asserts
  the feature is `verified` and presents the binding the capability will
  carry: base/result SHAs, the approved plan's allowed paths, the four
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

The provider subprocess never sees any of this: the capability lives in
controller state, and the executor presents the commit intent it was
handed. Trailers are frozen by the technical design §9.3
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
    {"schema_version", "target", "capability", "presented", "now", "current_epoch"}
)
CAPABILITY_FIELDS: Final[frozenset[str]] = BINDING_FIELDS | {
    "uses_consumed",
    "revoked_at",
}
PRESENTED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "capability_id",
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


def _normalized_repo_path(value: Any, *, field: str) -> str:
    """Validate a canonical repo-relative POSIX path (DAL021-024 §plan)."""
    if not _is_non_empty_str(value):
        raise _invalid(f"{field} must be a non-empty string")
    if value.startswith("/"):
        raise _invalid(f"{field} must be repo-relative, not absolute")
    components = value.split("/")
    if any(component in (".", "..") for component in components):
        raise _invalid(f"{field} must not contain . or .. segments")
    if any(component == "" for component in components):
        raise _invalid(f"{field} must be a normalized path without empty segments")
    return value


def _validate_target(target: Any) -> None:
    if not isinstance(target, dict) or frozenset(target) != TARGET_FIELDS:
        raise _invalid("target shape is not closed")
    if (
        not _is_non_empty_str(target.get("entity_id"))
        or target.get("entity_type") != "feature"
        or target.get("state") != VERIFIED_STATE
        or not _is_epoch_seconds(target.get("version"))
    ):
        raise _invalid("commit capability target must be a verified feature")


def _validate_trailers(trailers: Any, *, field: str) -> None:
    """The closed four-trailer set with value-class checks."""
    if not isinstance(trailers, dict) or frozenset(trailers) != TRAILER_KEYS:
        raise _invalid(f"{field} must carry exactly the four frozen trailers")
    for key in ("Feature-Id", "Task-Id", "Review-Id"):
        if not _is_non_empty_str(trailers[key]):
            raise _invalid(f"{field} {key} must be a non-empty string")
    if not _is_sha256_hex(trailers["Plan-Hash"]):
        raise _invalid(f"{field} Plan-Hash must be a sha256 digest")


def _validate_allowed_paths(allowed_paths: Any) -> None:
    if not isinstance(allowed_paths, list) or not allowed_paths:
        raise _invalid("allowed_paths must be a non-empty list")
    for entry in allowed_paths:
        if not isinstance(entry, dict) or frozenset(entry) != ALLOWED_PATH_FIELDS:
            raise _invalid("allowed path shape is not closed")
        path = _normalized_repo_path(entry.get("path"), field="allowed path")
        # The frozen plan-artifact schema forbids the exact ``.git`` component
        # (``.github``/``.gitignore`` remain expressible in a plan).
        if any(component == ".git" for component in path.split("/")):
            raise _invalid("allowed path must not name a .git subtree")
        if entry.get("path_type") not in PATH_TYPES:
            raise _invalid("allowed path_type must be file or directory")


def _validate_binding_core(binding: dict[str, Any]) -> None:
    """The binding values shared by the issue binding and the consume row.

    The closed-set check lives at the two entries: the issue binding is
    exactly ``BINDING_FIELDS``, the consume row additionally carries the
    lifecycle fields (``uses_consumed``, ``revoked_at``).
    """
    if not isinstance(binding, dict):
        raise _invalid("capability binding must be an object")
    if (
        not _is_non_empty_str(binding.get("capability_id"))
        or not _is_git_sha_hex(binding.get("base_sha"))
        or not _is_git_sha_hex(binding.get("result_sha"))
        or not _is_non_empty_str(binding.get("idempotency_key"))
        or not _is_epoch_seconds(binding.get("expires_at"))
        or not _is_epoch_seconds(binding.get("capability_epoch"))
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
    if not isinstance(facts, dict) or frozenset(facts) != ISSUE_FACT_FIELDS:
        raise _invalid("issue facts shape is not closed")
    if facts.get("schema_version") != ISSUE_FACTS_SCHEMA:
        raise _invalid("wrong issue facts schema")
    _validate_target(facts.get("target"))
    binding = facts.get("binding")
    if not isinstance(binding, dict) or frozenset(binding) != BINDING_FIELDS:
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
    if not isinstance(facts, dict) or frozenset(facts) != CONSUME_FACT_FIELDS:
        raise _invalid("consume facts shape is not closed")
    if facts.get("schema_version") != CONSUME_FACTS_SCHEMA:
        raise _invalid("wrong consume facts schema")
    _validate_target(facts.get("target"))
    capability = facts.get("capability")
    if not isinstance(capability, dict) or frozenset(capability) != CAPABILITY_FIELDS:
        raise _invalid("capability row shape is not closed")
    _validate_binding_core(capability)
    if not _is_epoch_seconds(capability.get("uses_consumed")):
        raise _invalid("capability uses_consumed must be epoch-less non-negative int")
    if capability.get("revoked_at") is not None and not _is_epoch_seconds(
        capability.get("revoked_at")
    ):
        raise _invalid("capability revoked_at must be epoch seconds or null")
    presented = facts.get("presented")
    if not isinstance(presented, dict) or frozenset(presented) != PRESENTED_FIELDS:
        raise _invalid("presented shape is not closed")
    #: The presented commit intent is executor output, not trusted controller
    #: state — a str subclass can override hash/equality (round-7 review F5
    #: family), so here the non-native check is *not* a raise: values pass
    #: through to the binding comparison, where inequality (or a failed set
    #: operation) lands the tamper block, never a crash. Only list-shaped
    #: drift still raises, because the derivation dereferences it directly.
    touched = presented.get("touched_paths")
    if not isinstance(touched, list) or not touched:
        raise _invalid("touched_paths must be a non-empty list")
    for path in touched:
        _normalized_repo_path(path, field="touched path")
        if any(component == ".git" for component in path.split("/")):
            raise _invalid("touched path must not name a .git subtree")
    if len(set(touched)) != len(touched):
        raise _invalid("touched_paths must not repeat a path")
    if not _is_epoch_seconds(facts.get("now")) or not _is_epoch_seconds(
        facts.get("current_epoch")
    ):
        raise _invalid("consume time fields must be epoch seconds")
    #: The controller issues under the feature's *current* epoch, so a bound
    #: epoch ahead of the current one is history the controller could not
    #: have produced — forged state, not staleness (the reverse direction,
    #: bound behind current, is the DAL-016 revocation-by-epoch and is judged
    #: as liveness below).
    if capability["capability_epoch"] > facts["current_epoch"]:
        raise _invalid("capability epoch is ahead of the current epoch")


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

    # Binding tampering on a live capability (class 3): label every
    # divergence, then land the frozen block once.
    violations: list[str] = []
    #: Presentation values are executor output (untrusted): a str subclass can
    #: override hash/equality so an equal-value subclass would survive every
    #: comparison below — the native check is a labelled violation (block, not
    #: raise), the paired trusted-side shape in the capability row raises.
    for field in ("capability_id", "base_sha", "result_sha", "idempotency_key"):
        if type(presented[field]) is not str:
            violations.append(f"presented {field} is not a native string")
    if type(presented["trailers"]) is not dict or not all(
        type(key) is str and type(value) is str
        for key, value in presented["trailers"].items()
    ):
        violations.append("presented trailers are not native string pairs")
    for path in presented["touched_paths"]:
        if type(path) is not str:
            violations.append("presented touched_paths carry a non-native string")
    if presented["capability_id"] != capability["capability_id"]:
        violations.append(
            "presented capability_id does not match the issued capability"
        )
    if presented["base_sha"] != capability["base_sha"]:
        violations.append("presented base_sha does not match the issued binding")
    if presented["result_sha"] != capability["result_sha"]:
        violations.append("presented result_sha does not match the issued binding")
    if presented["idempotency_key"] != capability["idempotency_key"]:
        violations.append(
            "presented idempotency_key does not match the issuing key"
        )
    if frozenset(presented["trailers"]) != frozenset(capability["trailers"]):
        violations.append("presented trailer set diverges from the issued binding")
    else:
        for key in ("Task-Id", "Plan-Hash", "Review-Id"):
            if presented["trailers"][key] != capability["trailers"][key]:
                violations.append(f"presented trailer {key} diverges from the issued binding")
        if presented["trailers"]["Feature-Id"] != target["entity_id"]:
            violations.append("presented trailer Feature-Id does not name the target feature")
    for path in presented["touched_paths"]:
        if not _inside_allowed(path, capability["allowed_paths"]):
            violations.append(f"touched path {path} is outside the allowed set")
    if violations:
        return _policy_block(target, tuple(violations))

    return CommitCapabilityEvaluation(
        receipt=OperationReceipt(
            ReceiptCode.APPLIED,
            schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,
        ),
        state_trace=(VERIFIED_STATE,),
        final_state=VERIFIED_STATE,
        final_entity_type=target["entity_type"],
    )
