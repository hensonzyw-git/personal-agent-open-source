"""Server-derived state and artifact binding validation (DAL-011).

The client returns only the digests it observed.  It never supplies the object
the server hashes.  State bindings are rebuilt from the current ``Feature`` row
inside the consume transaction.  Artifact bindings and their canonical body
bytes are read through a protected-store reader and checked together.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

from personal_agent_dal.machine.registry import jcs_sha256
from personal_agent_dal.machine.transition_types import ReceiptCodes, TransitionRefused


STATE_BINDING_SCHEMA: Final[str] = "dal.state-binding/1.0"
ARTIFACT_BINDING_SCHEMA: Final[str] = "dal.artifact-binding/1.0"
STATE_BINDING_CANONICALIZER: Final[str] = "rfc8785-jcs"

STATE_BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "feature_id",
        "feature_version",
        "feature_state",
        "checkpoint_state",
        "reason_code",
        "plan_version",
        "artifact_sha256",
        "repository_id",
        "base_sha",
        "result_sha",
        "last_verified_sha",
        "decision_frontier_version",
        "policy_version",
        "capability_epoch",
        "external_effect_inventory_sha256",
    }
)

ARTIFACT_BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "artifact_schema_version",
        "media_type",
        "feature_id",
        "artifact_kind",
        "artifact_version",
        "base_sha",
        "body_canonicalization",
        "body_sha256",
        "body_size",
        "acceptance_sha256",
        "allowed_paths_sha256",
    }
)

KNOWN_BODY_CANONICALIZERS: Final[frozenset[str]] = frozenset(
    {"utf8-lf-nfc/1.0", "rfc8785-jcs/1.0"}
)


@dataclass(frozen=True)
class ArtifactReadback:
    """One protected artifact read: frozen binding plus canonical body bytes."""

    binding: Mapping[str, Any]
    canonical_body: bytes


ArtifactReader = Callable[[str], ArtifactReadback]


def _mapping_value(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return getattr(row, name)


def build_state_binding(feature: Any) -> dict[str, Any]:
    """Construct the exact ``dal.state-binding/1.0`` object from server state."""

    return {
        "schema_version": STATE_BINDING_SCHEMA,
        "feature_id": _mapping_value(feature, "feature_id"),
        "feature_version": _mapping_value(feature, "version"),
        "feature_state": _mapping_value(feature, "state"),
        "checkpoint_state": _mapping_value(feature, "checkpoint_state"),
        "reason_code": _mapping_value(feature, "reason_code"),
        "plan_version": _mapping_value(feature, "plan_version"),
        "artifact_sha256": _mapping_value(feature, "artifact_sha256"),
        "repository_id": _mapping_value(feature, "repository_id"),
        "base_sha": _mapping_value(feature, "base_sha"),
        "result_sha": _mapping_value(feature, "result_sha"),
        "last_verified_sha": _mapping_value(feature, "last_verified_sha"),
        "decision_frontier_version": _mapping_value(
            feature, "decision_frontier_version"
        ),
        "policy_version": _mapping_value(feature, "policy_version"),
        "capability_epoch": _mapping_value(feature, "capability_epoch"),
        "external_effect_inventory_sha256": _mapping_value(
            feature, "external_effect_inventory_sha256"
        ),
    }


def _require_exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], refusal_code: str, label: str
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise TransitionRefused(
            refusal_code,
            f"{label} field set is not closed: missing={missing}, extra={extra}",
        )


def _require_digest(value: Any, expected: str, code: str, label: str) -> str:
    try:
        actual = jcs_sha256(value)
    except ValueError:
        raise TransitionRefused(
            code, f"{label} is not representable as RFC 8785 I-JSON"
        ) from None
    if not hmac.compare_digest(actual, expected):
        raise TransitionRefused(code, f"{label} digest does not match")
    return actual


def validate_state_binding(
    *,
    current_binding: Mapping[str, Any],
    protected_binding_sha256: str,
    observed_binding_sha256: str | None,
) -> str:
    """Validate current server state against the decision and device digests."""

    _require_exact_fields(
        current_binding,
        STATE_BINDING_FIELDS,
        ReceiptCodes.DECISION_STALE,
        "state binding",
    )
    if current_binding.get("schema_version") != STATE_BINDING_SCHEMA:
        raise TransitionRefused(
            ReceiptCodes.DECISION_STALE, "unknown state-binding schema"
        )
    if observed_binding_sha256 is None:
        raise TransitionRefused(
            ReceiptCodes.DECISION_STALE, "observed state digest is required"
        )
    actual = _require_digest(
        current_binding,
        protected_binding_sha256,
        ReceiptCodes.DECISION_STALE,
        "current state binding",
    )
    if not hmac.compare_digest(actual, observed_binding_sha256):
        raise TransitionRefused(
            ReceiptCodes.DECISION_STALE,
            "device-observed state digest is not current",
        )
    return actual


def validate_artifact_binding(
    *,
    current_artifact: ArtifactReadback,
    protected_binding_sha256: str,
    observed_binding_sha256: str | None,
) -> str:
    """Validate the protected body and its exact artifact binding together."""

    binding = dict(current_artifact.binding)
    _require_exact_fields(
        binding,
        ARTIFACT_BINDING_FIELDS,
        ReceiptCodes.APPROVAL_INVALID,
        "artifact binding",
    )
    if binding.get("schema_version") != ARTIFACT_BINDING_SCHEMA:
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID, "unknown artifact-binding schema"
        )
    if binding.get("body_canonicalization") not in KNOWN_BODY_CANONICALIZERS:
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID, "unknown artifact body canonicalizer"
        )
    if observed_binding_sha256 is None:
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID, "observed artifact digest is required"
        )

    body = current_artifact.canonical_body
    if len(body) != binding.get("body_size"):
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID, "protected artifact body size drifted"
        )
    body_sha256 = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(body_sha256, str(binding.get("body_sha256"))):
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID, "protected artifact body hash drifted"
        )

    actual = _require_digest(
        binding,
        protected_binding_sha256,
        ReceiptCodes.APPROVAL_INVALID,
        "current artifact binding",
    )
    if not hmac.compare_digest(actual, observed_binding_sha256):
        raise TransitionRefused(
            ReceiptCodes.APPROVAL_INVALID,
            "device-observed artifact digest is not current",
        )
    return actual
