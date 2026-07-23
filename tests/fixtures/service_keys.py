"""Test-side signing for the Finance MCP authorisation gate.

The server holds only the verification public key; a real caller is the Agent
backend, which signs. In tests this helper plays that role: it owns a private
service key, can write the matching public PEM where the server will load it,
and mints the headers a governed Streamable HTTP call carries.

Keeping it here means every test signs the same way the bridge does, so a test
that passes proves the real header contract, not a private one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent_core.host_context import (
    HostContext,
    ServiceKey,
    ServiceKeyRing,
    sign_host_context,
)
from personal_data_mcp.server.authz import Authorizer
from personal_data_mcp.server.keys import (
    ACTIVE_KID_ENV,
    ACTIVE_PEM_ENV,
)


DEFAULT_KID = "svc-test-1"
DEFAULT_DEVICE = "device-test-1"
DEFAULT_USER = "henson"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
DEFAULT_ATV = "atv-test"


@dataclass
class SignedCaller:
    """Signs Host Contexts the way the governed bridge does."""

    kid: str = DEFAULT_KID
    device_id: str = DEFAULT_DEVICE
    user_id: str = DEFAULT_USER
    scopes: tuple[str, ...] = ("meta.capabilities.read",)
    allowed_tools_version: str = DEFAULT_ATV
    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    @property
    def ring(self) -> ServiceKeyRing:
        return ServiceKeyRing(
            active=ServiceKey(self.kid, self.private_key, self.private_key.public_key())
        )

    def authorizer(self) -> Authorizer:
        return Authorizer(self.ring)

    def public_pem(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def write_public_pem(self, directory: Path) -> Path:
        path = directory / "service_public.pem"
        path.write_bytes(self.public_pem())
        return path

    def env(self, directory: Path) -> dict[str, str]:
        """Environment that points the server at this caller's public key."""
        path = self.write_public_pem(directory)
        return {ACTIVE_KID_ENV: self.kid, ACTIVE_PEM_ENV: str(path)}

    def host_context(
        self,
        tool: str,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        scopes: tuple[str, ...] | None = None,
    ) -> HostContext:
        return HostContext(
            agent_id="agent-test",
            device_id=self.device_id,
            user_id=self.user_id,
            scopes=scopes if scopes is not None else self.scopes,
            tool=tool,
            request_id=request_id or str(uuid.uuid4()),
            trace_id=DEFAULT_TRACE,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
            request_fingerprint="fp-test",
            allowed_tools_version=self.allowed_tools_version,
            timezone=DEFAULT_TIMEZONE,
        )

    def headers(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        host: HostContext | None = None,
        **host_kwargs: Any,
    ) -> dict[str, str]:
        """The six headers a governed HTTP call sends, signed over `arguments`."""
        host = host or self.host_context(tool, **host_kwargs)
        token = sign_host_context(self.ring, host, arguments)
        return {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": host.request_id,
            "Idempotency-Key": host.idempotency_key,
            "traceparent": host.trace_id,
            "X-User-ID": host.user_id,
            "X-Timezone": host.timezone,
        }
