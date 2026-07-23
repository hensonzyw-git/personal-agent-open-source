"""DEV-008: enrollment, challenge, token and revocation end to end."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent.auth.device_keys import (
    b64u_encode,
    build_signing_input,
    der_to_jose,
    encode_device_public_key,
)
from personal_agent.auth.enrollment import (
    DEFAULT_DEVICE_SCOPES,
    DEVICE_MANAGE_SCOPE,
    MAX_CHALLENGE_ATTEMPTS,
    ChallengeError,
    EnrollmentError,
    claim_enrollment_code,
    consume_challenge,
    create_challenge,
    create_enrollment_code,
    revoke_device,
)
from personal_agent.auth.tokens import (
    ALGORITHM,
    CLOCK_SKEW,
    MAX_TTL,
    REQUIRED_CLAIMS,
    SigningKey,
    TokenError,
    TokenKeyRing,
    UnknownSigningKeyError,
    issue_access_token,
    verify_access_token,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import AuthChallenge, Device


NOW = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
VECTORS = json.loads(
    (
        Path(__file__).parents[2]
        / "src/personal_agent/auth/vectors/device_auth_vectors.json"
    ).read_text(encoding="utf-8")
)


@pytest.fixture()
def session(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture()
def device_key() -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(
        int(VECTORS["private_key_scalar_hex"], 16), ec.SECP256R1()
    )


@pytest.fixture()
def ring() -> TokenKeyRing:
    private = ec.generate_private_key(ec.SECP256R1())
    return TokenKeyRing(
        active=SigningKey("api-2026-01", private, private.public_key())
    )


def enroll(session, device_key, *, manage: bool = False) -> Device:
    issued = create_enrollment_code(
        session, now=NOW, grants_device_manage=manage
    )
    return claim_enrollment_code(
        session,
        code=issued.code,
        public_key_b64u=encode_device_public_key(device_key.public_key()),
        display_name="iPhone",
        allowed_tools_version="v1",
        now=NOW,
    )


def sign(device_key, challenge, device_id: str) -> str:
    der = device_key.sign(
        build_signing_input(
            challenge_id=challenge.challenge_id,
            nonce_b64u=challenge.nonce,
            device_id=device_id,
        ),
        ec.ECDSA(hashes.SHA256()),
    )
    return b64u_encode(der_to_jose(der))


# --- enrollment -------------------------------------------------------------


def test_a_code_registers_exactly_one_device(session, device_key) -> None:
    issued = create_enrollment_code(session, now=NOW)
    device = claim_enrollment_code(
        session,
        code=issued.code,
        public_key_b64u=encode_device_public_key(device_key.public_key()),
        display_name="iPhone",
        allowed_tools_version="v1",
        now=NOW,
    )
    assert device.status == "active"
    assert device.device_key_thumbprint == VECTORS["device_key_thumbprint_b64u"]

    with pytest.raises(EnrollmentError):
        claim_enrollment_code(
            session,
            code=issued.code,
            public_key_b64u=encode_device_public_key(device_key.public_key()),
            display_name="second try",
            allowed_tools_version="v1",
            now=NOW,
        )


def test_the_plaintext_code_is_never_stored(session) -> None:
    issued = create_enrollment_code(session, now=NOW)
    session.commit()
    stored = session.execute(
        __import__("sqlalchemy").text("SELECT code_hash FROM enrollment_codes")
    ).scalars().all()
    assert issued.code not in stored


def test_an_expired_code_is_refused(session, device_key) -> None:
    issued = create_enrollment_code(session, now=NOW)
    with pytest.raises(EnrollmentError):
        claim_enrollment_code(
            session,
            code=issued.code,
            public_key_b64u=encode_device_public_key(device_key.public_key()),
            display_name="late",
            allowed_tools_version="v1",
            now=NOW + timedelta(minutes=11),
        )


def test_device_manage_is_granted_only_by_the_code(session, device_key) -> None:
    plain = enroll(session, device_key)
    assert DEVICE_MANAGE_SCOPE not in plain.scopes
    assert all(scope in plain.scopes for scope in DEFAULT_DEVICE_SCOPES)

    session.delete(plain)
    session.flush()
    managed = enroll(session, device_key, manage=True)
    assert DEVICE_MANAGE_SCOPE in managed.scopes


# --- challenge --------------------------------------------------------------


def test_a_signed_challenge_authenticates(session, device_key) -> None:
    device = enroll(session, device_key)
    challenge = create_challenge(session, device_id=device.device_id, now=NOW)
    authenticated = consume_challenge(
        session,
        challenge_id=challenge.challenge_id,
        device_id=device.device_id,
        nonce=challenge.nonce,
        signature_b64u=sign(device_key, challenge, device.device_id),
        now=NOW,
    )
    assert authenticated.device_id == device.device_id


def test_a_challenge_cannot_be_replayed(session, device_key) -> None:
    device = enroll(session, device_key)
    challenge = create_challenge(session, device_id=device.device_id, now=NOW)
    signature = sign(device_key, challenge, device.device_id)
    arguments = {
        "challenge_id": challenge.challenge_id,
        "device_id": device.device_id,
        "nonce": challenge.nonce,
        "signature_b64u": signature,
        "now": NOW,
    }
    consume_challenge(session, **arguments)
    with pytest.raises(ChallengeError):
        consume_challenge(session, **arguments)


def test_an_expired_challenge_is_refused(session, device_key) -> None:
    device = enroll(session, device_key)
    challenge = create_challenge(session, device_id=device.device_id, now=NOW)
    with pytest.raises(ChallengeError):
        consume_challenge(
            session,
            challenge_id=challenge.challenge_id,
            device_id=device.device_id,
            nonce=challenge.nonce,
            signature_b64u=sign(device_key, challenge, device.device_id),
            now=NOW + timedelta(minutes=3),
        )


def test_failed_attempts_exhaust_a_challenge(session, device_key) -> None:
    device = enroll(session, device_key)
    challenge = create_challenge(session, device_id=device.device_id, now=NOW)
    wrong = b64u_encode(b"\x01" * 64)

    for _ in range(MAX_CHALLENGE_ATTEMPTS):
        with pytest.raises(ChallengeError):
            consume_challenge(
                session,
                challenge_id=challenge.challenge_id,
                device_id=device.device_id,
                nonce=challenge.nonce,
                signature_b64u=wrong,
                now=NOW,
            )

    # Even the correct signature no longer works: a stolen challenge id cannot
    # be used for unlimited guesses.
    with pytest.raises(ChallengeError):
        consume_challenge(
            session,
            challenge_id=challenge.challenge_id,
            device_id=device.device_id,
            nonce=challenge.nonce,
            signature_b64u=sign(device_key, challenge, device.device_id),
            now=NOW,
        )


def test_the_nonce_is_stored_only_as_a_hash(session, device_key) -> None:
    device = enroll(session, device_key)
    challenge = create_challenge(session, device_id=device.device_id, now=NOW)
    stored = session.get(AuthChallenge, challenge.challenge_id)
    assert stored is not None
    assert challenge.nonce not in stored.nonce_hash


def test_another_devices_signature_is_refused(session, device_key) -> None:
    device = enroll(session, device_key)
    challenge = create_challenge(session, device_id=device.device_id, now=NOW)
    impostor = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ChallengeError):
        consume_challenge(
            session,
            challenge_id=challenge.challenge_id,
            device_id=device.device_id,
            nonce=challenge.nonce,
            signature_b64u=sign(impostor, challenge, device.device_id),
            now=NOW,
        )


# --- tokens -----------------------------------------------------------------


def make_token(ring: TokenKeyRing, **overrides) -> str:
    arguments = {
        "device_id": "018f0000-0000-4000-8000-000000000002",
        "device_key_thumbprint": VECTORS["device_key_thumbprint_b64u"],
        "scopes": ["finance.expense.write"],
        "allowed_tools_version": "v1",
        "now": NOW,
    }
    arguments.update(overrides)
    return issue_access_token(ring, **arguments)


def test_a_fresh_token_verifies_with_every_required_claim(ring) -> None:
    claims = verify_access_token(ring, make_token(ring), now=NOW)
    for claim in REQUIRED_CLAIMS:
        assert claim in claims
    assert claims["sub"] == f"device:{claims['device_id']}"
    assert claims["exp"] - claims["iat"] == MAX_TTL.total_seconds()


def test_an_expired_token_is_refused(ring) -> None:
    token = make_token(ring)
    with pytest.raises(TokenError):
        verify_access_token(ring, token, now=NOW + timedelta(minutes=11))


def test_a_token_is_still_valid_inside_the_skew(ring) -> None:
    token = make_token(ring)
    verify_access_token(ring, token, now=NOW + MAX_TTL + CLOCK_SKEW / 2)


def test_a_ttl_beyond_the_maximum_cannot_be_issued(ring) -> None:
    with pytest.raises(TokenError):
        make_token(ring, ttl=timedelta(hours=1))


def test_alg_none_is_refused(ring) -> None:
    forged = jwt.encode({"device_id": "x"}, key="", algorithm="none")
    with pytest.raises(TokenError):
        verify_access_token(ring, forged, now=NOW)


def test_an_algorithm_substitution_is_refused(ring) -> None:
    # Signing HS256 with the public key as the shared secret is the classic
    # confusion attack; the algorithm comes from the allowlist, not the header.
    forged = jwt.encode(
        {"iss": "personal-agent-api", "aud": "personal-agent-api"},
        key="secret",
        algorithm="HS256",
        headers={"kid": ring.active_kid},
    )
    with pytest.raises(TokenError):
        verify_access_token(ring, forged, now=NOW)


def test_an_unknown_kid_never_reaches_verification(ring) -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    stranger = TokenKeyRing(active=SigningKey("api-2099-99", other, other.public_key()))
    with pytest.raises(UnknownSigningKeyError):
        verify_access_token(ring, make_token(stranger), now=NOW)


def test_a_token_from_another_key_is_refused(ring) -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    forged = TokenKeyRing(
        active=SigningKey(ring.active_kid, other, other.public_key())
    )
    with pytest.raises(TokenError):
        verify_access_token(ring, make_token(forged), now=NOW)


@pytest.mark.parametrize("claim", ["exp", "aud", "iss", "device_id", "jti"])
def test_a_missing_required_claim_is_refused(ring, claim: str) -> None:
    private = ring._active.private_key
    claims = verify_access_token(ring, make_token(ring), now=NOW)
    claims.pop(claim)
    stripped = jwt.encode(
        claims, private, algorithm=ALGORITHM, headers={"kid": ring.active_kid}
    )
    with pytest.raises(TokenError):
        verify_access_token(ring, stripped, now=NOW)


def test_a_wrong_audience_is_refused(ring) -> None:
    private = ring._active.private_key
    claims = verify_access_token(ring, make_token(ring), now=NOW)
    claims["aud"] = "personal-data-mcp"
    forged = jwt.encode(
        claims, private, algorithm=ALGORITHM, headers={"kid": ring.active_kid}
    )
    with pytest.raises(TokenError):
        verify_access_token(ring, forged, now=NOW)


def test_rotation_keeps_tokens_in_flight_valid(ring) -> None:
    before = make_token(ring)
    new_private = ec.generate_private_key(ec.SECP256R1())
    rotated = ring.rotated(
        SigningKey("api-2026-02", new_private, new_private.public_key())
    )

    # The old private key stops signing immediately; the old public key stays
    # for verification until every outstanding token has expired.
    assert rotated.active_kid == "api-2026-02"
    verify_access_token(rotated, before, now=NOW + timedelta(minutes=5))
    verify_access_token(rotated, make_token(rotated), now=NOW)


# --- revocation -------------------------------------------------------------


def test_revocation_takes_effect_without_waiting_for_expiry(
    session, device_key, ring
) -> None:
    device = enroll(session, device_key)
    token = make_token(ring, device_id=device.device_id)

    # The token itself stays cryptographically valid, which is exactly why
    # every request must read the device row rather than trust the token alone.
    assert revoke_device(session, device_id=device.device_id, now=NOW)
    claims = verify_access_token(ring, token, now=NOW + timedelta(minutes=1))
    assert claims["device_id"] == device.device_id

    current = session.get(Device, device.device_id)
    assert current is not None and current.status == "revoked"
    assert current.revoked_at == NOW

    with pytest.raises(ChallengeError):
        create_challenge(session, device_id=device.device_id, now=NOW)
    assert not revoke_device(session, device_id=device.device_id, now=NOW)


def test_a_revoked_device_is_current_in_the_same_session(
    session, device_key
) -> None:
    # A bulk UPDATE does not reliably refresh an object already loaded in the
    # session. Revocation is a security decision read on every request, so the
    # in-session view must not lag behind the row.
    device = enroll(session, device_key)
    assert device.status == "active" and device.revoked_at is None

    revoke_device(session, device_id=device.device_id, now=NOW)

    assert device.status == "revoked"
    assert device.revoked_at == NOW
