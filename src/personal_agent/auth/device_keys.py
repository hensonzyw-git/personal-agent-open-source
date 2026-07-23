"""Device key and challenge wire contract, per technical design 4.2.1.

Every byte here is fixed because two independent implementations have to agree:
Swift on the device using Security.framework, and Python on the server using
PyCA cryptography. The two libraries disagree by default about ECDSA signature
encoding, so the contract names one and rejects the other.

- a device public key is a P-256 ANSI X9.63 uncompressed point,
  `0x04 || X(32) || Y(32)`, carried as unpadded base64url;
- the signing input is five LF-separated lines with no trailing newline;
- a signature on the wire is JOSE ES256: 64 bytes of `R || S`, each 32 bytes
  unsigned big-endian and left zero padded. Security.framework returns X9.62
  DER, so the app converts before sending and the server refuses DER outright.

Refusing DER matters: a DER blob and a JOSE blob for the same signature differ
in length, and quietly accepting both would leave the two implementations free
to drift until a real device fails to authenticate.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils


AUTH_CONTEXT: Final[str] = "personal-agent-auth-v1"
AUDIENCE: Final[str] = "personal-agent-api"

UNCOMPRESSED_POINT_PREFIX: Final[int] = 0x04
COORDINATE_BYTES: Final[int] = 32
PUBLIC_KEY_BYTES: Final[int] = 1 + 2 * COORDINATE_BYTES
JOSE_SIGNATURE_BYTES: Final[int] = 2 * COORDINATE_BYTES
NONCE_BYTES: Final[int] = 32

#: Order of the P-256 base point. R and S must be in [1, n-1].
CURVE_ORDER: Final[int] = (
    0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
)


class DeviceKeyError(ValueError):
    """A device public key is not a valid P-256 uncompressed point."""


class SignatureFormatError(ValueError):
    """A signature is not 64-byte JOSE ES256 with in-range scalars."""


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("expected an unpadded base64url string")
    if value.endswith("="):
        raise ValueError("base64url values on the wire carry no padding")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def load_device_public_key(encoded: str) -> ec.EllipticCurvePublicKey:
    """Parse the enrolled public key from its wire form."""
    try:
        raw = b64u_decode(encoded)
    except (ValueError, TypeError) as exc:
        raise DeviceKeyError("public key is not unpadded base64url") from exc
    if len(raw) != PUBLIC_KEY_BYTES:
        raise DeviceKeyError(
            f"public key must be {PUBLIC_KEY_BYTES} bytes, got {len(raw)}"
        )
    if raw[0] != UNCOMPRESSED_POINT_PREFIX:
        raise DeviceKeyError(
            "public key must be an uncompressed point starting with 0x04"
        )
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as exc:
        raise DeviceKeyError("public key is not a point on P-256") from exc


def encode_device_public_key(key: ec.EllipticCurvePublicKey) -> str:
    """Render a public key in the enrolled wire form."""
    numbers = key.public_numbers()
    raw = (
        bytes([UNCOMPRESSED_POINT_PREFIX])
        + numbers.x.to_bytes(COORDINATE_BYTES, "big")
        + numbers.y.to_bytes(COORDINATE_BYTES, "big")
    )
    return b64u_encode(raw)


def device_key_thumbprint(encoded_public_key: str) -> str:
    """SHA-256 over the 65 raw bytes, as unpadded base64url.

    This names the registered key that issued a token. It is not a
    proof-of-possession over each request, and the design says so; the exposure
    window is bounded by the short TTL instead.
    """
    raw = b64u_decode(encoded_public_key)
    if len(raw) != PUBLIC_KEY_BYTES:
        raise DeviceKeyError(
            f"public key must be {PUBLIC_KEY_BYTES} bytes, got {len(raw)}"
        )
    return b64u_encode(hashlib.sha256(raw).digest())


def build_signing_input(
    *, challenge_id: str, nonce_b64u: str, device_id: str
) -> bytes:
    """The exact bytes both sides sign.

    Five lines, single LF separators, no trailing newline, no normalisation.
    """
    for name, value in (
        ("challenge_id", challenge_id),
        ("nonce", nonce_b64u),
        ("device_id", device_id),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        if "\n" in value:
            raise ValueError(f"{name} must not contain a newline")
    return "\n".join(
        [AUTH_CONTEXT, challenge_id, nonce_b64u, device_id, AUDIENCE]
    ).encode("utf-8")


def jose_to_der(signature: bytes) -> bytes:
    """Convert a 64-byte `R || S` signature to the DER form the library wants."""
    if len(signature) != JOSE_SIGNATURE_BYTES:
        raise SignatureFormatError(
            f"signature must be {JOSE_SIGNATURE_BYTES} bytes of R||S, "
            f"got {len(signature)}; DER encoded signatures are rejected"
        )
    r = int.from_bytes(signature[:COORDINATE_BYTES], "big")
    s = int.from_bytes(signature[COORDINATE_BYTES:], "big")
    for name, value in (("r", r), ("s", s)):
        if not 1 <= value < CURVE_ORDER:
            raise SignatureFormatError(f"signature scalar {name} is out of range")
    return utils.encode_dss_signature(r, s)


def der_to_jose(der_signature: bytes) -> bytes:
    """Convert Security.framework's DER output to the 64-byte wire form.

    Kept server side so the fixed test vectors can prove both directions; the
    conversion itself happens in the app before the request is sent.
    """
    r, s = utils.decode_dss_signature(der_signature)
    return r.to_bytes(COORDINATE_BYTES, "big") + s.to_bytes(
        COORDINATE_BYTES, "big"
    )


def verify_challenge_signature(
    *,
    encoded_public_key: str,
    signature_b64u: str,
    challenge_id: str,
    nonce_b64u: str,
    device_id: str,
) -> bool:
    """Whether this device signed this challenge. Never raises on bad input."""
    try:
        public_key = load_device_public_key(encoded_public_key)
        signature = b64u_decode(signature_b64u)
        der = jose_to_der(signature)
        message = build_signing_input(
            challenge_id=challenge_id,
            nonce_b64u=nonce_b64u,
            device_id=device_id,
        )
    except (DeviceKeyError, SignatureFormatError, ValueError, TypeError):
        return False

    try:
        public_key.verify(der, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True
