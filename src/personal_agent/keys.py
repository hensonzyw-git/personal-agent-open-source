"""Loading the three key rings the Agent API process holds.

The Agent backend is the only service that *signs*: it mints device access
tokens, it mints the per-call internal JWT Finance MCP verifies, and it seals
conversation payloads. Those are three separate materials on purpose (design 4.3
and 8.5), so this module loads them separately and never derives one from
another:

- **data ring** (symmetric, AES-256-GCM): seals chat requests, resolved write
  intents and the conversation timeline. Never minted at runtime -- a process
  that generated its own key would seal rows the next process could not open,
  and a parked duplicate decision has to survive a restart.
- **token ring** (ES256, private): signs the 10-minute device access token. The
  previous *public* keys stay loadable so a rotation does not reject a token
  that was minted a minute earlier.
- **service ring** (ES256, private): signs the Host Context. Its public half is
  what Finance MCP loads through `PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH`;
  the private half must never be on that side.

Every loader refuses rather than defaulting. A missing key is a deployment
mistake, not a runtime mode, and a service that cannot sign must not start.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_pem_public_key,
)

from personal_agent.auth.tokens import SigningKey, TokenKeyRing
from personal_agent_core.crypto import KeyEntry, KeyRing
from personal_agent_core.host_context import ServiceKey, ServiceKeyRing


#: The AAD's `service_name` component (design 8.5). Changing it makes every
#: previously sealed row unopenable, so it is a constant, not configuration.
SERVICE: Final[str] = "personal-agent-api"

DATA_ACTIVE_KID_ENV: Final[str] = "PERSONAL_AGENT_DATA_ACTIVE_KID"
DATA_ACTIVE_KEY_ENV: Final[str] = "PERSONAL_AGENT_DATA_ACTIVE_KEY_PATH"
#: Optional `kid=path` entries, whitespace-separated, kept decrypt-only.
DATA_PREVIOUS_ENV: Final[str] = "PERSONAL_AGENT_DATA_PREVIOUS_KEYS"

TOKEN_ACTIVE_KID_ENV: Final[str] = "PERSONAL_AGENT_TOKEN_ACTIVE_KID"
TOKEN_ACTIVE_KEY_ENV: Final[str] = "PERSONAL_AGENT_TOKEN_ACTIVE_PRIVATE_KEY_PATH"
#: Optional `kid=path` entries naming retired *public* keys still in overlap.
TOKEN_PREVIOUS_ENV: Final[str] = "PERSONAL_AGENT_TOKEN_PREVIOUS_PUBLIC_KEYS"

SERVICE_ACTIVE_KID_ENV: Final[str] = "PERSONAL_AGENT_SERVICE_ACTIVE_KID"
SERVICE_ACTIVE_KEY_ENV: Final[str] = (
    "PERSONAL_AGENT_SERVICE_ACTIVE_PRIVATE_KEY_PATH"
)
SERVICE_PREVIOUS_ENV: Final[str] = "PERSONAL_AGENT_SERVICE_PREVIOUS_PUBLIC_KEYS"

#: `CAP-001`. Cross-cutting design 5.6 fixes five independent key purposes and
#: forbids reusing one for another. Two of them are symmetric HMAC secrets the
#: Agent holds: the pagination cursor signer, and the identifier/lineage HMAC
#: that turns a legacy conversation id or an internal id into a fingerprint.
#: They are separate because they have different blast radii -- a leaked cursor
#: key lets someone forge a page request, while a leaked identifier key lets
#: someone confirm guesses about which identifiers exist.
CURSOR_ACTIVE_KID_ENV: Final[str] = "PERSONAL_AGENT_CURSOR_ACTIVE_KID"
CURSOR_ACTIVE_KEY_ENV: Final[str] = "PERSONAL_AGENT_CURSOR_ACTIVE_KEY_PATH"
CURSOR_PREVIOUS_ENV: Final[str] = "PERSONAL_AGENT_CURSOR_PREVIOUS_KEYS"

IDENTIFIER_ACTIVE_KID_ENV: Final[str] = "PERSONAL_AGENT_IDENTIFIER_ACTIVE_KID"
IDENTIFIER_ACTIVE_KEY_ENV: Final[str] = (
    "PERSONAL_AGENT_IDENTIFIER_ACTIVE_KEY_PATH"
)
IDENTIFIER_PREVIOUS_ENV: Final[str] = "PERSONAL_AGENT_IDENTIFIER_PREVIOUS_KEYS"

KEY_BYTES: Final[int] = 32


class AgentKeyConfigError(RuntimeError):
    """Agent key material is missing, unreadable or unusable."""


def _entries(value: str | None, env_name: str) -> list[tuple[str, str]]:
    """Parse whitespace-separated `kid=path` pairs, or refuse."""
    parsed: list[tuple[str, str]] = []
    for item in (value or "").split():
        kid, _, path = item.partition("=")
        if not kid or not path:
            raise AgentKeyConfigError(
                f"malformed entry {item!r} in {env_name}; expected kid=path"
            )
        parsed.append((kid, path))
    return parsed


def _read_bytes(path: str, what: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise AgentKeyConfigError(f"cannot read {what} at {path}") from exc


def _load_symmetric(path: str) -> bytes:
    raw = _read_bytes(path, "data key").decode("utf-8", errors="ignore").strip()
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise AgentKeyConfigError("data key is not valid base64") from exc
    if len(key) != KEY_BYTES:
        # Failing here rather than at first use: a short key would otherwise
        # surface as an unopenable row long after the mistake was made.
        raise AgentKeyConfigError(
            f"data key must be {KEY_BYTES} bytes, got {len(key)}"
        )
    return key


def _load_private(path: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = load_pem_private_key(_read_bytes(path, "signing key"), password=None)
    except (ValueError, TypeError) as exc:
        raise AgentKeyConfigError(
            f"signing key at {path} is not an unencrypted PEM private key"
        ) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise AgentKeyConfigError("signing key is not an EC private key")
    if not isinstance(key.curve, ec.SECP256R1):
        # ES256 is fixed by the token contracts; a P-384 key would sign tokens
        # no verifier on either side accepts.
        raise AgentKeyConfigError("signing key must be on the P-256 curve")
    return key


def _load_public(path: str) -> ec.EllipticCurvePublicKey:
    try:
        key = load_pem_public_key(_read_bytes(path, "verification key"))
    except ValueError as exc:
        raise AgentKeyConfigError(
            f"verification key at {path} is not a valid PEM"
        ) from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise AgentKeyConfigError("verification key is not an EC public key")
    if not isinstance(key.curve, ec.SECP256R1):
        raise AgentKeyConfigError("verification key must be on the P-256 curve")
    return key


def _require(env: dict[str, str], kid_name: str, path_name: str) -> tuple[str, str]:
    kid = env.get(kid_name)
    path = env.get(path_name)
    if not kid or not path:
        raise AgentKeyConfigError(
            f"no key configured; set {kid_name} and {path_name}"
        )
    return kid, path


def load_agent_data_keyring(env: dict[str, str] | None = None) -> KeyRing:
    """Build the payload-sealing ring, or refuse to run."""
    env = env if env is not None else dict(os.environ)
    kid, path = _require(env, DATA_ACTIVE_KID_ENV, DATA_ACTIVE_KEY_ENV)

    entries = [KeyEntry(kid=kid, key=_load_symmetric(path), state="active")]
    for retired_kid, retired_path in _entries(
        env.get(DATA_PREVIOUS_ENV), DATA_PREVIOUS_ENV
    ):
        entries.append(
            KeyEntry(
                kid=retired_kid,
                key=_load_symmetric(retired_path),
                state="decrypt_only",
            )
        )
    return KeyRing(entries, service=SERVICE)


@dataclass(frozen=True)
class HmacKey:
    """One symmetric HMAC secret with the id that names it in trace."""

    kid: str
    secret: bytes

    def __post_init__(self) -> None:
        if len(self.secret) != KEY_BYTES:
            raise AgentKeyConfigError(
                f"HMAC key must be {KEY_BYTES} bytes, got {len(self.secret)}"
            )


@dataclass(frozen=True)
class HmacKeyRing:
    """One active HMAC key plus retired verification-only keys."""

    active: HmacKey
    previous: tuple[HmacKey, ...] = ()

    def __post_init__(self) -> None:
        entries = self.verification_keys
        kids = [entry.kid for entry in entries]
        if len(kids) != len(set(kids)):
            raise AgentKeyConfigError("HMAC key ids must be unique")
        for index, entry in enumerate(entries):
            if any(
                hmac.compare_digest(entry.secret, other.secret)
                for other in entries[index + 1 :]
            ):
                raise AgentKeyConfigError(
                    "active and retired HMAC keys must use distinct material"
                )

    @property
    def verification_keys(self) -> tuple[HmacKey, ...]:
        return (self.active, *self.previous)

    @property
    def kid(self) -> str:
        return self.active.kid

    @property
    def secret(self) -> bytes:
        return self.active.secret


def _load_hmac_key(*, kid: str, path: str, purpose: str) -> HmacKey:
    return HmacKey(kid=f"{purpose}:{kid}", secret=_load_symmetric(path))


def _load_hmac_keyring(
    env: dict[str, str],
    kid_name: str,
    path_name: str,
    previous_name: str,
    purpose: str,
) -> HmacKeyRing:
    kid, path = _require(env, kid_name, path_name)
    return HmacKeyRing(
        active=_load_hmac_key(kid=kid, path=path, purpose=purpose),
        previous=tuple(
            _load_hmac_key(kid=retired_kid, path=retired_path, purpose=purpose)
            for retired_kid, retired_path in _entries(
                env.get(previous_name), previous_name
            )
        ),
    )


def load_cursor_key(env: dict[str, str] | None = None) -> HmacKeyRing:
    """The pagination cursor signer (`CAP-001`).

    Separate material from every other purpose. Reusing the data key here would
    mean a signature oracle over the same secret that seals the archive.
    """
    env = env if env is not None else dict(os.environ)
    ring = _load_hmac_keyring(
        env,
        CURSOR_ACTIVE_KID_ENV,
        CURSOR_ACTIVE_KEY_ENV,
        CURSOR_PREVIOUS_ENV,
        "cursor",
    )
    _require_distinct_symmetric(
        env,
        ring,
        own_active_env=CURSOR_ACTIVE_KEY_ENV,
        own_previous_env=CURSOR_PREVIOUS_ENV,
    )
    return ring


def load_identifier_key(env: dict[str, str] | None = None) -> HmacKeyRing:
    """The identifier and lineage fingerprint key (`CAP-001`)."""
    env = env if env is not None else dict(os.environ)
    ring = _load_hmac_keyring(
        env,
        IDENTIFIER_ACTIVE_KID_ENV,
        IDENTIFIER_ACTIVE_KEY_ENV,
        IDENTIFIER_PREVIOUS_ENV,
        "identifier",
    )
    _require_distinct_symmetric(
        env,
        ring,
        own_active_env=IDENTIFIER_ACTIVE_KEY_ENV,
        own_previous_env=IDENTIFIER_PREVIOUS_ENV,
    )
    return ring


def _require_distinct_symmetric(
    env: dict[str, str],
    ring: HmacKeyRing,
    *,
    own_active_env: str,
    own_previous_env: str,
) -> None:
    """Refuse a symmetric key that is also serving another purpose.

    Compared by material, not by path: copying one key file to a second name is
    exactly how key separation quietly stops being real.
    """
    purpose_paths = (
        (DATA_ACTIVE_KEY_ENV, DATA_PREVIOUS_ENV),
        (CURSOR_ACTIVE_KEY_ENV, CURSOR_PREVIOUS_ENV),
        (IDENTIFIER_ACTIVE_KEY_ENV, IDENTIFIER_PREVIOUS_ENV),
    )
    for active_name, previous_name in purpose_paths:
        if (active_name, previous_name) == (
            own_active_env,
            own_previous_env,
        ):
            continue
        paths: list[str] = []
        active_path = env.get(active_name)
        if active_path:
            paths.append(active_path)
        paths.extend(
            path
            for _, path in _entries(env.get(previous_name), previous_name)
        )
        for path in paths:
            material = _load_symmetric(path)
            if any(
                hmac.compare_digest(material, entry.secret)
                for entry in ring.verification_keys
            ):
                raise AgentKeyConfigError(
                    f"the keys for {own_active_env} must be separate material "
                    f"from the keys for {active_name}"
                )


def load_access_token_ring(env: dict[str, str] | None = None) -> TokenKeyRing:
    """Build the device access-token ring: one signer, retired verifiers."""
    env = env if env is not None else dict(os.environ)
    kid, path = _require(env, TOKEN_ACTIVE_KID_ENV, TOKEN_ACTIVE_KEY_ENV)

    private = _load_private(path)
    active = SigningKey(kid, private, private.public_key())
    previous = [
        SigningKey(retired_kid, None, _load_public(retired_path))
        for retired_kid, retired_path in _entries(
            env.get(TOKEN_PREVIOUS_ENV), TOKEN_PREVIOUS_ENV
        )
    ]
    return TokenKeyRing(active=active, previous=previous)


def load_service_signing_ring(env: dict[str, str] | None = None) -> ServiceKeyRing:
    """Build the Host Context signing ring.

    Distinct material from the access-token ring: a compromise of one must not
    let an attacker mint the other's tokens, so active and overlap keys in the
    two rings may not contain the same public key material.
    """
    env = env if env is not None else dict(os.environ)
    kid, path = _require(env, SERVICE_ACTIVE_KID_ENV, SERVICE_ACTIVE_KEY_ENV)
    private = _load_private(path)
    previous = [
        ServiceKey(retired_kid, None, _load_public(retired_path))
        for retired_kid, retired_path in _entries(
            env.get(SERVICE_PREVIOUS_ENV), SERVICE_PREVIOUS_ENV
        )
    ]

    service_publics = {
        private.public_key().public_numbers(),
        *(entry.public_key.public_numbers() for entry in previous),
    }
    token_publics: set[ec.EllipticCurvePublicNumbers] = set()
    token_path = env.get(TOKEN_ACTIVE_KEY_ENV)
    if token_path:
        token_publics.add(_load_private(token_path).public_key().public_numbers())
    token_publics.update(
        _load_public(retired_path).public_numbers()
        for _, retired_path in _entries(
            env.get(TOKEN_PREVIOUS_ENV), TOKEN_PREVIOUS_ENV
        )
    )
    if service_publics & token_publics:
        raise AgentKeyConfigError(
            "the service signing key and the access-token signing key must be "
            "separate material"
        )

    active = ServiceKey(kid, private, private.public_key())
    return ServiceKeyRing(active=active, previous=previous)
