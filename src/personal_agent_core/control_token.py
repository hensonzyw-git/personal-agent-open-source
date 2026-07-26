"""The internal control-plane token, shared so the two sides cannot drift.

The scheduler and the crash-recovery scan on the Agent API read Finance MCP
through a private control API (technical design 7.6.1 and 7.7): "get the
execution for this idempotency key", "list the successful writes committed on
this day". Those reads are not tool calls, so they do not carry a per-call Host
Context — there is no tool, no arguments and no idempotency key to bind for a
date query.

They still must be authenticated, and with a token that cannot be mistaken for a
tool-call token. Two things keep the planes apart:

- a distinct audience, `personal-data-mcp-control`. A tool-call Host Context has
  `aud=personal-data-mcp`, so neither token verifies where the other is
  expected, even though both are signed with the same service key material;
- the token names the exact read it authorises. A token minted to look up one
  execution cannot be replayed to look up another, and one minted for a date
  query cannot read an execution at all.

Finance MCP holds only the verification key. It never signs, and the signing
helper here exists for the Agent API side and for tests.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import jwt

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import (
    ALGORITHM,
    CLOCK_SKEW,
    ISSUER,
    MAX_TTL,
    SUBJECT,
    ServiceKeyRing,
)
from personal_agent_core.timeutil import utc_now


CONTROL_AUDIENCE: Final[str] = "personal-data-mcp-control"
DEFAULT_CONTROL_TTL: Final[timedelta] = timedelta(seconds=60)

#: How many record pointers one batch read may carry. It lives here, with the
#: resource hash, because it is a contract *between* the two services: the
#: server refuses a larger batch, so the client has to split at the same number.
#: Keeping the cap on one side only is what makes an oversized card unopenable
#: rather than slow.
MAX_RECORD_BATCH: Final[int] = 100


class ControlAction(StrEnum):
    """The reads the control plane may authorise, named so each is bindable."""

    GET_EXECUTION = "get_execution"
    LIST_SUCCESSFUL_WRITES = "list_successful_writes"
    #: "Which duplicate check blocked the write I sent under this idempotency
    #: key?" The id is deliberately not on the MCP result: that is the
    #: model-facing channel, and design 5.2 requires the model never to see or
    #: forge a `duplicate_check_id`.
    GET_PENDING_DUPLICATE_CHECK = "get_pending_duplicate_check"
    #: "What does the ledger hold for these records *now*?" Design 7.7 step 5: a
    #: review card must show the current fields, so a correction made in Feishu
    #: on the computer is visible immediately.
    #:
    #: Deliberately batch-only. A card is opened as a whole, so one request
    #: covers it behind one schema validation, and the resource is a hash of the
    #: exact ordered pointers -- a token cannot be replayed against a different
    #: body, a different record, or the same id in another table. A separate
    #: single-record route existed briefly and had no caller; a second
    #: authenticated endpoint for the same job is surface, not convenience.
    GET_RECORD_FIELDS_BATCH = "get_record_fields_batch"


class ControlTokenError(Exception):
    """The control token could not be produced."""


def record_batch_resource(records: list[tuple[str, str]]) -> str:
    """Bind a batch token to the exact ordered record pointers in its body."""
    canonical = json.dumps(
        [
            {"table_kind": table_kind, "record_id": record_id}
            for table_kind, record_id in records
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def sign_control_token(
    ring: ServiceKeyRing,
    *,
    action: ControlAction,
    resource: str,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_CONTROL_TTL,
) -> str:
    """Mint a token for exactly one control read of one resource."""
    if ttl > MAX_TTL:
        raise ControlTokenError(f"ttl {ttl} exceeds the {MAX_TTL} maximum")
    issued_at = now or utc_now()
    claims = {
        "iss": ISSUER,
        "aud": CONTROL_AUDIENCE,
        "sub": SUBJECT,
        "iat": int(issued_at.timestamp()),
        "nbf": int(issued_at.timestamp()),
        "exp": int((issued_at + ttl).timestamp()),
        "control_action": str(action),
        "control_resource": resource,
    }
    return jwt.encode(
        claims,
        ring.signing_key(),
        algorithm=ALGORITHM,
        headers={"kid": ring.active_kid, "typ": "JWT"},
    )


def verify_control_token(
    ring: ServiceKeyRing,
    token: str,
    *,
    action: ControlAction,
    resource: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check a control token against the exact read it is being used for.

    Raises `HOST_CONTEXT_MISMATCH` on any disagreement, the same stable code the
    tool-call gate uses, so a client cannot tell an auth failure on one plane
    from the other.
    """

    def mismatch(detail: str) -> AppError:
        return AppError(ErrorCode.HOST_CONTEXT_MISMATCH, internal_detail=detail)

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise mismatch("unreadable control header") from exc
    if header.get("alg") != ALGORITHM:
        raise mismatch(f"unexpected alg {header.get('alg')!r}")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise mismatch("missing kid")

    public_key = ring.verification_key(kid)
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[ALGORITHM],
            audience=CONTROL_AUDIENCE,
            issuer=ISSUER,
            options={
                "require": ["iss", "aud", "sub", "iat", "nbf", "exp"],
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise mismatch(f"control token rejected: {type(exc).__name__}") from exc

    reference = (now or utc_now()).timestamp()
    skew = CLOCK_SKEW.total_seconds()
    if claims["exp"] - claims["iat"] > MAX_TTL.total_seconds():
        raise mismatch("control token lifetime exceeds the maximum")
    if claims["exp"] <= reference - skew:
        raise mismatch("control token has expired")
    if claims["nbf"] > reference + skew:
        raise mismatch("control token is not yet valid")
    if claims.get("sub") != SUBJECT:
        raise mismatch("unexpected subject")
    if claims.get("control_action") != str(action):
        raise mismatch("control token authorises a different action")
    if claims.get("control_resource") != resource:
        raise mismatch("control token authorises a different resource")
    return claims
