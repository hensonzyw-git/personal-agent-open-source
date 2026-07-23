"""Independent MCP Client Core.

The official SDK supplies protocol and transport primitives. Lifecycle, catalog,
timeouts, cancellation and error mapping live here instead of inside an agent
framework, because PRD 7.1 requires those to be observable and testable on their
own, and because the framework is meant to be replaceable without migrating the
connectors.

Two behaviours follow the 2025-11-25 spec rather than intuition:

- pagination is followed to exhaustion. A first page is not a catalog, and a
  server is free to return one tool per page;
- a dropped connection is not a cancellation. The spec is explicit about this,
  so a transport failure raises rather than resolving as "cancelled", and the
  caller has to treat the call as possibly in flight.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Literal

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from personal_agent_core.errors import AppError, ErrorCode


PROTOCOL_VERSION: Final[str] = "2025-11-25"

DEFAULT_INITIALIZE_TIMEOUT: Final[timedelta] = timedelta(seconds=5)
DEFAULT_LIST_TIMEOUT: Final[timedelta] = timedelta(seconds=5)
DEFAULT_READ_CALL_TIMEOUT: Final[timedelta] = timedelta(seconds=15)
DEFAULT_WRITE_CALL_TIMEOUT: Final[timedelta] = timedelta(seconds=30)

#: A server is free to paginate one tool at a time; this only stops a runaway.
MAX_CATALOG_PAGES: Final[int] = 100


class McpTransportError(RuntimeError):
    """The connection failed. Never means the call did not happen."""


class McpTimeoutError(RuntimeError):
    """A request exceeded its budget. For writes, the outcome is unknown."""


@dataclass(frozen=True)
class StdioTransport:
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    kind: Literal["stdio"] = "stdio"


@dataclass(frozen=True)
class StreamableHttpTransport:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    kind: Literal["streamable_http"] = "streamable_http"


Transport = StdioTransport | StreamableHttpTransport


@dataclass(frozen=True)
class ServerIdentity:
    name: str
    version: str
    protocol_version: str
    capabilities: dict[str, Any]


class McpClientCore:
    """One connection to one MCP server.

    Deliberately not a pool and not a singleton: connector credentials must not
    be shared between servers, and one server's failure must not take another's
    catalog with it.
    """

    def __init__(self, connector_id: str, transport: Transport) -> None:
        self.connector_id = connector_id
        self.transport = transport
        self._session: ClientSession | None = None
        self._identity: ServerIdentity | None = None
        self._task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()
        self._failure: BaseException | None = None

    @property
    def identity(self) -> ServerIdentity:
        if self._identity is None:
            raise McpTransportError(f"{self.connector_id} is not initialized")
        return self._identity

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    async def _run(self, ready: asyncio.Event) -> None:
        """Own the connection for its whole life, in one task.

        anyio requires a cancel scope to be exited by the task that entered it.
        Holding the transport in an AsyncExitStack across method calls broke that
        the moment the SDK's internal task group cancelled, which is what a
        refused connection does, and unwinding then raised its own error on top
        of the real one. Entering and exiting here keeps every scope in one task.
        """
        stack = AsyncExitStack()
        try:
            async with stack:
                if isinstance(self.transport, StdioTransport):
                    read_stream, write_stream = await stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=self.transport.command,
                                args=self.transport.args,
                                env=self.transport.env or None,
                            )
                        )
                    )
                else:
                    # Connector credentials ride on this client and nowhere
                    # else, so one server's headers never reach another.
                    http_client = None
                    if self.transport.headers:
                        http_client = await stack.enter_async_context(
                            httpx.AsyncClient(headers=self.transport.headers)
                        )
                    read_stream, write_stream, _ = await stack.enter_async_context(
                        streamable_http_client(
                            self.transport.url, http_client=http_client
                        )
                    )
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                with anyio.fail_after(
                    DEFAULT_INITIALIZE_TIMEOUT.total_seconds()
                ):
                    result = await session.initialize()

                self._session = session
                self._identity = ServerIdentity(
                    name=result.serverInfo.name,
                    version=result.serverInfo.version,
                    protocol_version=result.protocolVersion,
                    capabilities=result.capabilities.model_dump(exclude_none=True),
                )
                ready.set()
                await self._shutdown.wait()
        except BaseException as exc:
            self._failure = exc
        finally:
            self._session = None
            ready.set()

    async def connect(self) -> ServerIdentity:
        """Open the transport and complete the MCP handshake."""
        if self._session is not None:
            return self.identity

        self._shutdown = asyncio.Event()
        self._failure = None
        ready = asyncio.Event()
        self._task = asyncio.create_task(self._run(ready))
        await ready.wait()

        if self._session is None:
            failure = self._failure
            await self._await_task()
            if isinstance(failure, TimeoutError):
                raise McpTimeoutError(
                    f"{self.connector_id} did not complete initialize in time"
                ) from failure
            raise McpTransportError(
                f"{self.connector_id} failed to connect: "
                f"{type(failure).__name__ if failure else 'unknown'}"
            ) from failure
        return self.identity

    async def _await_task(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._shutdown.set()
        try:
            await task
        except BaseException:
            # The connection is already gone; a teardown error adds nothing.
            pass

    async def close(self) -> None:
        """Shut down cleanly. Safe to call more than once."""
        self._session = None
        self._identity = None
        await self._await_task()

    async def reconnect(self) -> ServerIdentity:
        """Re-establish after a restart. The catalog must be re-read."""
        await self.close()
        return await self.connect()

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise McpTransportError(f"{self.connector_id} is not connected")
        return self._session

    async def ping(self) -> bool:
        try:
            with anyio.fail_after(5):
                await self._require_session().send_ping()
        except Exception:
            return False
        return True

    async def list_tools(self) -> list[Tool]:
        """Read the whole catalog, following pagination to exhaustion."""
        session = self._require_session()
        tools: list[Tool] = []
        cursor: str | None = None
        for _ in range(MAX_CATALOG_PAGES):
            try:
                with anyio.fail_after(DEFAULT_LIST_TIMEOUT.total_seconds()):
                    page = await session.list_tools(cursor=cursor)
            except TimeoutError as exc:
                raise McpTimeoutError(
                    f"{self.connector_id} tools/list timed out"
                ) from exc
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                return tools
        raise McpTransportError(
            f"{self.connector_id} did not finish paginating tools/list"
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: timedelta = DEFAULT_READ_CALL_TIMEOUT,
    ) -> Any:
        """Invoke one tool.

        A timeout raises rather than returning a value. For a write the caller
        must then treat the call as possibly committed, which is exactly what the
        Finance execution state machine is built to resolve.
        """
        session = self._require_session()
        try:
            # The SDK enforces the per-request budget itself; wrapping it in a
            # second, outer timeout is what tore the cancel scope apart.
            result = await session.call_tool(
                name, arguments, read_timeout_seconds=timeout
            )
        except TimeoutError as exc:
            raise McpTimeoutError(
                f"{self.connector_id}.{name} exceeded {timeout}"
            ) from exc
        except asyncio.CancelledError:
            # Propagated so the SDK sends the cancellation notification. The
            # spec is explicit that this is not the same as the server having
            # stopped work, so callers must not read it as "nothing happened".
            raise
        except Exception as exc:
            raise McpTransportError(
                f"{self.connector_id}.{name} failed: {type(exc).__name__}"
            ) from exc

        if result.isError:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=f"{self.connector_id}.{name} returned isError",
            )
        return result.structuredContent or result.content

    async def list_resources(self) -> list[Any]:
        with anyio.fail_after(DEFAULT_LIST_TIMEOUT.total_seconds()):
            result = await self._require_session().list_resources()
        return list(result.resources)

    async def read_resource(self, uri: str) -> Any:
        from pydantic import AnyUrl

        with anyio.fail_after(DEFAULT_LIST_TIMEOUT.total_seconds()):
            result = await self._require_session().read_resource(AnyUrl(uri))
        return result.contents

    async def __aenter__(self) -> "McpClientCore":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
