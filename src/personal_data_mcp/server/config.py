"""Where the Finance MCP server is allowed to listen.

Technical design 3.2 puts `personal-data-mcp` on `127.0.0.1:<port>` and keeps
`/mcp` off the public Nginx route entirely. That is a deployment intention, and
a deployment intention that only exists in a document is one edit away from
being wrong. So the bind address is validated in the process itself: a
non-loopback host refuses to start rather than starting and listening.

The host must be a literal loopback IP. A name is refused even when it would
resolve to a loopback address today, because what a name resolves to is not a
property of this configuration.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass


class ServerConfigError(ValueError):
    """The requested listening configuration is not permitted."""


@dataclass(frozen=True)
class ServerConfig:
    """The listening configuration, validated on construction."""

    host: str = "127.0.0.1"
    port: int = 8811
    mcp_path: str = "/mcp"

    def __post_init__(self) -> None:
        require_loopback(self.host)
        if not 0 <= self.port <= 65535:
            raise ServerConfigError(f"port {self.port} is out of range")
        if not self.mcp_path.startswith("/"):
            raise ServerConfigError("mcp_path must be an absolute path")


def require_loopback(host: str) -> None:
    """Refuse anything that is not a literal loopback address."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ServerConfigError(
            f"bind host must be a literal loopback IP, not the name {host!r}"
        ) from None
    if not address.is_loopback:
        raise ServerConfigError(
            f"bind host {host!r} is not a loopback address; "
            "the Finance MCP endpoint must not be reachable off-host"
        )
