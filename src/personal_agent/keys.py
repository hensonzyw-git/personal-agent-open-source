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
import os
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
