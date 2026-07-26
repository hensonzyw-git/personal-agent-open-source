"""The device identity surface of design 5.1: enrollment, tokens, devices.

`DEV-029`. Three of these six handlers are the **only unauthenticated public
endpoints in the system**, so they are written to the §5.1 rule for an
attacker-facing boundary: every failure shape was chosen before the code, each
one fails closed, and nothing about why a request was refused travels outward.

Four properties are the point of this module, and each is a test:

- **the client never names its own authority.** A body is parsed against a
  closed field set, so `scopes`, `status`, `device_id` or
  `allowed_tools_version` in an enrollment request is a refusal, not a field
  that gets ignored and might later get read.
- **a refusal is one refusal.** An unknown, spent and expired enrollment code
  are indistinguishable outward, as are every reason a challenge or a signature
  can fail. The operator can tell them apart in `internal_detail`; a caller
  cannot use the surface as an oracle.
- **a token states the device's authority as it is now.** Scopes, the manifest
  binding and the thumbprint are read from the device row at issuance, never
  carried over from whatever was true when the challenge was minted.
- **`device.manage` is not self-service.** It manages *other* devices; a push
  token is bound to the calling device and cannot be written for another one
  even by an administrator device.

Errors here deliberately do **not** use the Finance `ErrorCode` catalogue for
authentication outcomes. That catalogue is the model- and tool-facing contract;
an enrollment or signature refusal is neither, and `_Unauthenticated` already
set the precedent that the auth surface answers in its own shape. Malformed
input is still `INVALID_ARGUMENT`, because that is a request defect rather than
an authentication decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from personal_agent.auth.device_keys import DeviceKeyError, load_device_public_key
from personal_agent.auth.enrollment import (
    ChallengeError,
    DEVICE_MANAGE_SCOPE,
    EnrollmentError,
    claim_enrollment_code,
    consume_challenge,
    create_challenge,
    decode_device_scopes,
    revoke_device,
)
from personal_agent.auth.tokens import MAX_TTL, TokenKeyRing, issue_access_token
from personal_agent.storage.models import Device
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode


#: Scope names design 5.1 assigns to the device surface itself.
SELF_READ_SCOPE: Final[str] = "device.self.read"
SELF_REVOKE_SCOPE: Final[str] = "device.self.revoke"

MAX_DISPLAY_NAME_CHARS: Final[int] = 64

#: An APNs device token is hex. Apple documents neither a fixed length nor a
#: stable one, so the bound is generous and the *alphabet* does the work: a
#: non-hex token cannot reach the provider client or the log redactor.
MIN_PUSH_TOKEN_CHARS: Final[int] = 64
MAX_PUSH_TOKEN_CHARS: Final[int] = 256

_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")


class EnrollmentRejected(Exception):
    """An enrollment could not be completed. Outward: one opaque refusal."""


class DeviceAuthRejected(Exception):
    """A challenge or signature was refused. Outward: one opaque refusal."""


@dataclass(frozen=True)
class IssuedToken:
    """What `POST /v1/auth/tokens` returns. The token itself is never logged."""

    access_token: str
    expires_at: datetime
    device_id: str
    scopes: tuple[str, ...]
    allowed_tools_version: str

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_at": self.expires_at.isoformat(),
            "expires_in": int(MAX_TTL.total_seconds()),
            "device_id": self.device_id,
            "scopes": list(self.scopes),
            "allowed_tools_version": self.allowed_tools_version,
        }


def _closed_body(body: dict[str, Any], allowed: frozenset[str]) -> None:
    """Refuse a body carrying anything outside the contract.

    Ignoring an unexpected field is the more forgiving choice and the wrong one:
    a client that can send `scopes` today without an error has been told the
    field is meaningful, and the next reader of this code may believe it.
    """
    unexpected = sorted(set(body) - allowed)
    if unexpected:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"unexpected fields in request body: {unexpected}",
        )


def _text(body: dict[str, Any], field: str, *, max_chars: int) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"{field} is required",
        )
    if len(value) > max_chars:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"{field} must be at most {max_chars} characters",
        )
    if any(character < " " or character == "\x7f" for character in value):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"{field} must not contain control characters",
        )
    return value


# --- POST /v1/enrollments/claim ----------------------------------------------


def claim_device(
    session,
    *,
    body: dict[str, Any],
    allowed_tools_version: str,
    now: datetime,
) -> dict[str, Any]:
    """Register a device public key against a one-time code.

    `allowed_tools_version` is the *server's* current manifest version, passed in
    by composition. A device enrolled against one manifest is granted the tools
    of that manifest and nothing else, so this value can never come from the
    request.
    """
    _closed_body(body, frozenset({"code", "public_key", "display_name"}))
    code = _text(body, "code", max_chars=128)
    public_key = _text(body, "public_key", max_chars=256)
    display_name = _text(body, "display_name", max_chars=MAX_DISPLAY_NAME_CHARS)

    try:
        load_device_public_key(public_key)
    except DeviceKeyError as exc:
        # A malformed key is a request defect, and saying which way it is
        # malformed helps the app developer without helping an attacker: the
        # code is what gates enrollment, and it has already been presented.
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"public_key is not a P-256 X9.63 point: {exc}",
        ) from exc

    try:
        device = claim_enrollment_code(
            session,
            code=code,
            public_key_b64u=public_key,
            display_name=display_name,
            allowed_tools_version=allowed_tools_version,
            now=now,
        )
    except EnrollmentError as exc:
        raise EnrollmentRejected(str(exc)) from exc

    return {
        "device_id": device.device_id,
        "display_name": device.display_name,
        "device_key_thumbprint": device.device_key_thumbprint,
        "scopes": list(decode_device_scopes(device.scopes)),
        "allowed_tools_version": device.allowed_tools_version,
        "created_at": device.created_at.isoformat(),
    }


# --- POST /v1/auth/challenges ------------------------------------------------


def issue_device_challenge(
    session, *, body: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Mint a single-use challenge for an active device."""
    _closed_body(body, frozenset({"device_id"}))
    device_id = _text(body, "device_id", max_chars=64)
    try:
        challenge = create_challenge(session, device_id=device_id, now=now)
    except ChallengeError as exc:
        # Unknown and revoked are the same answer: otherwise this endpoint tells
        # a caller which device ids exist.
        raise DeviceAuthRejected(str(exc)) from exc
    return {
        "challenge_id": challenge.challenge_id,
        "device_id": challenge.device_id,
        "nonce": challenge.nonce,
        "audience": "personal-agent-api",
        "expires_at": challenge.expires_at.isoformat(),
    }


