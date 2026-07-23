"""DEV-008: the device auth wire contract, pinned to fixed vectors.

The vector file is the artifact the Swift client must reproduce. If any assertion
here changes, a shipped device stops authenticating, so these are contract tests
rather than implementation tests.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent.auth.device_keys import (
    AUDIENCE,
    AUTH_CONTEXT,
    CURVE_ORDER,
    JOSE_SIGNATURE_BYTES,
    PUBLIC_KEY_BYTES,
    DeviceKeyError,
    SignatureFormatError,
    b64u_decode,
    b64u_encode,
    build_signing_input,
    der_to_jose,
    device_key_thumbprint,
    encode_device_public_key,
    jose_to_der,
    load_device_public_key,
    verify_challenge_signature,
)


VECTORS_PATH = (
    Path(__file__).parents[2]
    / "src/personal_agent/auth/vectors/device_auth_vectors.json"
)
V = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def private_key() -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(
        int(V["private_key_scalar_hex"], 16), ec.SECP256R1()
    )


def sign_now() -> str:
    """A fresh signature over the vector's challenge."""
    der = private_key().sign(
        build_signing_input(
            challenge_id=V["challenge_id"],
            nonce_b64u=V["nonce_b64u"],
            device_id=V["device_id"],
        ),
        ec.ECDSA(hashes.SHA256()),
    )
    return b64u_encode(der_to_jose(der))


# --- the vectors ------------------------------------------------------------


def test_the_public_key_encoding_matches_the_vector() -> None:
    assert encode_device_public_key(private_key().public_key()) == (
        V["public_key_x963_b64u"]
    )
    raw = b64u_decode(V["public_key_x963_b64u"])
    assert len(raw) == PUBLIC_KEY_BYTES == 65
    assert raw[0] == 0x04


def test_the_thumbprint_matches_the_vector() -> None:
    assert device_key_thumbprint(V["public_key_x963_b64u"]) == (
        V["device_key_thumbprint_b64u"]
    )


def test_the_signing_input_matches_the_vector_byte_for_byte() -> None:
    built = build_signing_input(
        challenge_id=V["challenge_id"],
        nonce_b64u=V["nonce_b64u"],
        device_id=V["device_id"],
    )
    assert built == V["signing_input_utf8"].encode("utf-8")

    lines = built.decode("utf-8").split("\n")
    assert len(lines) == 5
    assert lines[0] == AUTH_CONTEXT
    assert lines[4] == AUDIENCE
    assert not built.endswith(b"\n")
    assert b"\r" not in built


def test_the_frozen_signature_still_verifies() -> None:
    assert verify_challenge_signature(
        encoded_public_key=V["public_key_x963_b64u"],
        signature_b64u=V["signature_jose_b64u"],
        challenge_id=V["challenge_id"],
        nonce_b64u=V["nonce_b64u"],
        device_id=V["device_id"],
    )


def test_der_and_jose_forms_agree() -> None:
    der = b64u_decode(V["signature_der_b64u"])
    jose = b64u_decode(V["signature_jose_b64u"])
    assert der_to_jose(der) == jose
    assert jose_to_der(jose) == der
    assert len(jose) == JOSE_SIGNATURE_BYTES == 64


def test_vectors_carry_no_padding() -> None:
    # Padding is where two base64url implementations usually diverge.
    for key in (
        "public_key_x963_b64u",
        "device_key_thumbprint_b64u",
        "nonce_b64u",
        "signature_jose_b64u",
    ):
        assert not V[key].endswith("=")


# --- rejections -------------------------------------------------------------


def test_a_der_signature_on_the_wire_is_rejected() -> None:
    # Security.framework returns DER. Accepting both forms would let the two
    # implementations drift until a real device fails.
    assert not verify_challenge_signature(
        encoded_public_key=V["public_key_x963_b64u"],
        signature_b64u=V["signature_der_b64u"],
        challenge_id=V["challenge_id"],
        nonce_b64u=V["nonce_b64u"],
        device_id=V["device_id"],
    )
    with pytest.raises(SignatureFormatError):
        jose_to_der(b64u_decode(V["signature_der_b64u"]))


@pytest.mark.parametrize(
    "scalars",
    [
        (0, 1),
        (1, 0),
        (CURVE_ORDER, 1),
        (1, CURVE_ORDER),
        (CURVE_ORDER + 1, 1),
    ],
)
def test_out_of_range_scalars_are_rejected(scalars: tuple[int, int]) -> None:
    r, s = scalars
    raw = r.to_bytes(32, "big", signed=False) + s.to_bytes(32, "big")
    with pytest.raises(SignatureFormatError):
        jose_to_der(raw)


@pytest.mark.parametrize("length", [0, 32, 63, 65, 70, 128])
def test_wrong_length_signatures_are_rejected(length: int) -> None:
    with pytest.raises(SignatureFormatError):
        jose_to_der(b"\x01" * length)


def test_a_signature_for_a_different_challenge_does_not_verify() -> None:
    signature = sign_now()
    for override in (
        {"challenge_id": "018f0000-0000-7000-8000-00000000dead"},
        {"device_id": "018f0000-0000-7000-8000-00000000beef"},
        {"nonce_b64u": b64u_encode(bytes(range(1, 33)))},
    ):
        arguments = {
            "encoded_public_key": V["public_key_x963_b64u"],
            "signature_b64u": signature,
            "challenge_id": V["challenge_id"],
            "nonce_b64u": V["nonce_b64u"],
            "device_id": V["device_id"],
        }
        arguments.update(override)
        assert not verify_challenge_signature(**arguments)


def test_another_devices_key_does_not_verify() -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    assert not verify_challenge_signature(
        encoded_public_key=encode_device_public_key(other.public_key()),
        signature_b64u=sign_now(),
        challenge_id=V["challenge_id"],
        nonce_b64u=V["nonce_b64u"],
        device_id=V["device_id"],
    )


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-base64url!",
        b64u_encode(b"\x04" + b"\x00" * 63),
        b64u_encode(b"\x02" + b"\x00" * 64),
        b64u_encode(b"\x04" + b"\xff" * 64),
    ],
)
def test_malformed_public_keys_are_rejected(encoded: str) -> None:
    with pytest.raises(DeviceKeyError):
        load_device_public_key(encoded)


def test_padded_base64url_is_rejected() -> None:
    padded = base64.urlsafe_b64encode(b"\x00" * 65).decode()
    assert padded.endswith("=")
    with pytest.raises(ValueError):
        b64u_decode(padded)


def test_verification_never_raises_on_hostile_input() -> None:
    for signature in ("", "!!!", "AAAA", V["public_key_x963_b64u"]):
        assert not verify_challenge_signature(
            encoded_public_key=V["public_key_x963_b64u"],
            signature_b64u=signature,
            challenge_id=V["challenge_id"],
            nonce_b64u=V["nonce_b64u"],
            device_id=V["device_id"],
        )


def test_signing_input_components_cannot_smuggle_a_newline() -> None:
    # Otherwise two different challenges could serialise to the same bytes.
    with pytest.raises(ValueError):
        build_signing_input(
            challenge_id="a\nb", nonce_b64u=V["nonce_b64u"], device_id="d"
        )
