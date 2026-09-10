"""Host Context and its signed binding, per technical design 4.3.

Finance MCP is reachable on loopback. Loopback is not an authorisation boundary,
so every call carries a short-lived internal JWT that binds the request to one
specific tool invocation. Both services need the same canonical argument hash to
agree, which is why this lives in the shared core rather than in either service.

The binding is what makes a stolen internal token useless for anything but the
one call it was minted for. A leaked bearer token that only said "the Agent API
sent this" could be replayed with different headers or different arguments; a
token that names the tool, the idempotency key, the request fingerprint and the
hash of the arguments cannot.

Two rules are enforced here rather than trusted to callers:

- the model never supplies any of these fields. The Agent Host drops them before
  signing, while the Finance MCP rejects them if they nevertheless arrive. They
  are excluded from the business-argument hash so they cannot be confused with
  model-visible input;
- the receiver recomputes the argument hash from what it actually received and
  compares field by field. Matching claims against a request that was modified
  in flight fails closed with a single stable error.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json
from personal_agent_core.timeutil import utc_now


ALGORITHM: Final[str] = "ES256"
ISSUER: Final[str] = "personal-agent-api"
AUDIENCE: Final[str] = "personal-data-mcp"
SUBJECT: Final[str] = "service:personal-agent-api"

DEFAULT_TTL: Final[timedelta] = timedelta(seconds=60)
MAX_TTL: Final[timedelta] = timedelta(minutes=5)
CLOCK_SKEW: Final[timedelta] = timedelta(seconds=30)

#: Names that only the Host may set. If any appears in model output it is
#: discarded, never merged, and never allowed to reach the argument hash —
#: unless the target tool's own contract declares that name as one of its
#: business fields (see `declared_model_fields`).
HOST_ONLY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "request_id",
        "idempotency_key",
        "user_id",
        "device_id",
        "granted_scopes",
        "timezone",
        "trace_id",
        "duplicate_override",
        "arguments_hash",
        "request_fingerprint",
        "allowed_tools_version",
    }
)

BOUND_CLAIMS: Final[tuple[str, ...]] = (
    "agent_id",
    "device_id",
    "user_id",
    "scopes",
    "tool",
    "request_id",
    "trace_id",
    "idempotency_key",
    "request_fingerprint",
    "arguments_hash",
    "allowed_tools_version",
    "timezone",
)


class HostContextError(Exception):
    """The signed context could not be produced."""


def declared_model_fields(input_schema: dict[str, Any]) -> frozenset[str]:
    """The top-level field names a tool's own contract declares.

    A host-only name and a business field can collide. `timezone` is the Host
    Context's message day-boundary *and* the calendar event's IANA zone
    (`calendar.create_event`, design §2.2): dropping the second because the
    first exists would silently write a Tokyo event as Asia/Shanghai.

    The exemption is therefore derived from the target tool's own closed
    schema rather than a hand-maintained list, and both sides of the binding
    — the Host that signs and the MCP server that verifies — read it from the
    same trusted manifest, so no caller can widen it. The names that may
    actually collide are pinned by a contract test.
    """
    return frozenset(input_schema.get("properties", {}))


def strip_host_only_fields(
    arguments: dict[str, Any], *, declared: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Remove anything the model is not allowed to decide.

    This is the Agent Host's pre-signing cleanup. The Finance MCP independently
    rejects any Host-only key that reaches its raw request boundary.

    `declared` carries the target tool's own top-level field names; a name in
    both `HOST_ONLY_FIELDS` and `declared` is that tool's business field and
    survives.
    """
    return {
        key: value
        for key, value in arguments.items()
        if key not in HOST_ONLY_FIELDS or key in declared
    }


def arguments_hash(
    arguments: dict[str, Any], *, declared: frozenset[str] = frozenset()
) -> str:
    """Canonical hash over the model-visible arguments only.

    A declared field is model-visible, so it is covered by the hash: a
    tampered event timezone must not verify against an honest signature.
    """
    return hashlib.sha256(
        canonical_json(strip_host_only_fields(arguments, declared=declared)).encode(
            "utf-8"
        )
    ).hexdigest()


@dataclass(frozen=True)
class HostContext:
    """What the Host asserts about one tool invocation."""

    agent_id: str
    device_id: str
    user_id: str
    scopes: tuple[str, ...]
    tool: str
    request_id: str
    trace_id: str
    idempotency_key: str
    request_fingerprint: str
    allowed_tools_version: str
    timezone: str = "Asia/Shanghai"
    #: A `duplicate_check_id` this Host is releasing for exactly this call. It
    #: travels as a signed claim rather than an argument because design 5.2
    #: requires that the model can neither read nor forge it: an argument is the
    #: model-facing channel, and `duplicate_override` is in `HOST_ONLY_FIELDS`
    #: precisely so a model-emitted copy is stripped before the hash is taken.
    duplicate_override: str | None = None

    def claims(
        self, arguments: dict[str, Any], *, declared: frozenset[str] = frozenset()
    ) -> dict[str, Any]:
        claims = {
            "agent_id": self.agent_id,
            "device_id": self.device_id,
            "user_id": self.user_id,
            "scopes": list(self.scopes),
            "tool": self.tool,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "idempotency_key": self.idempotency_key,
            "request_fingerprint": self.request_fingerprint,
            "arguments_hash": arguments_hash(arguments, declared=declared),
            "allowed_tools_version": self.allowed_tools_version,
            "timezone": self.timezone,
        }
        # Emitted only when there is one, so a plain call's token carries no
        # override claim at all and cannot be confused with one that does.
        if self.duplicate_override is not None:
            claims["duplicate_override"] = self.duplicate_override
        return claims


