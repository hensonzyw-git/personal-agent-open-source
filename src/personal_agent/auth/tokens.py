"""Access token issuance and verification, per technical design 4.2.2.

A 10-minute ES256 bearer token, with no long-lived refresh token. The short TTL
is the containment for a stolen token, alongside TLS, Keychain storage, logs
that never record tokens, and a device-status lookup on every request.

The verifier is deliberately strict in the places JWT libraries are usually
lax. The algorithm comes from an allowlist rather than the token header, so
`alg: none` and RS256-for-ES256 substitution are impossible. The `kid` is
resolved against a key ring before any signature check, so an unknown key never
reaches verification. Every required claim must be present; a missing `exp` is a
rejection, not an unlimited token.

Verifying a token is not the same as authorising a request. Technical design 4.2
requires the current device status to be read on every call, so revocation takes
effect immediately instead of waiting for expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent_core.ids import new_id
from personal_agent_core.timeutil import utc_now


ALGORITHM: Final[str] = "ES256"
ISSUER: Final[str] = "personal-agent-api"
AUDIENCE: Final[str] = "personal-agent-api"

MAX_TTL: Final[timedelta] = timedelta(minutes=10)
CLOCK_SKEW: Final[timedelta] = timedelta(seconds=30)

REQUIRED_CLAIMS: Final[tuple[str, ...]] = (
    "iss",
    "aud",
    "sub",
    "device_id",
    "device_key_thumbprint",
    "scopes",
    "allowed_tools_version",
    "iat",
    "nbf",
    "exp",
    "jti",
)


class TokenError(Exception):
    """A token could not be issued or trusted."""


class UnknownSigningKeyError(TokenError):
    """The token names a `kid` this verifier does not hold."""


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_key: ec.EllipticCurvePrivateKey | None
    public_key: ec.EllipticCurvePublicKey


class TokenKeyRing:
    """One signing key, plus previous public keys retained for verification.

    Rotation keeps the previous public key for at least `MAX_TTL + 2 * skew`, so
    tokens already in flight stay valid while the old private key stops signing
    immediately. Access token keys and internal service JWT keys are separate
    material and are never shared.
    """

    def __init__(self, *, active: SigningKey, previous: list[SigningKey] = []) -> None:
        if active.private_key is None:
            raise TokenError("the active key must be able to sign")
        kids = [active.kid, *(key.kid for key in previous)]
        if len(kids) != len(set(kids)):
            raise TokenError("duplicate kid in the token key ring")
        self._active = active
        self._keys = {key.kid: key for key in [active, *previous]}

    @property
    def active_kid(self) -> str:
        return self._active.kid

    def verification_key(self, kid: str) -> ec.EllipticCurvePublicKey:
        try:
            return self._keys[kid].public_key
        except KeyError:
            raise UnknownSigningKeyError(f"unknown kid {kid!r}") from None

    def rotated(self, new_active: SigningKey) -> "TokenKeyRing":
        """Promote a new signing key, demoting the current one to verify-only."""
        if new_active.kid in self._keys:
            raise TokenError(f"kid {new_active.kid} is already in the ring")
        retained = [
            SigningKey(key.kid, None, key.public_key) for key in self._keys.values()
        ]
        return TokenKeyRing(active=new_active, previous=retained)


def issue_access_token(
    ring: TokenKeyRing,
    *,
    device_id: str,
    device_key_thumbprint: str,
    scopes: list[str],
    allowed_tools_version: str,
    now: datetime | None = None,
    ttl: timedelta = MAX_TTL,
) -> str:
    """Mint a short-lived access token for one device."""
    if ttl > MAX_TTL:
        raise TokenError(f"ttl {ttl} exceeds the {MAX_TTL} maximum")
    issued_at = now or utc_now()
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"device:{device_id}",
        "device_id": device_id,
        "device_key_thumbprint": device_key_thumbprint,
        "scopes": scopes,
        "allowed_tools_version": allowed_tools_version,
        "iat": int(issued_at.timestamp()),
        "nbf": int(issued_at.timestamp()),
        "exp": int((issued_at + ttl).timestamp()),
        "jti": new_id(),
    }
    return jwt.encode(
        claims,
        ring._active.private_key,
        algorithm=ALGORITHM,
        headers={"kid": ring.active_kid, "typ": "JWT"},
    )


def verify_access_token(
    ring: TokenKeyRing, token: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Validate a token and return its claims, or raise.

    This proves the token was minted by this service and has not expired. It
    says nothing about whether the device is still registered; callers must read
    the current device status separately.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise TokenError("token header is unreadable") from exc

    algorithm = header.get("alg")
    if algorithm != ALGORITHM:
        # Never take the algorithm from the token itself.
        raise TokenError(f"unexpected algorithm {algorithm!r}")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TokenError("token header carries no kid")

    public_key = ring.verification_key(kid)

    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={
                "require": list(REQUIRED_CLAIMS),
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                # Time is checked below against an injected reference rather
                # than the wall clock, so verification is deterministic and the
                # skew rule is visible instead of buried in library defaults.
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"token rejected: {type(exc).__name__}") from exc

    for claim in ("iat", "nbf", "exp"):
        if not isinstance(claims[claim], int) or isinstance(claims[claim], bool):
            raise TokenError(f"{claim} must be an integer NumericDate")

    reference = (now or utc_now()).timestamp()
    skew = CLOCK_SKEW.total_seconds()
    if claims["exp"] - claims["iat"] > MAX_TTL.total_seconds():
        raise TokenError("token lifetime exceeds the maximum")
    if claims["exp"] <= reference - skew:
        raise TokenError("token has expired")
    if claims["nbf"] > reference + skew:
        raise TokenError("token is not yet valid")
    if claims["iat"] > reference + skew:
        raise TokenError("token was issued in the future")
    if claims["sub"] != f"device:{claims['device_id']}":
        raise TokenError("subject does not match device_id")
    return claims
