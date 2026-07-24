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

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, Tool
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import HOST_ONLY_FIELDS, ServiceKeyRing
from personal_data_mcp.server.authz import Authorizer
from personal_data_mcp.server.config import ServerConfig
from personal_data_mcp.server.control import SessionFactory, build_control_app
from personal_data_mcp.server.errors import (
    error_result,
    internal_error_result,
    success_result,
)
from personal_data_mcp.server.handlers import ToolInvocation, ToolRegistry
from personal_data_mcp.server.keys import load_verification_ring
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
    authorizer: Authorizer,
    name: str,
    arguments: dict[str, Any],
    headers: dict[str, str],
) -> CallToolResult:
    """Resolve one `tools/call` to a result, with no path that raises.

    The order is deliberate and load-bearing: the handler runs only after the
    Host Context is verified. A handler is the only thing that creates an
    execution record, so a rejected call cannot leave one behind. Every branch,
    including an unexpected exception, leaves through the error envelope, so the
    wire can only ever carry a stable code.
    """
    try:
        contract = registry.contract(name)
        handler = registry.handler(name)
        if handler is None or contract is None:
            # Not discoverable, so reaching here means the caller guessed a
            # name. It is refused identically whether the tool is unknown,
            # disabled or simply not built yet: a caller must not be able to
            # map this server's surface by comparing error codes.
            #
            # Authorisation is not attempted for an unknown tool: there is no
            # contract to state its required scopes, and the binding would have
            # nothing meaningful to check the tool name against.
            return error_result(
                AppError(
                    ErrorCode.TOOL_NOT_ALLOWLISTED,
                    internal_detail=f"no handler registered for {name!r}",
                )
            )

        # Verify before the handler. Nothing below this line may run for a
        # call that fails here.
        verified_call = authorizer.authorize(
            tool=name,
            arguments=arguments,
            headers=headers,
            required_scopes=tuple(contract["required_scopes"]),
        )
        forbidden = sorted(set(arguments) & HOST_ONLY_FIELDS)
        if forbidden:
            raise AppError(
                ErrorCode.HOST_CONTEXT_MISMATCH,
                internal_detail=(
                    f"{name} supplied Host-only fields {forbidden}"
                ),
            )
        try:
            Draft202012Validator(
                contract["model_input_schema"],
                format_checker=FormatChecker(),
            ).validate(arguments)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=(
                    f"{name} input failed schema validation at "
                    f"{list(exc.absolute_path)}"
                ),
            ) from exc

        payload = await handler(
            ToolInvocation(
                tool=name,
                arguments=arguments,
                verified_call=verified_call,
            )
        )
        return success_result(payload)
    except AppError as error:
        return error_result(error)
    except Exception:
        return internal_error_result()


def build_registry(
    *, expense_query_handler=None
) -> ToolRegistry:
    """The production tool set for this build."""
    registry = ToolRegistry()
    registry.register(meta.TOOL_NAME, meta.build_handler(registry))
    # The default server deliberately remains credential-free.  Service
    # composition supplies this only after it has loaded and freshly validated
    # the protected finance config; until then an enabled contract is not
    # advertised as executable.
    if expense_query_handler is not None:
        registry.register("finance.query_expenses", expense_query_handler)
    return registry


def build_server(
    config: ServerConfig | None = None,
    registry: ToolRegistry | None = None,
    authorizer: Authorizer | None = None,
    *,
    verification_ring: ServiceKeyRing | None = None,
) -> FastMCP:
    config = config or ServerConfig()
    registry = registry if registry is not None else build_registry()
    if authorizer is None:
        ring = verification_ring or load_verification_ring()
        authorizer = Authorizer(ring)

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
        return await dispatch(
            registry, authorizer, name, arguments, current_request_headers()
        )

    return server


def build_app(
    config: ServerConfig | None = None,
    registry: ToolRegistry | None = None,
    *,
    verification_ring: ServiceKeyRing | None = None,
    session_factory: SessionFactory | None = None,
) -> Starlette:
    """The ASGI application: the MCP endpoint, and the control API if a database
    is wired.

    The verification ring is resolved once and shared by the tool-call gate and
    the control plane, so both check signatures against the same keys. The
    control routes are only mounted when a `session_factory` is supplied; the
    MCP-only path (no database) still stands up for transport work.
    """
    config = config or ServerConfig()
    ring = verification_ring or load_verification_ring()
    server = build_server(config, registry, Authorizer(ring))
    app = server.streamable_http_app()

    if session_factory is not None:
        # The control API is a sibling on /internal, never an MCP tool and never
        # on /mcp. Adding its routes to the same app keeps it one loopback
        # service while keeping it off the model's tool surface entirely.
        control = build_control_app(
            verification_ring=ring, session_factory=session_factory
        )
        app.router.routes.extend(control.router.routes)

    app.add_middleware(LoopbackHttpGuard, mcp_path=config.mcp_path)
    return app
