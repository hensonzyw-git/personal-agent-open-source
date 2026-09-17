"""Operator tokens for the Dev Workflow Service (DAL-R08).

A separate identity domain from the worker token (`service.tokens`): an
operator token carries `operator_id` + closed `capability` set (`read`,
`control`) and never a `worker_id`/`capabilities` pair. The two schemas are
distinct strings (`dal.operator-token/1.0` vs `dal.worker-token/1.0`) and each
verifier refuses a foreign schema before anything else, so a worker token
presented at an operator endpoint is a refusal, not a capability confusion.
Like the worker token, it is fail-closed: undecodable, unverifiable, expired,
or foreign-schema tokens are refusals, never partial identity. It is opaque,
short-lived, returned exactly once by the issuing channel, and never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Final

from personal_agent_core.manifest import canonical_json


TOKEN_SCHEMA: Final[str] = "dal.operator-token/1.0"

#: Closed operator capability vocabulary. `read` covers the read-only
#: endpoints; `control` covers mutating operator actions. Endpoint
#: authorization depends on these, unlike worker capabilities (descriptive
#: registration only).
OPERATOR_CAPABILITIES: Final[tuple[str, ...]] = ("read", "control")


class OperatorTokenError(Exception):
    """A token that cannot establish operator identity."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sign(payload_b64: str, key: bytes) -> str:
    return hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def issue_operator_token(
    *,
    operator_id: str,
    capabilities: list[str],
    expires_at_epoch: int,
    key: bytes,
) -> str:
    """Issue an HMAC-signed, expiring operator token.

    The issuer enforces the same closed rules the verifier applies, so a
    token that passes here can never be rejected by the verifier for payload
    shape (duplicates, unknown capabilities, non-string id).
    """
    if not isinstance(operator_id, str) or not operator_id:
        raise ValueError("operator_id must be a non-empty string")
    if sorted(capabilities) != sorted(set(capabilities)):
        raise ValueError("duplicate operator capability")
    unknown = [c for c in capabilities if c not in OPERATOR_CAPABILITIES]
    if unknown:
        raise ValueError(f"unknown operator capability: {unknown!r}")
    payload = canonical_json(
        {
            "schema": TOKEN_SCHEMA,
            "operator_id": operator_id,
            "capabilities": sorted(capabilities),
            "exp": expires_at_epoch,
        }
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64, key)}"


def verify_operator_token(
    token: str,
    *,
    key: bytes,
    now_epoch: int,
) -> tuple[str, list[str]]:
    """Return `(operator_id, capabilities)` or raise `OperatorTokenError`."""
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError as exc:
        raise OperatorTokenError("malformed token") from exc
    if not hmac.compare_digest(_sign(payload_b64, key), signature):
        raise OperatorTokenError("bad signature")
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise OperatorTokenError("undecodable payload") from exc
    # A validly-signed payload whose top level is not an object (list, string,
    # null) is a refusal, not a 500: nothing about it may be read as fields.
    if not isinstance(payload, dict):
        raise OperatorTokenError("payload is not an object")
    # Schema check precedes field checks: a worker token is a foreign identity
    # domain, and its fields must never be read as operator fields.
    if payload.get("schema") != TOKEN_SCHEMA:
        raise OperatorTokenError("wrong token schema")
    operator_id = payload.get("operator_id")
    capabilities = payload.get("capabilities")
    exp = payload.get("exp")
    if (
        not isinstance(operator_id, str)
        or not operator_id
        or not isinstance(capabilities, list)
        or not all(isinstance(c, str) for c in capabilities)
        or not isinstance(exp, int)
    ):
        raise OperatorTokenError("invalid payload fields")
    if sorted(capabilities) != sorted(set(capabilities)):
        raise OperatorTokenError("duplicate capability")
    if any(c not in OPERATOR_CAPABILITIES for c in capabilities):
        raise OperatorTokenError("unknown capability")
    if exp <= now_epoch:
        raise OperatorTokenError("expired token")
    return operator_id, capabilities
