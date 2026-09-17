"""Device enrollment, challenge issuance and challenge consumption.

Per technical design 4.1 and 4.2. Enrollment codes and challenge nonces are
stored as hashes: the database holds enough to verify a presented value and not
enough to replay one if the file leaks.

Consumption is a single conditional UPDATE. Two concurrent token requests for
one challenge must not both succeed, and checking-then-updating in Python would
let them, so the "unused and unexpired" test lives in the WHERE clause.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import update
from sqlalchemy.orm import Session

from personal_agent.auth.device_keys import (
    NONCE_BYTES,
    b64u_encode,
    device_key_thumbprint,
    load_device_public_key,
    verify_challenge_signature,
)
from personal_agent.storage.models import AuthChallenge, Device, EnrollmentCode
from personal_agent_core.ids import new_id


ENROLLMENT_CODE_BYTES: Final[int] = 16
ENROLLMENT_TTL: Final[timedelta] = timedelta(minutes=10)
CHALLENGE_TTL: Final[timedelta] = timedelta(minutes=2)
MAX_CHALLENGE_ATTEMPTS: Final[int] = 5

#: Scopes a device gets by default. Device management is granted only by the
#: administrator CLI, never inferred from a client request.
DEFAULT_DEVICE_SCOPES: Final[tuple[str, ...]] = (
    "device.self.read",
    "device.self.revoke",
    "finance.expense.read",
    "finance.expense.write",
    "finance.income.write",
    "finance.family_fund.write",
    "meta.capabilities.read",
    "dal.read",
    "dal.request",
    "dal.prd.decide",
    "dal.delivery.decide",
)
DEVICE_MANAGE_SCOPE: Final[str] = "device.manage"


class EnrollmentError(Exception):
    """Enrollment could not be completed."""


class ChallengeError(Exception):
    """A challenge could not be issued or consumed."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encode_device_scopes(scopes: Iterable[str]) -> str:
    """Serialise the `devices.scopes` column.

    The column is JSON. It used to be written with `repr()` here while every
    reader used `json.loads`, which no test could see because nothing enrolled a
    device outside this module and a `repr` list still answers `in` correctly for
    a substring check. The first real enrollment would have produced a device
    whose scopes could not be read at all -- a device granted nothing, failing at
    composition rather than at the write. One encoder and one decoder now.
    """
    encoded = list(scopes)
    if not all(isinstance(scope, str) for scope in encoded):
        raise ValueError("device scopes must be strings")
    return json.dumps(encoded)


def decode_device_scopes(raw: str) -> tuple[str, ...]:
    """Read the `devices.scopes` column, or raise `ValueError`."""
    try:
        scopes = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("device scopes column is not JSON") from exc
    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) for scope in scopes
    ):
        raise ValueError("device scopes column is not a list of strings")
    return tuple(scopes)


def _expire(session: Session, model: type, primary_key: str) -> None:
    """Drop a row from the session cache after a bulk UPDATE.

    SQLAlchemy's `synchronize_session` was observed to refresh some columns of an
    already-loaded object and leave others stale, which is not a basis for a
    security decision. Expiring forces the next read to come from the database,
    which is what "read the current device status on every request" requires.
    """
    cached = session.identity_map.get(session.identity_key(model, primary_key))
    if cached is not None:
        session.expire(cached)


@dataclass(frozen=True)
class IssuedEnrollmentCode:
    """The plaintext code is returned once and never stored."""

    code: str
    expires_at: datetime


def create_enrollment_code(
    session: Session,
    *,
    now: datetime,
    grants_device_manage: bool = False,
) -> IssuedEnrollmentCode:
    """Mint a single-use enrollment code. Administrator CLI only."""
    code = b64u_encode(os.urandom(ENROLLMENT_CODE_BYTES))
    expires_at = now + ENROLLMENT_TTL
    session.add(
        EnrollmentCode(
            code_hash=_hash(code),
            grants_device_manage=grants_device_manage,
            created_at=now,
            expires_at=expires_at,
        )
    )
    session.flush()
    return IssuedEnrollmentCode(code=code, expires_at=expires_at)


