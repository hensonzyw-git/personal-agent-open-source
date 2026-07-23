"""DEV-007: AEAD envelopes fail closed and survive an interrupted rotation."""

from __future__ import annotations

import base64

import pytest

from personal_agent_core.crypto import (
    AAD_PREFIX,
    NONCE_BYTES,
    TAG_BYTES,
    CryptoError,
    DecryptionError,
    KeyEntry,
    KeyRing,
    KeyRingError,
    UnknownKeyError,
    build_aad,
    generate_key,
)
from personal_agent_core.sqlite import EncryptedEnvelope


PLAINTEXT = "午饭 45.00 个人支出".encode("utf-8")
WHERE = {"table": "tool_executions", "column": "encrypted_payload", "row_id": "key-1"}


@pytest.fixture()
def ring() -> KeyRing:
    return KeyRing(
        [generate_key("finance-data-2026-01")], service="personal-data-mcp"
    )


def test_round_trip(ring: KeyRing) -> None:
    envelope = ring.encrypt(PLAINTEXT, **WHERE)
    assert ring.decrypt(envelope, **WHERE) == PLAINTEXT


def test_the_envelope_matches_the_storage_contract(ring: KeyRing) -> None:
    # The column type and the cipher have to agree, or a value that encrypts
    # cannot be stored.
    envelope = ring.encrypt(PLAINTEXT, **WHERE)
    assert EncryptedEnvelope._validate(envelope) == envelope
    assert len(base64.urlsafe_b64decode(envelope["nonce"] + "==")) == NONCE_BYTES
    assert len(base64.urlsafe_b64decode(envelope["tag"] + "==")) == TAG_BYTES


def test_plaintext_never_appears_in_the_envelope(ring: KeyRing) -> None:
    envelope = ring.encrypt(PLAINTEXT, **WHERE)
    blob = "".join(str(value) for value in envelope.values())
    assert "午饭" not in blob
    assert "45.00" not in blob


def test_a_fresh_nonce_per_encryption(ring: KeyRing) -> None:
    # Nonce reuse under one GCM key is catastrophic, so identical plaintext in
    # the same place must still produce different envelopes.
    nonces = {ring.encrypt(PLAINTEXT, **WHERE)["nonce"] for _ in range(32)}
    assert len(nonces) == 32


# --- fail closed ------------------------------------------------------------


def test_a_tampered_tag_fails(ring: KeyRing) -> None:
    envelope = ring.encrypt(PLAINTEXT, **WHERE)
    flipped = bytearray(base64.urlsafe_b64decode(envelope["tag"] + "=="))
    flipped[0] ^= 0x01
    envelope["tag"] = base64.urlsafe_b64encode(bytes(flipped)).decode().rstrip("=")
    with pytest.raises(DecryptionError):
        ring.decrypt(envelope, **WHERE)


def test_a_tampered_ciphertext_fails(ring: KeyRing) -> None:
    envelope = ring.encrypt(PLAINTEXT, **WHERE)
    flipped = bytearray(base64.urlsafe_b64decode(envelope["ciphertext"] + "=="))
    flipped[0] ^= 0x01
    envelope["ciphertext"] = (
        base64.urlsafe_b64encode(bytes(flipped)).decode().rstrip("=")
    )
    with pytest.raises(DecryptionError):
        ring.decrypt(envelope, **WHERE)


@pytest.mark.parametrize(
    "moved",
    [
        {"table": "conversation_events", "column": "encrypted_payload", "row_id": "key-1"},
        {"table": "tool_executions", "column": "encrypted_result", "row_id": "key-1"},
        {"table": "tool_executions", "column": "encrypted_payload", "row_id": "key-2"},
    ],
)
def test_a_ciphertext_moved_to_another_location_fails(
    ring: KeyRing, moved: dict[str, str]
) -> None:
    # This is what stops a restored database from being quietly rearranged: a
    # ciphertext only opens where it was written.
    envelope = ring.encrypt(PLAINTEXT, **WHERE)
    with pytest.raises(DecryptionError):
        ring.decrypt(envelope, **moved)


def test_a_ciphertext_from_another_service_fails() -> None:
    key = generate_key("shared-kid")
    finance = KeyRing([key], service="personal-data-mcp")
    agent = KeyRing([key], service="personal-agent-api")
    envelope = finance.encrypt(PLAINTEXT, **WHERE)
    with pytest.raises(DecryptionError):
        agent.decrypt(envelope, **WHERE)