# --- POST /v1/auth/tokens ----------------------------------------------------


def issue_device_token(
    session,
    *,
    body: dict[str, Any],
    token_ring: TokenKeyRing,
    now: datetime,
) -> IssuedToken:
    """Verify a challenge signature and mint one 10-minute access token."""
    _closed_body(body, frozenset({"challenge_id", "device_id", "signature", "nonce"}))
    challenge_id = _text(body, "challenge_id", max_chars=64)
    device_id = _text(body, "device_id", max_chars=64)
    signature = _text(body, "signature", max_chars=256)
    nonce = _text(body, "nonce", max_chars=64)

    try:
        device = consume_challenge(
            session,
            challenge_id=challenge_id,
            device_id=device_id,
            nonce=nonce,
            signature_b64u=signature,
            now=now,
        )
    except ChallengeError as exc:
        raise DeviceAuthRejected(str(exc)) from exc

    # Authority is read here, not at challenge time: a scope change or a
    # revocation between the two must apply to this token.
    scopes = decode_device_scopes(device.scopes)
    token = issue_access_token(
        token_ring,
        device_id=device.device_id,
        device_key_thumbprint=device.device_key_thumbprint,
        scopes=list(scopes),
        allowed_tools_version=device.allowed_tools_version,
        now=now,
    )
    return IssuedToken(
        access_token=token,
        expires_at=now + MAX_TTL,
        device_id=device.device_id,
        scopes=scopes,
        allowed_tools_version=device.allowed_tools_version,
    )


# --- GET /v1/devices --------------------------------------------------------