def claim_enrollment_code(
    session: Session,
    *,
    code: str,
    public_key_b64u: str,
    display_name: str,
    allowed_tools_version: str,
    now: datetime,
) -> Device:
    """Register a device against a one-time code.

    The client supplies a public key and a display name. It does not supply
    scopes: those come from the server, and a client-declared scope list is
    ignored rather than merged.
    """
    load_device_public_key(public_key_b64u)  # rejects malformed keys early

    consumed = session.execute(
        update(EnrollmentCode)
        .where(
            EnrollmentCode.code_hash == _hash(code),
            EnrollmentCode.used_at.is_(None),
            EnrollmentCode.expires_at > now,
        )
        .values(used_at=now)
    )
    if consumed.rowcount != 1:
        raise EnrollmentError("enrollment code is unknown, used or expired")

    record = session.get(EnrollmentCode, _hash(code))
    assert record is not None
    scopes = list(DEFAULT_DEVICE_SCOPES)
    if record.grants_device_manage:
        scopes.append(DEVICE_MANAGE_SCOPE)

    device = Device(
        device_id=new_id(),
        display_name=display_name,
        public_key=public_key_b64u,
        device_key_thumbprint=device_key_thumbprint(public_key_b64u),
        status="active",
        scopes=encode_device_scopes(scopes),
        allowed_tools_version=allowed_tools_version,
        created_at=now,
    )
    session.add(device)
    session.flush()
    return device


@dataclass(frozen=True)
class IssuedChallenge:
    challenge_id: str
    device_id: str
    nonce: str
    expires_at: datetime


def create_challenge(
    session: Session, *, device_id: str, now: datetime
) -> IssuedChallenge:
    """Issue a single-use challenge for an active device."""
    device = session.get(Device, device_id)
    if device is None or device.status != "active":
        raise ChallengeError("device is unknown or revoked")

    nonce = b64u_encode(secrets.token_bytes(NONCE_BYTES))
    challenge = AuthChallenge(
        challenge_id=new_id(),
        device_id=device_id,
        nonce_hash=_hash(nonce),
        failed_attempts=0,
        created_at=now,
        expires_at=now + CHALLENGE_TTL,
    )
    session.add(challenge)
    session.flush()
    return IssuedChallenge(
        challenge_id=challenge.challenge_id,
        device_id=device_id,
        nonce=nonce,
        expires_at=challenge.expires_at,
    )


def consume_challenge(
    session: Session,
    *,
    challenge_id: str,
    device_id: str,
    nonce: str,
    signature_b64u: str,
    now: datetime,
) -> Device:
    """Verify a signature and consume the challenge, or raise.

    A failed attempt is counted. After `MAX_CHALLENGE_ATTEMPTS` the challenge is
    dead regardless of what is presented next, so a stolen challenge id cannot be
    used for unlimited signature guesses.
    """
    challenge = session.get(AuthChallenge, challenge_id)
    if (
        challenge is None
        or challenge.device_id != device_id
        or challenge.consumed_at is not None
        or challenge.expires_at <= now
        or challenge.failed_attempts >= MAX_CHALLENGE_ATTEMPTS
    ):
        raise ChallengeError("challenge is unknown, used, expired or exhausted")

    device = session.get(Device, device_id)
    if device is None or device.status != "active":
        raise ChallengeError("device is unknown or revoked")

    # Constant-time compare: the stored nonce hash is the only copy the server
    # keeps, and a timing signal on it would leak the nonce.
    if not hmac.compare_digest(challenge.nonce_hash, _hash(nonce)):
        challenge.failed_attempts += 1
        session.flush()
        raise ChallengeError("nonce does not match the issued challenge")

    if not verify_challenge_signature(
        encoded_public_key=device.public_key,
        signature_b64u=signature_b64u,
        challenge_id=challenge_id,
        nonce_b64u=nonce,
        device_id=device_id,
    ):
        challenge.failed_attempts += 1
        session.flush()
        raise ChallengeError("signature does not verify for this device")

    # One conditional UPDATE, so two concurrent submissions cannot both win.
    consumed = session.execute(
        update(AuthChallenge)
        .where(
            AuthChallenge.challenge_id == challenge_id,
            AuthChallenge.consumed_at.is_(None),
            AuthChallenge.expires_at > now,
        )
        .values(consumed_at=now)
    )
    if consumed.rowcount != 1:
        raise ChallengeError("challenge was consumed concurrently")
    _expire(session, AuthChallenge, challenge_id)
    return device


def revoke_device(session: Session, *, device_id: str, now: datetime) -> bool:
    """Revoke a device immediately.

    Outstanding access tokens are not tracked down; they stop working because
    every request reads the current device status.
    """
    result = session.execute(
        update(Device)
        .where(Device.device_id == device_id, Device.status == "active")
        .values(status="revoked", revoked_at=now)
    )
    if result.rowcount != 1:
        return False
    _expire(session, Device, device_id)
    return True
