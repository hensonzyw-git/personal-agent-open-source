"""Enrollment tokens for the Worker Transport (DAL-R04/R05).

A token is `base64url(canonical payload).hmac_sha256(payload, service_key)`.
The payload is `{worker_id, capabilities, exp}` where `exp` is epoch seconds.
Verification is fail-closed: an undecodable, unverifiable, or expired token is
a refusal, never a partial identity. The token is opaque and short-lived; it is
never logged or echoed back in a response.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Final

from personal_agent_core.manifest import canonical_json


TOKEN_SCHEMA: Final[str] = "dal.worker-token/1.0"


class TokenError(Exception):
    """A token that cannot establish identity (invalid, tampered, or expired)."""


def _sign(payload_b64: str, key: bytes) -> str:
    return hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def issue_token(
    *,
    worker_id: str,
    capabilities: list[str],
    expires_at_epoch: int,
    key: bytes,
) -> str:
    """Issue an HMAC-signed, expiring enrollment token."""
    payload = canonical_json(
        {
            "schema": TOKEN_SCHEMA,
            "worker_id": worker_id,
            "capabilities": sorted(capabilities),
            "exp": expires_at_epoch,
        }
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64, key)}"


def verify_token(
    token: str,
    *,
    key: bytes,
    now_epoch: int,
) -> tuple[str, list[str]]:
    """Return `(worker_id, capabilities)` or raise `TokenError`."""
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError as exc:
        raise TokenError("malformed token") from exc
    if not hmac.compare_digest(_sign(payload_b64, key), signature):
        raise TokenError("bad signature")
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("undecodable payload") from exc
    worker_id = payload.get("worker_id")
    capabilities = payload.get("capabilities")
    exp = payload.get("exp")
    if (
        not isinstance(worker_id, str)
        or not worker_id
        or not isinstance(capabilities, list)
        or not all(isinstance(c, str) for c in capabilities)
        or not isinstance(exp, int)
    ):
        raise TokenError("payload shape is not closed")
    if now_epoch >= exp:
        raise TokenError("token expired")
    return worker_id, capabilities
