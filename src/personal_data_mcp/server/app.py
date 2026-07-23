"""The Finance MCP server: loopback Streamable HTTP, and nothing else.

Two decisions are made here rather than inherited.

**GET is refused.** The MCP specification permits a Streamable HTTP server to
answer GET with 405, and this SDK does not: a GET carrying the negotiated
`Accept` header opens a `text/event-stream` and holds it open. That default is
spec-legal but wrong for this deployment. Technical design 6.1 fixes v0.1 as
stateless request/response with no business state on an MCP session, and the
server declares no sampling, roots, elicitation, prompts or `listChanged`
notifications — there is nothing a server-initiated stream could ever carry. A
held-open GET would therefore pin a connection and a file descriptor before any
authorisation has been evaluated, on a service that will run under a restricted
systemd user. A capability with no use is refused rather than left reachable.

**The listening address is enforced in the process.** See `config.py`.

The Host Context arrives in HTTP headers, which is the channel the Client Core
chose for this transport. The middleware lifts them out of the request and into
a context variable so the tool handler reads them from one place, and so a
request that carried none is distinguishable from one whose headers were lost.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Final

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, Tool
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.server.config import ServerConfig
from personal_data_mcp.server.errors import (
    error_result,
    internal_error_result,
    success_result,
)
from personal_data_mcp.server.handlers import ToolInvocation, ToolRegistry
from personal_data_mcp.server import meta


SERVER_NAME: Final[str] = "personal-data-mcp"

#: Headers the Client Core sends for a governed call. Read into a context
#: variable so the handler never reaches back into the transport.
_request_headers: ContextVar[dict[str, str]] = ContextVar(
    "personal_data_mcp_request_headers", default={}
)


def current_request_headers() -> dict[str, str]:
    """The lower-cased headers of the request being served, if any."""
    return _request_headers.get()


class LoopbackHttpGuard:
    """Refuse GET on the MCP path, and publish the request headers."""

    def __init__(self, app: ASGIApp, *, mcp_path: str) -> None:
        self.app = app
        self.mcp_path = mcp_path.rstrip("/") or "/"

    def _is_mcp(self, path: str) -> bool:
        return (path.rstrip("/") or "/") == self.mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] == "GET" and self._is_mcp(scope.get("path", "")):
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [
                        (b"allow", b"POST"),
                        (b"content-type", b"text/plain; charset=utf-8"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"Method Not Allowed"})
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        token = _request_headers.set(headers)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_headers.reset(token)


async def dispatch(
    registry: ToolRegistry,
    name: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """Resolve one `tools/call` to a result, with no path that raises.

    Every branch leaves through the error envelope, including an unexpected
    exception, so the wire can only ever carry a stable code.
    """
    try:
        handler = registry.handler(name)
        if handler is None:
            # Not discoverable, so reaching here means the caller guessed a
            # name. It is refused identically whether the tool is unknown,
            # disabled or simply not built yet: a caller must not be able to
            # map this server's surface by comparing error codes.
            return error_result(
                AppError(
                    ErrorCode.TOOL_NOT_ALLOWLISTED,
                    internal_detail=f"no handler registered for {name!r}",
                )
            )
        payload = await handler(ToolInvocation(tool=name, arguments=arguments))
        return success_result(payload)
    except AppError as error:
        return error_result(error)
    except Exception:
        return internal_error_result()


def build_registry() -> ToolRegistry:
    """The production tool set for this build."""
    registry = ToolRegistry()
    registry.register(meta.TOOL_NAME, meta.build_handler(registry))
    return registry


def build_server(
    config: ServerConfig | None = None,
    registry: ToolRegistry | None = None,
) -> FastMCP:
    config = config or ServerConfig()
    registry = registry if registry is not None else build_registry()

    server = FastMCP(
        SERVER_NAME,
        host=config.host,
        port=config.port,
        streamable_http_path=config.mcp_path,
        json_response=True,
        stateless_http=True,
    )

    @server._mcp_server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=entry["name"],
                description=entry["description"],
                inputSchema=entry["inputSchema"],
            )
            for entry in registry.catalog()
        ]

    # `validate_input=False` because the SDK's own validation failure is a
    # free-text message, not a stable code. Arguments are validated inside the
    # dispatch path instead, so every rejection has the same shape.
    @server._mcp_server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        return await dispatch(registry, name, arguments)

    return server


def build_app(
    config: ServerConfig | None = None,
    registry: ToolRegistry | None = None,
) -> Starlette:
    """The ASGI application, guard included."""
    config = config or ServerConfig()
    server = build_server(config, registry)
    app = server.streamable_http_app()
    app.add_middleware(LoopbackHttpGuard, mcp_path=config.mcp_path)
    return app
