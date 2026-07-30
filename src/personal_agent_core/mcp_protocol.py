"""Frozen MCP protocol policy shared by the Host and Finance service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from mcp import MCPError
from mcp.types import METHOD_NOT_FOUND, UNSUPPORTED_PROTOCOL_VERSION


MODERN_PROTOCOL_VERSION: Final[str] = "2026-07-28"
LEGACY_PROTOCOL_VERSION: Final[str] = "2025-11-25"

# Generic third-party connectors may explicitly use the SDK's one-step legacy
# fallback. The self-owned Finance connector and server are modern-only.
THIRD_PARTY_PROTOCOL_VERSIONS: Final[frozenset[str]] = frozenset(
    {MODERN_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION}
)
FINANCE_PROTOCOL_VERSIONS: Final[frozenset[str]] = frozenset(
    {MODERN_PROTOCOL_VERSION}
)

@dataclass(frozen=True)
class ModernProtocolOnlyMiddleware:
    """Veto the SDK's legacy initialize compatibility path.

    MCP SDK v2 deliberately serves both protocol eras. Finance is stricter:
    accepting a legacy ``initialize`` would silently re-enable protocol
    sessions and bypass the frozen 2026-07-28 deployment gate.
    """

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method == "initialize":
            raise MCPError(
                code=METHOD_NOT_FOUND,
                message="Method not found",
                data="initialize",
            )
        if ctx.protocol_version != MODERN_PROTOCOL_VERSION:
            requested = ctx.protocol_version
            raise MCPError(
                code=UNSUPPORTED_PROTOCOL_VERSION,
                message="Unsupported protocol version",
                data={
                    "requested": requested,
                    "supported": [MODERN_PROTOCOL_VERSION],
                },
            )
        return await call_next(ctx)
