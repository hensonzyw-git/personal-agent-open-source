"""Application-layer field encryption, per technical design 8.5.

Disk encryption and restic repository encryption protect a stolen disk. Neither
protects a field from a process that can already read the database file, which
is why sensitive columns carry their own AES-256-GCM envelope.

Two decisions here are what make a restore drill meaningful:

- the AAD binds each ciphertext to `service / table / column / row id`. A
  ciphertext lifted from one row and pasted into another fails to open, so a
  restored database cannot be quietly rearranged;
- a key ring has exactly one `active` key and any number of `decrypt_only` keys.
  Rotation is therefore interruptible: everything written under an old key stays
  readable while re-encryption proceeds, and there is never a moment with two
  keys claiming to be current.

Failures are always closed. A tampered tag, a wrong AAD or an unknown key id
raise; none of them return an empty value, because a silently empty amount is
worse than an error.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Final, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENVELOPE_VERSION: Final[int] = 1
KEY_BYTES: Final[int] = 32
NONCE_BYTES: Final[int] = 12
TAG_BYTES: Final[int] = 16

AAD_PREFIX: Final[str] = "personal-agent-aead-v1"

KeyState = Literal["active", "decrypt_only"]


class CryptoError(RuntimeError):
    """Base class for every failure in this module. Always fail closed."""


class UnknownKeyError(CryptoError):
    """The envelope names a key this ring does not hold."""


class DecryptionError(CryptoError):
    """Authentication failed: ciphertext, tag, AAD or key is wrong."""


class KeyRingError(CryptoError):
    """The key ring itself is not in a usable state."""


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def build_aad(
    *, service: str, table: str, column: str, row_id: str
) -> bytes:
    """The additional authenticated data, exactly as the design fixes it.

    Five lines, single LF separators, no trailing newline. Any drift here makes
    previously written ciphertext unopenable, so the shape is asserted by tests
    rather than left to convention.
    """
    for name, value in (
        ("service", service),
        ("table", table),
        ("column", column),
        ("row_id", row_id),
    ):
        if not isinstance(value, str) or not value:
            raise CryptoError(f"AAD component {name} must be a non-empty string")
        if "\n" in value:
            raise CryptoError(
                f"AAD component {name} must not contain a newline; "
                "it would make the binding ambiguous"
            )
    return "\n".join([AAD_PREFIX, service, table, column, row_id]).encode("utf-8")


@dataclass(frozen=True)
class KeyEntry:
    kid: str
    key: bytes
    state: KeyState

    def __post_init__(self) -> None:
        if len(self.key) != KEY_BYTES:
            raise KeyRingError(
                f"key {self.kid} is {len(self.key)} bytes, expected {KEY_BYTES}"
            )
        if not self.kid:
            raise KeyRingError("key id must not be empty")


class KeyRing:
    """One service's keys. Never serialised, never logged, never persisted."""

    def __init__(self, entries: list[KeyEntry], *, service: str) -> None:
        if not service:
            raise KeyRingError("a key ring must name its service")
        active = [entry for entry in entries if entry.state == "active"]
        if len(active) != 1:
            raise KeyRingError(
                f"a key ring needs exactly one active key, found {len(active)}"
            )
        kids = [entry.kid for entry in entries]
        if len(kids) != len(set(kids)):
            raise KeyRingError("duplicate key ids in ring")

        self.service = service
        self._entries = {entry.kid: entry for entry in entries}
        self._active = active[0]

    @property
    def active_kid(self) -> str:
        return self._active.kid

    @property
    def kids(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def get(self, kid: str) -> KeyEntry:
        try:
            return self._entries[kid]
        except KeyError:
            raise UnknownKeyError(
                f"key id {kid!r} is not in the {self.service} ring"
            ) from None

    def encrypt(
        self, plaintext: bytes, *, table: str, column: str, row_id: str
    ) -> dict[str, Any]:
        """Seal a value under the active key."""
        if not isinstance(plaintext, bytes):
            raise CryptoError("encrypt takes bytes; encode text before sealing")
        aad = build_aad(
            service=self.service, table=table, column=column, row_id=row_id
        )
        # A fresh nonce per encryption. Reuse under one key is catastrophic for
        # GCM, so it is generated here and never derived from row data.
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._active.key).encrypt(nonce, plaintext, aad)
        ciphertext, tag = sealed[:-TAG_BYTES], sealed[-TAG_BYTES:]
        return {
            "v": ENVELOPE_VERSION,
            "kid": self._active.kid,
            "nonce": _b64u_encode(nonce),
            "ciphertext": _b64u_encode(ciphertext),
            "tag": _b64u_encode(tag),
        }

    def decrypt(
        self,
        envelope: dict[str, Any],
        *,
        table: str,
        column: str,
        row_id: str,
    ) -> bytes:
        """Open a value under whichever key sealed it.

        Both `active` and `decrypt_only` keys can open data, which is what makes
        an interrupted rotation harmless.
        """
        if not isinstance(envelope, dict):
            raise DecryptionError("envelope must be a mapping")
        version = envelope.get("v")
        if version != ENVELOPE_VERSION:
            raise DecryptionError(f"unsupported envelope version {version!r}")

        entry = self.get(str(envelope.get("kid", "")))
        aad = build_aad(
            service=self.service, table=table, column=column, row_id=row_id
        )
        try:
            nonce = _b64u_decode(envelope["nonce"])
            ciphertext = _b64u_decode(envelope["ciphertext"])
            tag = _b64u_decode(envelope["tag"])
        except (KeyError, ValueError, TypeError) as exc:
            raise DecryptionError("envelope is malformed") from exc

        try:
            return AESGCM(entry.key).decrypt(nonce, ciphertext + tag, aad)
        except InvalidTag as exc:
            raise DecryptionError(
                "authentication failed: the ciphertext, tag, AAD binding or key "
                "does not match. Failing closed rather than returning a value."
            ) from exc

    def rotated(self, new_key: KeyEntry) -> "KeyRing":
        """Return a ring with `new_key` active and the previous key retained.

        The old key becomes `decrypt_only` rather than being dropped, so rows
        not yet re-encrypted stay readable. Destroying it is a separate,
        deliberate step once a scan proves nothing references it.
        """
        if new_key.state != "active":
            raise KeyRingError("the incoming key must be the new active key")
        if new_key.kid in self._entries:
            raise KeyRingError(f"key id {new_key.kid} is already in the ring")
        retained = [
            KeyEntry(entry.kid, entry.key, "decrypt_only")
            for entry in self._entries.values()
        ]
        return KeyRing([new_key, *retained], service=self.service)


def generate_key(kid: str, *, state: KeyState = "active") -> KeyEntry:
    """Generate a fresh 256-bit key. For tests and provisioning only."""
    return KeyEntry(kid=kid, key=os.urandom(KEY_BYTES), state=state)
