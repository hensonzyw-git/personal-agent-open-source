"""DEV-041 step 5: dual-version matrix and conformance tests.

Tests that the v2 migration (protocol 2026-07-28) inter-operates correctly
with legacy 2025-11-25 servers, that the Finance server forces 2026-07-28,
and that the modern path has no initialize/ping/session-id/GET-SSE leakage.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client

from personal_agent.mcp_client.core import (
    McpClientCore,
    McpTransportError,
    PROTOCOL_VERSION,
    StdioTransport,
    StreamableHttpTransport,
)

ENV = {"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"}

FINANCE_MODULE = "fixtures.mcp_servers.finance_fixture_server"
THIRDPARTY_MODULE = "fixtures.mcp_servers.thirdparty_server"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "personal-agent-conformance",
        "version": "0.1",
    },
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} did not become ready")


class HttpServer:
    def __init__(self, module: str) -> None:
        self.port = _free_port()
        self.module = module
        self.process = subprocess.Popen(
            [sys.executable, "-m", module, "http", str(self.port)],
            env=ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_ready(self.port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def __enter__(self) -> "HttpServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class LegacyRawServer:
    """A subprocess running the legacy 2025-11-25 raw JSON-RPC server."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "fixtures.mcp_servers.legacy_raw_server", str(self.port)],
            env=ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_ready(self.port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def __enter__(self) -> "LegacyRawServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# --- v2 client → v2 server: protocol and format ----------------------------


def test_v2_client_negotiates_2026_07_28_over_http() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        async def run() -> None:
            async with McpClientCore(
                "finance", StreamableHttpTransport(url=server.url)
            ) as client:
                assert client.identity.protocol_version == "2026-07-28"
                assert client.identity.name == "personal-data-mcp-fixture"
                assert client.identity.version == "0.1.0"

        asyncio.run(run())


def test_v2_client_negotiates_2026_07_28_over_stdio() -> None:
    async def run() -> None:
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", FINANCE_MODULE, "stdio"],
        )
        async with McpClientCore("finance", transport) as client:
            assert client.identity.protocol_version == "2026-07-28"

    asyncio.run(run())


def test_v2_tools_list_returns_snake_case_schema() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        async def run() -> None:
            async with McpClientCore(
                "finance", StreamableHttpTransport(url=server.url)
            ) as client:
                tools = await client.list_tools()
                assert len(tools) > 0
                for tool in tools:
                    assert hasattr(tool, "input_schema")
                    assert not hasattr(tool, "inputSchema")

        asyncio.run(run())


def test_v2_server_discover_succeeds_without_a_session_id() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        response = httpx.post(
            server.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": MODERN_META},
            },
            headers={
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "server/discover",
                "Content-Type": "application/json",
            },
            timeout=10,
            trust_env=False,
        )
        assert response.status_code == 200
        assert response.json()["result"]["supportedVersions"] == [
            PROTOCOL_VERSION
        ]
        assert "mcp-session-id" not in {k.lower() for k in response.headers}


def test_v2_server_rejects_a_method_header_body_mismatch() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        response = httpx.post(
            server.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": MODERN_META},
            },
            headers={
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "tools/list",
                "Content-Type": "application/json",
            },
            timeout=10,
            trust_env=False,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32020


def test_v2_server_refuses_delete() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        response = httpx.delete(server.url, timeout=5, trust_env=False)
        assert response.status_code in (405, 406)


# --- modern-only Finance versus explicitly compatible third parties --------


def test_generic_client_records_an_explicit_legacy_fallback() -> None:
    """The v2 client's negotiate_auto falls back to initialize when discover
    is unavailable and records the legacy server's actual identity.

    This compatibility leg is for explicitly configured third-party connectors,
    not the self-owned Finance connector.
    """
    with LegacyRawServer() as legacy:
        async def connect() -> None:
            async with McpClientCore(
                "legacy-third-party",
                StreamableHttpTransport(url=legacy.url),
            ) as client:
                assert client.identity.protocol_version == "2025-11-25"
                assert [tool.name for tool in await client.list_tools()] == [
                    "legacy.echo"
                ]

        asyncio.run(connect())


def test_finance_client_rejects_a_legacy_server() -> None:
    with LegacyRawServer() as legacy:
        async def attempt() -> None:
            async with McpClientCore(
                "finance",
                StreamableHttpTransport(url=legacy.url),
                allowed_protocol_versions=frozenset({PROTOCOL_VERSION}),
            ):
                pass

        with pytest.raises(McpTransportError) as excinfo:
            asyncio.run(attempt())
        assert "2025-11-25" in str(excinfo.value)


def test_finance_server_rejects_legacy_initialize_over_http() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        response = httpx.post(
            server.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-probe", "version": "0.1"},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=10,
            trust_env=False,
        )
        assert response.status_code in (200, 404)
        assert response.json()["error"]["code"] == -32601


def test_finance_server_reports_removed_initialize_on_modern_http() -> None:
    """Keep the official conformance finding covered by the local suite."""
    with HttpServer(FINANCE_MODULE) as server:
        response = httpx.post(
            server.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "modern-conformance-probe",
                        "version": "0.1",
                    },
                    "_meta": MODERN_META,
                },
            },
            headers={
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "initialize",
                "Content-Type": "application/json",
            },
            timeout=10,
            trust_env=False,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == -32601


def test_finance_server_reports_removed_ping_on_modern_http() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        response = httpx.post(
            server.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "ping",
                "params": {"_meta": MODERN_META},
            },
            headers={
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "ping",
                "Content-Type": "application/json",
            },
            timeout=10,
            trust_env=False,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == -32601


def test_finance_server_rejects_legacy_initialize_over_stdio() -> None:
    async def attempt() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", FINANCE_MODULE, "stdio"],
            env=ENV,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                with pytest.raises(MCPError) as excinfo:
                    await session.initialize()
                assert excinfo.value.code == -32601

    asyncio.run(attempt())


# --- third-party server: v2 client discovers tools -------------------------


def test_v2_client_discovers_third_party_tools() -> None:
    with HttpServer(THIRDPARTY_MODULE) as server:
        async def run() -> None:
            async with McpClientCore(
                "almanac", StreamableHttpTransport(url=server.url)
            ) as client:
                tools = await client.list_tools()
                names = {t.name for t in tools}
                assert "almanac.sunrise" in names

        asyncio.run(run())