def _projection(device: Device, *, is_self: bool) -> dict[str, Any]:
    """What a device row looks like outward.

    The public key, the thumbprint and the sealed push token are all absent.
    None of them is a secret the client does not already have about itself, and
    all of them are identifiers of *other* devices; design 5.1 says this
    response carries no key material at all, so the projection cannot grow one
    by accident.
    """
    return {
        "device_id": device.device_id,
        "display_name": device.display_name,
        "status": device.status,
        "is_self": is_self,
        "has_push_token": device.encrypted_push_token is not None,
        "allowed_tools_version": device.allowed_tools_version,
        "created_at": device.created_at.isoformat(),
        "revoked_at": (
            device.revoked_at.isoformat() if device.revoked_at is not None else None
        ),
    }


def list_devices(session, *, device_id: str, scopes: tuple[str, ...]) -> dict[str, Any]:
    """List this device, or every device when the caller holds `device.manage`."""
    if DEVICE_MANAGE_SCOPE in scopes:
        rows = (
            session.query(Device).order_by(Device.created_at.asc(), Device.device_id).all()
        )
    else:
        _require(SELF_READ_SCOPE, scopes)
        row = session.get(Device, device_id)
        rows = [] if row is None else [row]
    return {
        "devices": [
            _projection(row, is_self=row.device_id == device_id) for row in rows
        ]
    }


# --- DELETE /v1/devices/{device_id} -----------------------------------------


def revoke_device_by_id(
    session, *, caller_device_id: str, scopes: tuple[str, ...], device_id: str, now: datetime
) -> dict[str, Any]:
    """Revoke a device. Self-revocation is a device's own right."""
    if device_id == caller_device_id:
        _require(SELF_REVOKE_SCOPE, scopes)
    else:
        _require(DEVICE_MANAGE_SCOPE, scopes)

    if session.get(Device, device_id) is None:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=f"no such device {device_id}",
        )
    # A second revocation is not an error: the caller asked for a state that
    # already holds, and reporting a conflict would push a client to retry.
    revoke_device(session, device_id=device_id, now=now)
    device = session.get(Device, device_id)
    assert device is not None
    return _projection(device, is_self=device_id == caller_device_id)


# --- PUT /v1/devices/{device_id}/push-token ---------------------------------


def update_push_token(
    session,
    keyring: KeyRing,
    *,
    caller_device_id: str,
    device_id: str,
    body: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Store or clear the calling device's sealed APNs token.

    Self only, and deliberately not covered by `device.manage`: a push token is
    proof of where a notification lands, and an administrator device writing one
    for another device would redirect that device's notifications.
    """
    if device_id != caller_device_id:
        raise AppError(
            ErrorCode.SCOPE_DENIED,
            internal_detail="a push token can only be set for the calling device",
        )
    _closed_body(body, frozenset({"push_token"}))

    raw = body.get("push_token")
    device = session.get(Device, device_id)
    if device is None or device.status != "active":
        # The caller authenticated as this device, so this is a race with
        # revocation rather than a lookup failure.
        raise AppError(
            ErrorCode.SCOPE_DENIED,
            internal_detail="device is not active",
        )

    if raw is None:
        device.encrypted_push_token = None
        return {"device_id": device_id, "has_push_token": False}

    token = _push_token(raw)
    device.encrypted_push_token = keyring.encrypt(
        token.encode("ascii"),
        table="devices",
        column="encrypted_push_token",
        row_id=device_id,
    )
    return {"device_id": device_id, "has_push_token": True}


def _push_token(raw: Any) -> str:
    if not isinstance(raw, str):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="push_token must be a hex string or null",
        )
    if not (MIN_PUSH_TOKEN_CHARS <= len(raw) <= MAX_PUSH_TOKEN_CHARS):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail=(
                f"push_token must be {MIN_PUSH_TOKEN_CHARS}-{MAX_PUSH_TOKEN_CHARS} "
                "hex characters"
            ),
        )
    if len(raw) % 2 or any(character not in _HEX_DIGITS for character in raw):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            internal_detail="push_token must be an even-length hex string",
        )
    return raw.lower()


def _require(scope: str, scopes: tuple[str, ...]) -> None:
    if scope not in scopes:
        raise AppError(
            ErrorCode.SCOPE_DENIED,
            internal_detail=f"this device does not hold {scope}",
        )
