"""Independent MCP Client Core.

The official SDK supplies protocol and transport primitives. Lifecycle, catalog,
timeouts, cancellation and error mapping live here instead of inside an agent
framework, because PRD 7.1 requires those to be observable and testable on their
own, and because the framework is meant to be replaceable without migrating the
connectors.

Two behaviours follow the 2026-07-28 spec rather than intuition:

- pagination is followed to exhaustion. A first page is not a catalog, and a
  server is free to return one tool per page;
- a dropped connection is not a cancellation. The spec is explicit about this,
  so a transport failure raises rather than resolving as "cancelled", and the
  caller has to treat the call as possibly in flight.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Set
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Literal

import anyio
import httpx2
from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.client import negotiate_auto
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams, Tool

from personal_agent_core.errors import AppError, ClarificationQuestion, ErrorCode
from personal_agent_core.mcp_protocol import (
    MODERN_PROTOCOL_VERSION,
    THIRD_PARTY_PROTOCOL_VERSIONS,
)


PROTOCOL_VERSION: Final[str] = MODERN_PROTOCOL_VERSION

DEFAULT_INITIALIZE_TIMEOUT: Final[timedelta] = timedelta(seconds=5)
DEFAULT_LIST_TIMEOUT: Final[timedelta] = timedelta(seconds=5)
DEFAULT_READ_CALL_TIMEOUT: Final[timedelta] = timedelta(seconds=15)
DEFAULT_WRITE_CALL_TIMEOUT: Final[timedelta] = timedelta(seconds=30)

#: How long the HTTP transport will wait for bytes, which is *not* the call
#: deadline: `call_tool` owns that, per call, from the contract's effect.
#:
#: It exists because httpx defaults to a 5-second read timeout, and a governed
#: write is a single request whose server side talks to Feishu several times.
#: Live evidence (2026-07-26): a real `finance.log_expense` created the record,
#: read it back and reached `succeeded` in Finance, while the Agent saw nothing
#: -- the stream had already been torn down at 5s, so the session waited out its
#: own budget and reported `source_commit_unknown`. A verified write reported as
#: an unknown commit is the worst outcome this system has, and no offline test
#: could see it: every fake counterparty answers in milliseconds.
#:
#: Kept comfortably above the longest call budget so the deadline that fires is
#: always the call's, never the socket's.
TRANSPORT_READ_TIMEOUT: Final[timedelta] = (
    DEFAULT_WRITE_CALL_TIMEOUT + timedelta(seconds=15)
)

#: Loopback connect and write are fast or broken; there is nothing to wait for.
TRANSPORT_CONNECT_TIMEOUT: Final[timedelta] = timedelta(seconds=5)

#: A server is free to paginate one tool at a time; this only stops a runaway.
MAX_CATALOG_PAGES: Final[int] = 100


def _error_payload(result: Any) -> dict[str, Any] | None:
    """Extract the stable error envelope without trusting free-form text."""
    candidates: list[Any] = [result.structured_content]
    candidates.extend(result.content or [])
    for candidate in candidates:
        payload: Any = candidate
        if hasattr(candidate, "text"):
            try:
                payload = json.loads(candidate.text)
            except (TypeError, json.JSONDecodeError):
                continue
        if not isinstance(payload, dict):
            continue
        error = payload.get("error", payload)
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error
    return None


def _validate_protocol_version(
    connector_id: str,
    observed: str,
    allowed_protocol_versions: frozenset[str] = THIRD_PARTY_PROTOCOL_VERSIONS,
) -> None:
    if observed not in allowed_protocol_versions:
        raise McpTransportError(
            f"{connector_id} negotiated unsupported MCP protocol "
            f"{observed!r}; allowed "
            f"{sorted(allowed_protocol_versions)!r}"
        )


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

    def __init__(
        self,
        connector_id: str,
        transport: Transport,
        *,
        allowed_protocol_versions: Set[str] = (
            THIRD_PARTY_PROTOCOL_VERSIONS
        ),
    ) -> None:
        protocol_versions = frozenset(allowed_protocol_versions)
        if not protocol_versions:
            raise ValueError("allowed_protocol_versions must not be empty")
        if not protocol_versions <= THIRD_PARTY_PROTOCOL_VERSIONS:
            raise ValueError(
                "allowed_protocol_versions contains an unsupported version"
            )
        self.connector_id = connector_id
        self.transport = transport
        self.allowed_protocol_versions = protocol_versions
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
                    env = dict(self.transport.env)
                    env.setdefault(
                        "PYTHONPATH", os.pathsep.join(sys.path)
                    )
                    read_stream, write_stream = await stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=self.transport.command,
                                args=self.transport.args,
                                env=env or None,
                            )
                        )
                    )
                else:
                    # Connector credentials ride on this client and nowhere
                    # else, so one server's headers never reach another. System
                    # proxy settings are deliberately ignored: Finance uses a
                    # loopback endpoint and its credentials must never leave
                    # this machine through an ambient proxy.
                    #
                    # The timeout is stated rather than inherited. httpx's
                    # default read timeout is 5 seconds, which is shorter than a
                    # real governed write takes, and a stream torn down early
                    # turns a *completed* write into an unknown commit.
                    http_client = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            headers=self.transport.headers,
                            trust_env=False,
                            timeout=httpx2.Timeout(
                                TRANSPORT_READ_TIMEOUT.total_seconds(),
                                connect=TRANSPORT_CONNECT_TIMEOUT.total_seconds(),
                            ),
                        )
                    )
                    read_stream, write_stream = await stack.enter_async_context(
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
                    await negotiate_auto(session)
                _validate_protocol_version(
                    self.connector_id,
                    session.protocol_version,
                    self.allowed_protocol_versions,
                )

                self._session = session
                info = session.server_info
                self._identity = ServerIdentity(
                    name=info.name if info is not None else "",
                    version=info.version if info is not None else "",
                    protocol_version=session.protocol_version,
                    capabilities=(
                        session.server_capabilities.model_dump(exclude_none=True)
                        if session.server_capabilities is not None
                        else {}
                    ),
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
            # Unwrap ExceptionGroup (anyio task group) to surface the real
            # error — e.g. a protocol-version mismatch — instead of the
            # opaque "ExceptionGroup" type name.
            root = failure
            while hasattr(root, "exceptions") and root.exceptions:
                root = root.exceptions[0]
            detail = str(root) if root else "unknown"
            raise McpTransportError(
                f"{self.connector_id} failed to connect: {detail}"
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

    async def list_tools(self) -> list[Tool]:
        """Read the whole catalog, following pagination to exhaustion."""
        session = self._require_session()
        tools: list[Tool] = []
        cursor: str | None = None
        for _ in range(MAX_CATALOG_PAGES):
            try:
                with anyio.fail_after(DEFAULT_LIST_TIMEOUT.total_seconds()):
                    page = await session.list_tools(
                        params=PaginatedRequestParams(cursor=cursor)
                        if cursor
                        else None
                    )
            except TimeoutError as exc:
                raise McpTimeoutError(
                    f"{self.connector_id} tools/list timed out"
                ) from exc
            except MCPError as exc:
                # Same split as `call_tool`: v2 reports a request budget expiry
                # as MCPError -32001, and everything else here is the transport.
                if exc.code == -32001:
                    raise McpTimeoutError(
                        f"{self.connector_id} tools/list timed out"
                    ) from exc
                raise McpTransportError(
                    f"{self.connector_id} tools/list failed: {type(exc).__name__}"
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A server killed mid-session surfaces here as a raw SDK error.
                # Leaking it would mean callers cannot catch a dead connector
                # with the same `except` clause that already catches one on
                # `call_tool`, so composition's discovery diagnosis is skipped
                # and the operator gets an SDK traceback instead of "the Finance
                # MCP service could not be reached". The error vocabulary of
                # this client has to be uniform across its methods, because that
                # is what every caller's handler is written against.
                raise McpTransportError(
                    f"{self.connector_id} tools/list failed: {type(exc).__name__}"
                ) from exc
            tools.extend(page.tools)
            cursor = page.next_cursor
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
        host_context: dict[str, str] | None = None,
    ) -> Any:
        """Invoke one tool.

        `host_context` is the per-call authorisation the Host binds to this
        invocation. The caller states it once and this method routes it to the
        channel the transport actually has: HTTP headers, or `_meta` for stdio,
        which has no header layer. Callers cannot pick the channel themselves,
        so a context can neither be sent twice nor silently dropped.

        A timeout raises rather than returning a value. For a write the caller
        must then treat the call as possibly committed, which is exactly what the
        Finance execution state machine is built to resolve.
        """
        meta: dict[str, Any] | None = None
        if host_context:
            if isinstance(self.transport, StreamableHttpTransport):
                # Headers only. Repeating the bearer token in `_meta` would put
                # it in the JSON-RPC body, which server frameworks routinely
                # log, for no gain over the header that already carries it.
                #
                # It cannot be installed by mutating the shared AsyncClient
                # either: concurrent calls would receive each other's identity.
                # The protocol is stateless, so a short isolated connection is
                # the safe boundary.
                call_transport = StreamableHttpTransport(
                    url=self.transport.url,
                    headers={**self.transport.headers, **host_context},
                )
                async with McpClientCore(
                    self.connector_id,
                    call_transport,
                    allowed_protocol_versions=self.allowed_protocol_versions,
                ) as call_client:
                    return await call_client.call_tool(
                        name, arguments, timeout=timeout
                    )
            # stdio has no headers, so `_meta` is the only channel available.
            meta = dict(host_context)

        session = self._require_session()
        try:
            # The SDK enforces the per-request budget itself; wrapping it in a
            # second, outer timeout is what tore the cancel scope apart.
            result = await session.call_tool(
                name,
                arguments,
                read_timeout_seconds=timeout.total_seconds(),
                meta=meta,
            )
        except TimeoutError as exc:
            raise McpTimeoutError(
                f"{self.connector_id}.{name} exceeded {timeout}"
            ) from exc
        except MCPError as exc:
            # v2 raises MCPError with code -32001 (REQUEST_TIMEOUT) when a
            # call exceeds its budget. Without this branch every budget
            # expiry is mislabeled as a transport failure.
            if exc.code == -32001:
                raise McpTimeoutError(
                    f"{self.connector_id}.{name} exceeded {timeout}"
                ) from exc
            raise McpTransportError(
                f"{self.connector_id}.{name} failed: {type(exc).__name__}"
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

        if result.is_error:
            payload = _error_payload(result)
            clarification_question = None
            if payload is not None:
                try:
                    code = ErrorCode(payload["code"])
                except ValueError:
                    code = ErrorCode.INTERNAL_ERROR
                if code is ErrorCode.CLARIFICATION_REQUIRED:
                    try:
                        clarification_question = ClarificationQuestion(
                            payload.get("clarification_question")
                        )
                    except (TypeError, ValueError):
                        # A connector cannot introduce arbitrary prose into a
                        # persisted clarification chain.  Keep the generic,
                        # stable code when its optional closed field is absent
                        # or malformed.
                        clarification_question = None
            else:
                code = ErrorCode.INTERNAL_ERROR
            raise AppError(
                code,
                internal_detail=(
                    f"{self.connector_id}.{name} returned is_error"
                    + (
                        f" with stable code {payload['code']}"
                        if payload is not None
                        else " without a valid error envelope"
                    )
                ),
                clarification_question=clarification_question,
            )
        return result.structured_content or result.content

    async def list_resources(self) -> list[Any]:
        """Read the complete resource catalog, following every cursor."""
        session = self._require_session()
        resources: list[Any] = []
        cursor: str | None = None
        for _ in range(MAX_CATALOG_PAGES):
            try:
                with anyio.fail_after(DEFAULT_LIST_TIMEOUT.total_seconds()):
                    page = await session.list_resources(
                        params=PaginatedRequestParams(cursor=cursor)
                        if cursor
                        else None
                    )
            except TimeoutError as exc:
                raise McpTimeoutError(
                    f"{self.connector_id} resources/list timed out"
                ) from exc
            resources.extend(page.resources)
            cursor = page.next_cursor
            if not cursor:
                return resources
        raise McpTransportError(
            f"{self.connector_id} did not finish paginating resources/list"
        )

    async def read_resource(self, uri: str) -> Any:
        with anyio.fail_after(DEFAULT_LIST_TIMEOUT.total_seconds()):
            result = await self._require_session().read_resource(uri)
        return result.contents

    async def __aenter__(self) -> "McpClientCore":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
