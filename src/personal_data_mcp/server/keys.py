"""Loading the service verification key the Finance MCP process holds.

Finance MCP verifies the Agent backend's per-call signature and never signs, so
it loads public keys only. The material comes from a configured PEM file rather
than being baked in: in production it is a systemd credential, and in tests it
is a temp file whose private half the test keeps. Either way the process refuses
to start without it, because a server that cannot verify must not accept calls
(fail closed), and a missing key is a deployment mistake, not a runtime mode.

The active kid signs; retired kids stay verifiable so a key rotation on the
Agent side does not reject calls that were signed a minute before the cutover.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent_core.host_context import ServiceKey, ServiceKeyRing


ACTIVE_KID_ENV: Final[str] = "PERSONAL_DATA_MCP_SERVICE_ACTIVE_KID"
ACTIVE_PEM_ENV: Final[str] = "PERSONAL_DATA_MCP_SERVICE_ACTIVE_PUBLIC_KEY_PATH"
#: Optional `kid=path` entries, whitespace-separated, for retired keys still in
#: the overlap window.
PREVIOUS_ENV: Final[str] = "PERSONAL_DATA_MCP_SERVICE_PREVIOUS_PUBLIC_KEYS"


class ServiceKeyConfigError(RuntimeError):
    """The verification key material is missing or unreadable."""


def _load_public(path: str) -> ec.EllipticCurvePublicKey:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ServiceKeyConfigError(
            f"cannot read verification key at {path}"
        ) from exc
    try:
        key = load_pem_public_key(data)
    except ValueError as exc:
        raise ServiceKeyConfigError("verification key is not a valid PEM") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ServiceKeyConfigError("verification key is not an EC public key")
    return key


def load_verification_ring(env: dict[str, str] | None = None) -> ServiceKeyRing:
    """Build the public-only verification ring from configuration.

    Raises rather than returning a permissive default: without a key there is
    nothing to verify against, and the server must not start.
    """
    env = env if env is not None else dict(os.environ)

    active_kid = env.get(ACTIVE_KID_ENV)
    active_path = env.get(ACTIVE_PEM_ENV)
    if not active_kid or not active_path:
        raise ServiceKeyConfigError(
            "no service verification key configured; set "
            f"{ACTIVE_KID_ENV} and {ACTIVE_PEM_ENV}"
        )

    active = ServiceKey(active_kid, None, _load_public(active_path))

    previous: list[ServiceKey] = []
    for entry in (env.get(PREVIOUS_ENV) or "").split():
        kid, _, path = entry.partition("=")
        if not kid or not path:
            raise ServiceKeyConfigError(
                f"malformed retired-key entry {entry!r}; expected kid=path"
            )
        previous.append(ServiceKey(kid, None, _load_public(path)))

    return ServiceKeyRing(active=active, previous=previous)