@dataclass(frozen=True)
class ServiceKey:
    kid: str
    private_key: ec.EllipticCurvePrivateKey | None
    public_key: ec.EllipticCurvePublicKey


class ServiceKeyRing:
    """Internal service keys.

    Separate material from the access-token ring: Finance MCP holds only the
    verification public key and never the Agent API's signing key, so a
    compromise of one service cannot mint the other's tokens.
    """

    def __init__(
        self, *, active: ServiceKey, previous: list[ServiceKey] = []
    ) -> None:
        kids = [active.kid, *(key.kid for key in previous)]
        if len(kids) != len(set(kids)):
            raise HostContextError("duplicate kid in the service key ring")
        self._active = active
        self._keys = {key.kid: key for key in [active, *previous]}

    @property
    def active_kid(self) -> str:
        return self._active.kid

    def signing_key(self) -> ec.EllipticCurvePrivateKey:
        if self._active.private_key is None:
            raise HostContextError("this ring holds verification keys only")
        return self._active.private_key

    def verification_key(self, kid: str) -> ec.EllipticCurvePublicKey:
        try:
            return self._keys[kid].public_key
        except KeyError:
            raise AppError(
                ErrorCode.HOST_CONTEXT_MISMATCH,
                internal_detail=f"unknown service kid {kid!r}",
            ) from None

    def public_only(self) -> "ServiceKeyRing":
        """The ring as Finance MCP holds it: no private key material."""
        keys = [ServiceKey(k.kid, None, k.public_key) for k in self._keys.values()]
        active = next(key for key in keys if key.kid == self.active_kid)
        others = [key for key in keys if key.kid != self.active_kid]
        return ServiceKeyRing(active=active, previous=others)

    def rotated(self, new_active: ServiceKey) -> "ServiceKeyRing":
        if new_active.kid in self._keys:
            raise HostContextError(f"kid {new_active.kid} is already in the ring")
        retained = [
            ServiceKey(key.kid, None, key.public_key) for key in self._keys.values()
        ]
        return ServiceKeyRing(active=new_active, previous=retained)


def sign_host_context(
    ring: ServiceKeyRing,
    context: HostContext,
    arguments: dict[str, Any],
    *,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_TTL,
    declared: frozenset[str] = frozenset(),
) -> str:
    """Mint the per-call internal token.

    `declared` must be the target tool's own declared field names, derived
    from the same contract the verifier will use.
    """
    if ttl > MAX_TTL:
        raise HostContextError(f"ttl {ttl} exceeds the {MAX_TTL} maximum")
    issued_at = now or utc_now()
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "iat": int(issued_at.timestamp()),
        "nbf": int(issued_at.timestamp()),
        "exp": int((issued_at + ttl).timestamp()),
        "jti": context.request_id,
        **context.claims(arguments, declared=declared),
    }
    return jwt.encode(
        claims,
        ring.signing_key(),
        algorithm=ALGORITHM,
        headers={"kid": ring.active_kid, "typ": "JWT"},
    )


def verify_host_context(
    ring: ServiceKeyRing,
    token: str,
    *,
    tool: str,
    idempotency_key: str,
    request_id: str,
    user_id: str,
    trace_id: str,
    timezone: str,
    arguments: dict[str, Any],
    duplicate_override: str | None = None,
    now: datetime | None = None,
    declared: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Check the token against what actually arrived.

    Raises `HOST_CONTEXT_MISMATCH` on any disagreement. The caller must not
    create an execution record before this returns: a request whose binding does
    not hold has to leave no trace of having been half accepted.
    """

    def mismatch(detail: str) -> AppError:
        return AppError(
            ErrorCode.HOST_CONTEXT_MISMATCH, internal_detail=detail
        )

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise mismatch("unreadable header") from exc
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
            audience=AUDIENCE,
            issuer=ISSUER,
            options={
                "require": ["iss", "aud", "sub", "iat", "nbf", "exp", "jti"],
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise mismatch(f"token rejected: {type(exc).__name__}") from exc

    reference = (now or utc_now()).timestamp()
    skew = CLOCK_SKEW.total_seconds()
    if claims["exp"] - claims["iat"] > MAX_TTL.total_seconds():
        raise mismatch("service token lifetime exceeds the maximum")
    if claims["exp"] <= reference - skew:
        raise mismatch("service token has expired")
    if claims["nbf"] > reference + skew:
        raise mismatch("service token is not yet valid")
    if claims.get("sub") != SUBJECT:
        raise mismatch("unexpected subject")

    for claim in BOUND_CLAIMS:
        if claim not in claims:
            raise mismatch(f"missing bound claim {claim}")

    # Compare against what was actually received, not against the token's own
    # copy of it.
    observed = {
        "tool": tool,
        "idempotency_key": idempotency_key,
        "request_id": request_id,
        "user_id": user_id,
        "trace_id": trace_id,
        "timezone": timezone,
        "arguments_hash": arguments_hash(arguments, declared=declared),
    }
    for field, value in observed.items():
        if claims[field] != value:
            raise mismatch(f"{field} does not match the signed context")
    # Absent on both sides is the normal call. Any disagreement -- a header
    # without a claim, a claim without a header, or two different ids -- is a
    # mismatch, so an override can only release the check the Host signed for.
    if claims.get("duplicate_override") != duplicate_override:
        raise mismatch("duplicate_override does not match the signed context")
    return claims