def test_an_unknown_key_id_is_refused(ring: KeyRing) -> None:
    envelope = ring.encrypt(PLAINTEXT, **WHERE)
    envelope["kid"] = "finance-data-2027-01"
    with pytest.raises(UnknownKeyError):
        ring.decrypt(envelope, **WHERE)


def test_a_wrong_key_under_a_known_id_fails() -> None:
    sealed = KeyRing([generate_key("kid-1")], service="s").encrypt(
        PLAINTEXT, **WHERE
    )
    other = KeyRing([generate_key("kid-1")], service="s")
    with pytest.raises(DecryptionError):
        other.decrypt(sealed, **WHERE)


def test_an_unsupported_envelope_version_is_refused(ring: KeyRing) -> None:
    envelope = ring.encrypt(PLAINTEXT, **WHERE)
    envelope["v"] = 2
    with pytest.raises(DecryptionError):
        ring.decrypt(envelope, **WHERE)


# --- AAD shape --------------------------------------------------------------


def test_the_aad_shape_is_exactly_as_specified() -> None:
    aad = build_aad(
        service="personal-data-mcp",
        table="tool_executions",
        column="encrypted_payload",
        row_id="key-1",
    )
    assert aad == (
        f"{AAD_PREFIX}\n"
        "personal-data-mcp\ntool_executions\nencrypted_payload\nkey-1"
    ).encode("utf-8")
    assert not aad.endswith(b"\n")
    assert aad.count(b"\n") == 4


def test_aad_components_cannot_be_empty_or_contain_newlines() -> None:
    # A newline inside a component would make the five-line binding ambiguous:
    # two different locations could serialise to the same AAD.
    for override in ({"table": ""}, {"row_id": "key\n1"}, {"column": "x\ny"}):
        components = {"service": "s", "table": "t", "column": "c", "row_id": "r"}
        components.update(override)
        with pytest.raises(CryptoError):
            build_aad(**components)


# --- key ring and rotation --------------------------------------------------


def test_exactly_one_active_key(ring: KeyRing) -> None:
    with pytest.raises(KeyRingError):
        KeyRing([], service="s")
    with pytest.raises(KeyRingError):
        KeyRing(
            [generate_key("a"), generate_key("b")], service="s"
        )  # two actives
    with pytest.raises(KeyRingError):
        KeyRing([generate_key("a", state="decrypt_only")], service="s")


def test_keys_must_be_256_bit() -> None:
    with pytest.raises(KeyRingError):
        KeyEntry(kid="short", key=b"\x00" * 16, state="active")


def test_rotation_keeps_old_data_readable(ring: KeyRing) -> None:
    before = ring.encrypt(PLAINTEXT, **WHERE)
    rotated = ring.rotated(generate_key("finance-data-2026-02"))

    # Interrupted rotation is the normal case: some rows are re-encrypted, some
    # are not, and both must open.
    assert rotated.decrypt(before, **WHERE) == PLAINTEXT
    after = rotated.encrypt(PLAINTEXT, **WHERE)
    assert after["kid"] == "finance-data-2026-02"
    assert rotated.decrypt(after, **WHERE) == PLAINTEXT
    assert set(rotated.kids) == {"finance-data-2026-01", "finance-data-2026-02"}


def test_rotation_never_leaves_two_active_keys(ring: KeyRing) -> None:
    rotated = ring.rotated(generate_key("finance-data-2026-02"))
    assert rotated.active_kid == "finance-data-2026-02"
    assert rotated.get("finance-data-2026-01").state == "decrypt_only"


def test_rotation_refuses_a_reused_key_id(ring: KeyRing) -> None:
    with pytest.raises(KeyRingError):
        ring.rotated(generate_key("finance-data-2026-01"))


def test_the_two_services_hold_independent_rings() -> None:
    agent = KeyRing([generate_key("agent-data-2026-01")], service="personal-agent-api")
    finance = KeyRing(
        [generate_key("finance-data-2026-01")], service="personal-data-mcp"
    )
    assert agent.get("agent-data-2026-01").key != finance.get(
        "finance-data-2026-01"
    ).key
    with pytest.raises(UnknownKeyError):
        agent.get("finance-data-2026-01")
